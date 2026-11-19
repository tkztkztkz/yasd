"""Unit tests for the YAMD multi-target frontend.

Run with:  python -m unittest discover -s tests -v
"""

import asyncio
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

import yamd


class FakeCollector:
    """Small stand-in for StrataCollector / HalogenCollector."""

    def __init__(self, snapshot, connected=True, error=None, sleeping=False,
                 state_log=None):
        self.snapshot = snapshot
        self._connected = connected
        self.error = error
        self.sleeping = sleeping
        self.state_log = state_log or []

    def get_snapshot(self):
        return self.snapshot

    def is_connected(self):
        return self._connected

    def is_sleeping(self):
        return self.sleeping

    def get_last_error(self):
        return self.error

    def get_state_log(self):
        return list(self.state_log)

    def get_raw_responses(self):
        return {}, {}, {}


def runtime(kind, snapshot, name=None, connected=True, error=None,
            sleeping=False, local=False):
    spec = yamd.TargetSpec(
        name=name or f"{kind}-1",
        kind=kind,
        url="http://127.0.0.1:9999",
    )
    collector = FakeCollector(snapshot, connected=connected, error=error,
                              sleeping=sleeping)
    return yamd.TargetRuntime(spec=spec, collector=collector, local=local)


def strata_snapshot(**overrides):
    snap = yamd.StrataSnapshot(timestamp=time.time())
    snap.loaded = True
    snap.state = "idle"
    snap.model = "strata-model"
    snap.max_context = 32768
    snap.ctx_used = 12000
    snap.concurrency_serving = 1
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


def halogen_snapshot(**overrides):
    snap = yamd.HalogenSnapshot(timestamp=time.time())
    snap.model = "halogen-model"
    snap.engine_up = True
    snap.capability_probe = "ok"
    snap.slots = 4
    snap.context = 32768
    snap.kv_usage_ratio = 0.42
    snap.cache_hit_rate = 0.60
    snap.cache_token_hit_rate = 0.70
    snap.draft_tokens_total = 100
    snap.draft_tokens_accepted = 71
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


def request_rows(count, prefix="r"):
    """Newest-first Strata request rows, enough to exercise paging."""
    now = time.time()
    return [{
        "id": f"{prefix}{i}",
        "time": now - i,
        "prompt_tokens": 1000 + i,
        "reused": 800,
        "output_tokens": 100,
        "decode_tok_s": 20.0,
        "prompt_ms": 200.0,
        "finish": "stop",
    } for i in range(count)]


class AdapterTests(unittest.TestCase):
    def test_strata_busy_and_digest(self):
        snap = strata_snapshot(
            state="generating",
            requests=[{
                "time": 1000.0,
                "prompt_tokens": 2000,
                "reused": 1500,
                "output_tokens": 300,
                "decode_tok_s": 42.5,
                "prompt_ms": 250.0,
                "finish": "stop",
            }],
        )
        view = yamd.make_view(runtime("strata", snap))
        self.assertEqual(view.badge, "BUSY")
        self.assertEqual(view.live, "1/1 q0")
        self.assertEqual(view.digest["cached_tokens"], 1500)
        self.assertAlmostEqual(view.digest["prefill_tok_s"], 2000.0, places=3)
        self.assertEqual(view.digest["decode_tok_s"], 42.5)

    def test_strata_badges(self):
        self.assertEqual(
            yamd.make_view(runtime("strata", strata_snapshot(queued=2))).badge,
            "QUEUED")
        self.assertEqual(
            yamd.make_view(runtime("strata", strata_snapshot(),
                                   sleeping=True)).badge,
            "SLEEPING")
        self.assertEqual(
            yamd.make_view(runtime("strata", strata_snapshot(),
                                   connected=False)).badge,
            "OFFLINE")

    def test_strata_unloaded_state_is_sleeping(self):
        # yamd._strata_badge treats live.state == "unloaded" as SLEEPING even when
        # /health omits `loaded`; YAMD must keep that mapping.
        snap = strata_snapshot(state="unloaded")
        self.assertEqual(yamd.make_view(runtime("strata", snap)).badge,
                         "SLEEPING")

    def test_halogen_badges(self):
        cases = [
            (halogen_snapshot(in_flight=2), "BUSY"),
            (halogen_snapshot(queued=1), "QUEUED"),
            (halogen_snapshot(engine_up=False), "NO ENGINE"),
            (halogen_snapshot(capability_probe="failed"), "DEGRADED"),
            (halogen_snapshot(), "IDLE"),
        ]
        for snap, expected in cases:
            self.assertEqual(yamd.make_view(runtime("halogen", snap)).badge,
                             expected)

        offline = yamd.make_view(runtime("halogen", halogen_snapshot(),
                                         connected=False))
        self.assertEqual(offline.badge, "OFFLINE")

    def test_halogen_digest(self):
        snap = halogen_snapshot(requests=[{
            "time": 1000.0,
            "requests": 2,
            "prompt_tokens": 3000,
            "cached_tokens": 2000,
            "output_tokens": 400,
            "prefill_tok_s": 800.0,
            "decode_tok_s": 44.0,
            "draft_hit": 0.71,
        }])
        view = yamd.make_view(runtime("halogen", snap))
        self.assertEqual(view.digest["requests"], 2)
        self.assertEqual(view.digest["output_tokens"], 400)
        self.assertAlmostEqual(view.digest["draft_hit"], 0.71)


class EngineVersionTests(unittest.TestCase):
    def test_strata_label(self):
        snap = strata_snapshot(engine_version="1.2.0")
        self.assertEqual(
            yamd._engine_version_label("strata", snap, 120), "Strata 1.2.0")
        self.assertEqual(
            yamd._engine_version_label("strata", snap, 60), "Strata 1.2.0")
        self.assertEqual(
            yamd._engine_version_label("strata", snap, 40), "")
        snap.engine_version = ""
        self.assertEqual(
            yamd._engine_version_label("strata", snap, 120), "")

    def test_halogen_label(self):
        snap = halogen_snapshot(api_version="2.0.0", engine_version="2.1.0")
        self.assertEqual(
            yamd._engine_version_label("halogen", snap, 120),
            "Halogen 2.0.0 · engine 2.1.0")
        self.assertEqual(
            yamd._engine_version_label("halogen", snap, 80),
            "2.1.0 · API 2.0.0")
        self.assertEqual(
            yamd._engine_version_label("halogen", snap, 60), "2.1.0")

        snap.engine_version = "2.0.0"
        self.assertEqual(
            yamd._engine_version_label("halogen", snap, 120),
            "Halogen 2.0.0")
        self.assertEqual(
            yamd._engine_version_label("halogen", snap, 60), "2.0.0")

    def test_panel_title_carries_version(self):
        snap = strata_snapshot(engine_version="1.2.0")
        panel = yamd._make_engine_panel("strata", snap, 120)
        buf = io.StringIO()
        Console(file=buf, width=120, force_terminal=False).print(panel)
        self.assertIn("Engine · Strata 1.2.0", buf.getvalue())

        snap = halogen_snapshot(api_version="2.0.0", engine_version="2.1.0")
        panel = yamd._make_engine_panel("halogen", snap, 120)
        buf = io.StringIO()
        Console(file=buf, width=120, force_terminal=False).print(panel)
        self.assertIn("Engine · Halogen 2.0.0 · engine 2.1.0", buf.getvalue())


class StrataDraftingTests(unittest.TestCase):
    def _text(self, snapshot, width=120):
        buf = io.StringIO()
        Console(file=buf, width=width, force_terminal=False).print(
            yamd.strata_make_engine_panel(snapshot, width))
        return buf.getvalue()

    def test_dflash2(self):
        snap = strata_snapshot(
            drafting="dflash2", spec=8, mtp_max=6, lookup=3,
            pool_workers=15, kv="int8", expert_slots=3945,
            expert_cache_mib=7709)
        text = self._text(snap)
        self.assertIn("Draft:", text)
        self.assertIn("DFlash2", text)
        self.assertIn("spec 8", text)
        self.assertIn("lookup 3", text)
        self.assertIn("pool 15", text)

    def test_dflash2_narrow_drops_tail(self):
        snap = strata_snapshot(
            drafting="dflash2", spec=8, mtp_max=6, lookup=3,
            pool_workers=15)
        text = self._text(snap, width=80)
        self.assertIn("DFlash2 · lookup 3", text)
        self.assertNotIn("pool 15", text)

    def test_mtp(self):
        snap = strata_snapshot(
            drafting="mtp", spec=8, mtp_max=6, lookup=3, pool_workers=15)
        text = self._text(snap)
        self.assertIn("Draft:", text)
        self.assertIn("MTP ≤5", text)
        self.assertNotIn("DFlash", text)

    def test_off(self):
        snap = strata_snapshot(drafting="off", lookup=3, pool_workers=2)
        text = self._text(snap)
        self.assertIn("Draft:", text)
        self.assertIn("off", text)

    def test_legacy_without_drafting_field(self):
        snap = strata_snapshot(drafting="", spec=8, mtp_max=6, lookup=3)
        text = self._text(snap)
        self.assertIn("Spec:", text)
        self.assertIn("MTP ≤5", text)


class StrataRequestRingTests(unittest.TestCase):
    def setUp(self):
        self.collector = yamd.StrataCollector(
            "http://127.0.0.1:1", req_rows=12, seed_all=True)

    def test_merge_dedup_and_newest_first(self):
        self.collector._merge_requests([
            {"id": "a", "time": 1.0},
            {"id": "b", "time": 2.0},
            {"id": "c", "time": 3.0},
        ])
        self.collector._merge_requests([
            {"id": "d", "time": 4.0},
            {"id": "b", "time": 2.0},
        ])
        self.assertEqual(
            [row["id"] for row in self.collector._ring_requests()],
            ["d", "c", "b", "a"],
        )

    def test_ring_cap_keeps_newest(self):
        self.collector._req_ring_max = 2
        self.collector._merge_requests([
            {"id": "a", "time": 1.0},
            {"id": "b", "time": 2.0},
            {"id": "c", "time": 3.0},
        ])
        self.assertEqual(
            [row["id"] for row in self.collector._ring_requests()],
            ["c", "b"],
        )

    def test_fallback_key_without_id(self):
        row = {"time": 1.0, "prompt_tokens": 2, "output_tokens": 3}
        self.assertEqual(yamd.StrataCollector._request_key(row), "1.0:2:3")


class GraphAlignmentTests(unittest.TestCase):
    def test_short_series_is_left_padded_not_stretched(self):
        graph = yamd._braille_graph([1.0] * 5, 2.0, 10)   # 10 cols, 5 samples
        self.assertEqual(len(graph), 10)
        self.assertTrue(graph.startswith(" "), graph)

    def test_long_series_uses_newest_samples_only(self):
        # width 5 -> 10 virtual columns; changing older samples must not
        # reshape the curve (no bucketing/resampling).
        a = [float(i + 1) for i in range(20)]
        b = [999.0] * 10 + a[10:]
        self.assertEqual(yamd._braille_graph(a, 20.0, 5),
                         yamd._braille_graph(b, 20.0, 5))

    def test_graph_uses_the_newest_window(self):
        values = [float(i + 1) for i in range(120)]
        graph = yamd._braille_graph(values, 120.0, 55)    # newest 110 samples
        self.assertEqual(len(graph), 55)
        self.assertFalse(graph.startswith(" "), graph)


class StrataGraphRingTests(unittest.TestCase):
    def setUp(self):
        self.collector = yamd.StrataCollector("http://127.0.0.1:1")
        self.collector._graph_max = 5

    def _snapshot(self, ts):
        return yamd.StrataSnapshot(timestamp=ts)

    def test_seeds_from_server_history(self):
        snap = self._snapshot(100.0)
        self.collector._update_graph_ring(snap, {
            "history": {"cpu": [float(i) for i in range(10)]},
        })
        self.assertEqual(snap.client_history["cpu"], [5.0, 6.0, 7.0, 8.0, 9.0])
        self.assertEqual(self.collector._graph_last_sec, 100)

    def test_appends_once_per_second(self):
        self.collector._update_graph_ring(self._snapshot(100.0), {
            "history": {"cpu": [float(i) for i in range(10)]},
        })
        # Same second: no sample is added.
        self.collector._update_graph_ring(self._snapshot(100.5), {
            "history": {"cpu": [float(i) for i in range(10)]},
        })
        self.assertEqual(len(self.collector._graph_ring["cpu"]), 5)
        # New second: one sample is added and the ring stays capped.
        self.collector._update_graph_ring(self._snapshot(101.0), {
            "history": {"cpu": [float(i) for i in range(10)]},
        })
        self.assertEqual(self.collector._graph_ring["cpu"],
                         [6.0, 7.0, 8.0, 9.0, 9.0])

    def test_late_sensor_key_is_seeded(self):
        self.collector._update_graph_ring(self._snapshot(100.0), {
            "history": {"cpu": [1.0, 2.0]},
        })
        snap = self._snapshot(101.0)
        self.collector._update_graph_ring(snap, {
            "history": {"cpu": [1.0, 2.0], "gpu_util": [3.0, 4.0]},
        })
        self.assertEqual(snap.client_history["gpu_util"], [3.0, 4.0])


class ConfigTests(unittest.TestCase):
    def test_parse_target_arg(self):
        spec = yamd.parse_target_arg("main=halogen@127.0.0.1:8731")
        self.assertEqual(spec.name, "main")
        self.assertEqual(spec.kind, "halogen")
        self.assertEqual(spec.url, "http://127.0.0.1:8731")

        spec = yamd.parse_target_arg("strata@10.0.0.5:8080")
        self.assertEqual(spec.kind, "strata")
        self.assertIn("10.0.0.5", spec.name)

        spec = yamd.parse_target_arg("10.0.0.5:8080")
        self.assertEqual(spec.kind, "auto")
        self.assertEqual(spec.url, "http://10.0.0.5:8080")

        spec = yamd.parse_target_arg("x=http://h:1/v1")
        self.assertEqual(spec.name, "x")
        self.assertEqual(spec.url, "http://h:1")

    def test_json_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.json"
            path.write_text(json.dumps({"targets": [{
                "name": "h",
                "kind": "halogen",
                "url": "127.0.0.1:8731",
                "api_key_env": "YAMD_TEST_KEY",
            }]}), encoding="utf-8")
            os.environ["YAMD_TEST_KEY"] = "secret"
            specs = yamd.load_config_file(path)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].name, "h")
            self.assertEqual(specs[0].url, "http://127.0.0.1:8731")
            self.assertEqual(specs[0].api_key, "secret")
            del os.environ["YAMD_TEST_KEY"]

    def test_toml_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text(
                "[[target]]\n"
                'name = "s"\n'
                'kind = "strata"\n'
                'url = "10.0.0.7:8080"\n',
                encoding="utf-8",
            )
            specs = yamd.load_config_file(path)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].kind, "strata")
            self.assertEqual(specs[0].url, "http://10.0.0.7:8080")

    def test_direct_api_key_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text(
                "[[target]]\n"
                'name = "local"\n'
                'kind = "halogen"\n'
                'url = "127.0.0.1:8731"\n'
                'api_key = "literal-key"\n'
                'api_key_env = "YAMD_TEST_KEY"\n',
                encoding="utf-8",
            )
            os.environ["YAMD_TEST_KEY"] = "from-env"
            specs = yamd.load_config_file(path)
            self.assertEqual(specs[0].api_key, "literal-key")
            del os.environ["YAMD_TEST_KEY"]

            # With only api_key, it is used as-is.
            path.write_text(
                "[[target]]\n"
                'name = "local"\n'
                'kind = "halogen"\n'
                'url = "127.0.0.1:8731"\n'
                'api_key = "only-literal"\n',
                encoding="utf-8",
            )
            self.assertEqual(yamd.load_config_file(path)[0].api_key,
                             "only-literal")

    def test_build_specs_cli_and_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "targets.json"
            config.write_text("{}", encoding="utf-8")
            args = Namespace(
                server=None, name=None, kind="auto", target=[], config=str(config),
                api_key=None,
            )
            specs = yamd.build_specs(args)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].kind, "halogen")

            args.target = ["a=halogen@127.0.0.1:8731",
                           "b=strata@10.0.0.5:8080"]
            specs = yamd.build_specs(args)
            self.assertEqual([s.name for s in specs], ["a", "b"])

            args.server = "10.0.0.9:8731"
            specs = yamd.build_specs(args)
            self.assertEqual(specs[0].url, "http://10.0.0.9:8731")
            self.assertEqual(len(specs), 3)

    def test_cli_replaces_config_target_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "targets.json"
            config.write_text(json.dumps({"targets": [{
                "name": "a", "kind": "halogen", "url": "127.0.0.1:8731",
            }]}), encoding="utf-8")
            args = Namespace(
                server=None, name=None, kind="auto",
                target=["a=strata@10.0.0.9:8080"], config=str(config),
                api_key=None,
            )
            specs = yamd.build_specs(args)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].name, "a")
            self.assertEqual(specs[0].kind, "strata")
            self.assertEqual(specs[0].url, "http://10.0.0.9:8080")


def web_args(**overrides):
    base = dict(web=None, web_only=None, port=None, listen=None, view=None)
    base.update(overrides)
    return Namespace(**base)


class WebConfigTests(unittest.TestCase):
    def test_toml_web_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text(
                "[web]\n"
                "enabled = true\n"
                "port = 9000\n"
                'listen = "127.0.0.1"\n'
                "only = true\n",
                encoding="utf-8",
            )
            cfg = yamd.load_web_config(path)
            opts = yamd.resolve_web_options(web_args(), cfg)
            self.assertTrue(opts.enabled)
            self.assertTrue(opts.only)
            self.assertEqual(opts.port, 9000)
            self.assertEqual(opts.listen, "127.0.0.1")

    def test_json_web_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.json"
            path.write_text(json.dumps({
                "targets": [],
                "web": {"enabled": True, "port": 9100,
                        "listen": "0.0.0.0"},
            }), encoding="utf-8")
            cfg = yamd.load_web_config(path)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["port"], 9100)

    def test_cli_overrides_config(self):
        cfg = {"enabled": True, "only": True, "port": 9000,
               "listen": "0.0.0.0"}

        opts = yamd.resolve_web_options(
            web_args(web=True), cfg)
        self.assertTrue(opts.enabled)
        self.assertFalse(opts.only)          # --web = terminal + browser

        opts = yamd.resolve_web_options(
            web_args(web=False), cfg)
        self.assertFalse(opts.enabled)
        self.assertFalse(opts.only)          # --no-web wins

        opts = yamd.resolve_web_options(
            web_args(web_only=True), {"enabled": False})
        self.assertTrue(opts.enabled)
        self.assertTrue(opts.only)

        opts = yamd.resolve_web_options(
            web_args(port=9200), {"enabled": False})
        self.assertTrue(opts.enabled)
        self.assertEqual(opts.port, 9200)

        opts = yamd.resolve_web_options(
            web_args(listen="127.0.0.1"), cfg)
        self.assertEqual(opts.listen, "127.0.0.1")

    def test_config_port_alone_does_not_enable(self):
        opts = yamd.resolve_web_options(
            web_args(), {"port": 9000})
        self.assertFalse(opts.enabled)

    def test_invalid_port(self):
        with self.assertRaises(ValueError):
            yamd.resolve_web_options(web_args(), {"port": "nope"})
        with self.assertRaises(ValueError):
            yamd.resolve_web_options(web_args(), {"port": 70000})

    def test_no_web_plus_web_only_is_rejected(self):
        with self.assertRaises(ValueError):
            yamd.resolve_web_options(
                web_args(web=False, web_only=True), {})

    def test_no_web_overrides_port(self):
        # --port implies --web, but an explicit --no-web must still win.
        opts = yamd.resolve_web_options(web_args(web=False, port=9000), {})
        self.assertFalse(opts.enabled)
        self.assertEqual(opts.port, 9000)


class UiConfigTests(unittest.TestCase):
    def test_ui_table_sets_startup_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text('[ui]\nview = "split"\n', encoding="utf-8")
            cfg = yamd.load_ui_config(path)
            self.assertEqual(
                yamd.resolve_ui_options(web_args(), cfg, 2), "split")

    def test_top_level_view_shorthand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text('view = "detail"\n', encoding="utf-8")
            cfg = yamd.load_ui_config(path)
            self.assertEqual(cfg["view"], "detail")

    def test_ui_table_does_not_affect_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.toml"
            path.write_text(
                '[[target]]\nname = "t"\nurl = "127.0.0.1:8731"\n'
                '[ui]\nview = "split"\n',
                encoding="utf-8",
            )
            specs = yamd.load_config_file(path)
            self.assertEqual([spec.name for spec in specs], ["t"])
            self.assertEqual(yamd.load_ui_config(path)["view"], "split")

    def test_auto_view_uses_target_count(self):
        args = web_args()
        cfg = {"view": "auto", "auto_split_max": 3}
        self.assertEqual(yamd.resolve_ui_options(args, cfg, 3), "split")
        self.assertEqual(yamd.resolve_ui_options(args, cfg, 4), "monitor")
        # Default threshold is 3, and --view auto works too.
        self.assertEqual(
            yamd.resolve_ui_options(web_args(view="auto"),
                                    {"view": "auto"}, 3), "split")
        self.assertEqual(
            yamd.resolve_ui_options(web_args(view="auto"),
                                    {"view": "auto"}, 4), "monitor")

    def test_cli_view_overrides_config(self):
        self.assertEqual(
            yamd.resolve_ui_options(
                web_args(view="detail"), {"view": "split"}, 2),
            "detail")

    def test_invalid_ui_values(self):
        with self.assertRaises(ValueError):
            yamd.resolve_ui_options(web_args(), {"view": "wide"}, 1)
        with self.assertRaises(ValueError):
            yamd.resolve_ui_options(
                web_args(), {"view": "auto", "auto_split_max": 0}, 1)
        with self.assertRaises(ValueError):
            yamd.resolve_ui_options(
                web_args(), {"view": "auto", "auto_split_max": "nope"}, 1)

    def test_blink_defaults_to_false(self):
        self.assertFalse(yamd.resolve_blink({}))

    def test_blink_true(self):
        self.assertTrue(yamd.resolve_blink({"blink": True}))

    def test_blink_must_be_bool(self):
        with self.assertRaises(ValueError):
            yamd.resolve_blink({"blink": "yes"})


class StrataBlinkTests(unittest.TestCase):
    @staticmethod
    def _render(blink):
        snap = yamd.StrataSnapshot(timestamp=time.time())
        snap.loaded = True
        snap.max_context = 1000
        snap.ctx_used = 990
        panel = yamd.strata_make_metrics_panel(snap, 120, 0, True, blink=blink)
        buf = io.StringIO()
        Console(file=buf, width=120, force_terminal=False,
                color_system=None).print(panel)
        return buf.getvalue()

    def test_blink_off_keeps_solid_red_bar(self):
        # Halogen-style warning: the >95% bar stays visible (red), no blink.
        self.assertIn("█", self._render(False))

    def test_blink_on_is_time_based(self):
        # Odd seconds draw the bar, even seconds hide it; this works at the
        # lower YAMD refresh rate too.
        with mock.patch.object(yamd.time, "monotonic", return_value=1):
            self.assertIn("█", self._render(True))
        with mock.patch.object(yamd.time, "monotonic", return_value=0):
            self.assertNotIn("█", self._render(True))


class KeyReaderTests(unittest.TestCase):
    def test_parse_keys(self):
        parse = yamd.KeyReader.parse_keys
        self.assertEqual(parse(b"\t"), ["tab"])
        self.assertEqual(parse(b"\x1b[Z"), ["shift_tab"])
        self.assertEqual(parse(b"\x1b[A"), ["up"])
        self.assertEqual(parse(b"\x1b[B"), ["down"])
        self.assertEqual(parse(b"\x1b[5~"), ["pgup"])
        self.assertEqual(parse(b"\x1b[6~"), ["pgdn"])
        self.assertEqual(parse(b"\x1b[H"), ["home"])
        self.assertEqual(parse(b"\x1b[F"), ["end"])
        self.assertEqual(parse(b"\x1b[1~"), ["home"])
        self.assertEqual(parse(b"\x1b[4~"), ["end"])
        self.assertEqual(parse(b"mDq"), ["m", "d", "q"])
        self.assertEqual(parse(b"\x03"), ["quit"])
        self.assertEqual(parse(b"1"), ["1"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.strata = runtime("strata", strata_snapshot(
            state="reading",
            requests=[{
                "time": time.time(),
                "prompt_tokens": 2000,
                "reused": 1000,
                "output_tokens": 120,
                "decode_tok_s": 33.0,
                "prompt_ms": 100.0,
            }],
        ), name="strata-a")
        self.halogen = runtime("halogen", halogen_snapshot(
            in_flight=2,
            busy_for_s=3.4,
            requests=[{
                "time": time.time(),
                "requests": 1,
                "prompt_tokens": 1200,
                "cached_tokens": 900,
                "output_tokens": 340,
                "prefill_tok_s": 812.0,
                "decode_tok_s": 42.1,
                "draft_hit": 0.71,
            }],
        ), name="halogen-a", local=True)

        self.app = yamd.YAMDApplication(specs=[])
        self.app._runtimes = [self.strata, self.halogen]

    def _render(self, width, height):
        layout = self.app._layout_for(width, height)
        buf = io.StringIO()
        console = Console(file=buf, width=width, height=height,
                          force_terminal=False, color_system=None,
                          highlight=False)
        console.print(layout)
        return buf.getvalue()

    def test_monitor_sizes(self):
        for width, height in ((120, 40), (80, 24), (60, 20)):
            out = self._render(width, height)
            self.assertIn("halogen-a", out)
            self.assertIn("strata-a", out)

    def test_web_layout_has_no_keys_row(self):
        # The browser has its own button bar, so its frame must not spend a
        # row on the terminal key-help line.
        self.app.view_mode = "monitor"
        browser = self.app._layout_for(80, 24, web=True)
        self.assertNotIn("keys", [child.name for child in browser.children])
        terminal = self.app._layout_for(80, 24)
        self.assertIn("keys", [child.name for child in terminal.children])

    def test_summary_keeps_note_on_24_rows(self):
        # 80x24 is the classic terminal: the Selected panel must still show
        # its fifth row (the error reason) rather than clipping it.
        self.app.view_mode = "monitor"
        self.app._selected = 1
        self.app._runtimes[1].collector.error = "connection refused"
        out = self._render(80, 24)
        self.assertIn("Note:", out)
        self.assertIn("connection refused", out)

    def test_summary_keeps_endpoint_on_24_rows(self):
        out = self._render(80, 24)
        self.assertIn("Endpoint:", out)

    def test_detail_header_keeps_badge_at_narrow_widths(self):
        long = runtime("halogen", halogen_snapshot(in_flight=1),
                       name="a-very-long-target-name-here")
        self.app._runtimes = [long]
        self.app.view_mode = "detail"
        for width in (40, 70, 90, 100):
            header = self._render(width, 30).splitlines()[1]
            self.assertIn("YAMD", header)
            self.assertIn("BUSY", header)

    def test_monitor_header_drops_clock_before_status(self):
        many = [runtime("halogen", halogen_snapshot(), name=f"t{i}")
                for i in range(12)]
        self.app._runtimes = many
        header = self._render(57, 24).splitlines()[1]
        self.assertIn("12 targets", header)
        self.assertIn("12 online", header)
        # No clock: dropping it is what keeps the three cells from colliding.
        self.assertNotRegex(header, r"\d{2}:\d{2}:\d{2}")

    def test_detail_both_kinds(self):
        self.app.view_mode = "detail"
        for index, name in ((0, "strata-a"), (1, "halogen-a")):
            self.app._selected = index
            out = self._render(120, 40)
            self.assertIn(name, out)
            self.assertIn("Metrics", out)

    def test_split(self):
        self.app._selected = 1
        out = self._render(120, 40)
        self.assertIn("Targets", out)
        self.assertIn("halogen-a", out)

    def test_split_shrinks_targets_when_few(self):
        self.app.view_mode = "split"
        one = self.app._render_split(120, 40, self.app._views()[:1])
        self.assertEqual(one["targets"].size, 4)     # 1 target + borders/header
        two = self.app._render_split(120, 40, self.app._views())
        self.assertEqual(two["targets"].size, 5)     # 2 targets + borders/header

    def test_split_caps_targets_at_configured_ratio(self):
        many = [runtime("halogen", halogen_snapshot(), name=f"t{i}")
                for i in range(20)]
        self.app._runtimes = many
        layout = self.app._render_split(120, 40, self.app._views())
        # usable = 40 - 3 header - 1 title - 1 keys = 35; 35% -> 12 rows.
        self.assertEqual(layout["targets"].size, 12)
        self.assertEqual(layout["targets"].size,
                         max(4, int(35 * self.app.split_pct / 100)))

    def test_monitor_scroll(self):
        many = [runtime("halogen", halogen_snapshot(), name=f"t{i}")
                for i in range(20)]
        self.app._runtimes = many
        self.app._selected = 19
        out = self._render(100, 24)
        self.assertIn("t19", out)
        self.assertIn("of 20", out)

    def test_requests_fill_available_rows(self):
        rows = request_rows(30)
        rt = runtime("strata", strata_snapshot(requests=rows), name="s")
        self.app._runtimes = [rt]
        self.app._selected = 0
        self.app.view_mode = "detail"
        self.app.req_rows = None
        out = self._render(120, 40)
        # 120x40 detail: 18 rows fit, so a 30-row ring is not capped at 12.
        self.assertIn("18 of 30 kept", out)

    def test_requests_scrolled_range(self):
        rows = request_rows(30)
        rt = runtime("strata", strata_snapshot(requests=rows), name="s")
        rt.req_anchor = yamd._request_row_key(rows[12])
        self.app._runtimes = [rt]
        self.app._selected = 0
        self.app.view_mode = "detail"
        self.app.req_rows = None
        out = self._render(120, 40)
        # The anchored row is the top of the page, not the start of the ring.
        self.assertIn("13–30 of 30 kept", out)

    def test_pgdn_uses_rendered_page_size(self):
        rows = request_rows(30)
        rt = runtime("strata", strata_snapshot(requests=rows), name="s")
        self.app._runtimes = [rt]
        self.app._selected = 0
        self.app.view_mode = "detail"
        self.app.req_rows = None
        self._render(120, 40)               # records 18 visible rows
        self.app._handle_key("pgdn")
        self.assertEqual(rt.req_anchor, yamd._request_row_key(rows[12]))
        out = self._render(120, 40)
        self.assertIn("13–30 of 30 kept", out)

    def test_halogen_requests_scrolled_range(self):
        now = time.time()
        rows = [{
            "time": now - i,
            "requests": 1,
            "prompt_tokens": 1000 + i,
            "cached_tokens": 800,
            "output_tokens": 100,
            "prefill_tok_s": 800.0,
            "decode_tok_s": 20.0,
            "draft_hit": 0.5,
        } for i in range(30)]
        rt = runtime("halogen", halogen_snapshot(requests=rows), name="h")
        rt.req_anchor = yamd._request_row_key(rows[12])
        self.app._runtimes = [rt]
        self.app._selected = 0
        self.app.view_mode = "detail"
        self.app.req_rows = None
        out = self._render(120, 40)
        self.assertIn("13–30/30 windows", out)

    def test_requests_anchor_survives_new_rows(self):
        rows = request_rows(30)
        anchor = yamd._request_row_key(rows[12])
        self.assertEqual(yamd._request_offset(rows, 18, anchor), 12)

        newer = request_rows(1, prefix="new")
        shifted = newer + rows
        # The same row moved down by one; the view stays on it instead of
        # jumping back to the newest page.
        self.assertEqual(yamd._request_offset(shifted, 18, anchor), 13)

    def test_requests_anchor_falls_back_to_oldest(self):
        rows = request_rows(30)
        self.assertEqual(
            yamd._request_offset(rows, 10, "row-that-was-pruned"), 20)
        self.assertEqual(yamd._request_offset([], 10, "gone"), 0)

    def test_requests_follow_newest_by_default(self):
        rows = request_rows(30)
        self.assertEqual(yamd._request_offset(rows, 18, None), 0)

    def test_detail_offline_panel(self):
        offline = runtime("halogen", halogen_snapshot(), name="offline",
                          connected=False, error="connection refused")
        self.app._runtimes = [offline]
        self.app._selected = 0
        self.app.view_mode = "detail"
        out = self._render(100, 30)
        self.assertIn("SERVER OFFLINE", out)
        self.assertIn("connection refused", out)


class RequestScrollTests(unittest.TestCase):
    def setUp(self):
        self.rows = request_rows(30)
        self.rt = runtime("strata", strata_snapshot(requests=self.rows),
                          name="s")
        # Pin the page size instead of rendering a frame first.
        self.rt.req_page_rows = 5
        self.app = yamd.YAMDApplication(specs=[])
        self.app._runtimes = [self.rt]
        self.app.view_mode = "detail"

    def test_page_and_row_keys(self):
        self.app._handle_key("pgdn")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[5]))
        self.app._handle_key("j")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[6]))
        self.app._handle_key("k")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[5]))
        self.app._handle_key("pgdn")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[10]))

    def test_home_and_end(self):
        self.app._handle_key("end")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[25]))
        # Paging past the oldest row stays on the last page.
        self.app._handle_key("pgdn")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[25]))
        self.app._handle_key("home")
        self.assertIsNone(self.rt.req_anchor)
        # Paging past the newest row keeps the follow-newest state.
        self.app._handle_key("pgup")
        self.assertIsNone(self.rt.req_anchor)

    def test_monitor_ignores_scroll_keys(self):
        self.app.view_mode = "monitor"
        self.app._handle_key("pgdn")
        self.assertIsNone(self.rt.req_anchor)

    def test_anchor_is_per_target(self):
        other_rows = request_rows(10, prefix="b")
        other = runtime("strata", strata_snapshot(requests=other_rows),
                        name="other")
        other.req_page_rows = 5
        self.app._runtimes = [self.rt, other]
        self.app._handle_key("pgdn")
        self.app._handle_key("tab")
        self.assertIsNone(other.req_anchor)
        self.app._handle_key("pgdn")
        self.assertEqual(other.req_anchor,
                         yamd._request_row_key(other_rows[5]))
        self.app._handle_key("shift_tab")
        self.assertEqual(self.rt.req_anchor,
                         yamd._request_row_key(self.rows[5]))


class WebInputTests(unittest.TestCase):
    def setUp(self):
        self.app = yamd.YAMDApplication(specs=[], cycle=5.0, hold=10.0)
        self.app._runtimes = [
            runtime("halogen", halogen_snapshot(), name=f"t{i}")
            for i in range(3)
        ]

    def test_html_has_controls_and_input(self):
        html = yamd._web_html()
        self.assertIn("<title>YAMD</title>", html)
        self.assertIn("disableStdin: false", html)
        self.assertIn("term.onData", html)
        self.assertIn('id="bar"', html)
        self.assertIn('data-cmd="next"', html)
        self.assertIn('data-cmd="split"', html)
        self.assertIn('data-cmd="req_older"', html)
        self.assertIn('data-cmd="req_newer"', html)
        # Font-size controls live in the browser and persist per browser.
        self.assertIn('data-font="-1"', html)
        self.assertIn('data-font="1"', html)
        self.assertIn('id="fontsize"', html)
        self.assertIn("stepFont", html)
        self.assertIn("yamd.fontSize", html)
        # Fullscreen toggle uses the browser API and refits on change.
        self.assertIn('data-fullscreen="1"', html)
        self.assertIn("toggleFullscreen", html)
        self.assertIn("fullscreenchange", html)
        # The rotation mode moved from the frame to the HTML bar.
        self.assertIn('id="mode"', html)
        self.assertIn("refreshState", html)
        self.assertIn('"/state"', html)

    def test_html_shows_disconnect_banner(self):
        # The dashboard server cannot render this itself: while it is down no
        # frame is produced, so the browser must announce the lost WebSocket.
        html = yamd._web_html()
        self.assertIn('id="net"', html)
        self.assertIn("Dashboard disconnected", html)
        self.assertIn("setLink(false)", html)
        self.assertIn("setLink(true)", html)
        # Input focus still returns on every successful connection.
        self.assertIn("term.focus();", html)

    def test_html_fails_loudly_if_upstream_changes(self):
        original = yamd.WEB_HTML
        yamd.WEB_HTML = "<html></html>"
        try:
            with self.assertRaises(RuntimeError):
                yamd._web_html()
        finally:
            yamd.WEB_HTML = original

    def test_web_render_error_uses_yamd_name(self):
        self.assertEqual(yamd.YAMDWebServer.RENDER_ERROR_PREFIX, "YAMD")

    def test_web_state_reports_mode(self):
        # The browser bar polls /state for the text that used to live at the
        # end of the terminal key-help line.
        self.assertTrue(self.app.web_state()["mode"].startswith("auto"))
        self.app._auto = False
        self.assertEqual(self.app.web_state()["mode"],
                         "manual (a resumes)")

    def test_view_commands(self):
        for view in ("detail", "split", "monitor"):
            self.app.handle_web_message(json.dumps({
                "type": "command", "command": view,
            }))
            self.assertEqual(self.app.view_mode, view)

    def test_request_scroll_commands(self):
        rows = request_rows(10)
        rt = runtime("strata", strata_snapshot(requests=rows), name="s")
        rt.req_page_rows = 3
        self.app._runtimes = [rt]
        self.app.view_mode = "detail"
        self.app.handle_web_message(json.dumps({
            "type": "command", "command": "req_older",
        }))
        self.assertEqual(rt.req_anchor, yamd._request_row_key(rows[3]))
        self.app.handle_web_message(json.dumps({
            "type": "command", "command": "req_newer",
        }))
        self.assertIsNone(rt.req_anchor)

    def test_target_commands_and_keys(self):
        self.app.handle_web_message(json.dumps({
            "type": "command", "command": "next",
        }))
        self.assertEqual(self.app._selected, 1)
        self.app.handle_web_message(json.dumps({
            "type": "command", "command": "prev",
        }))
        self.assertEqual(self.app._selected, 0)
        self.app.handle_web_message(json.dumps({
            "type": "input", "data": "\t",
        }))
        self.assertEqual(self.app._selected, 1)
        self.app.handle_web_message(json.dumps({
            "type": "input", "data": "2",
        }))
        self.assertEqual(self.app._selected, 1)
        self.app.handle_web_message(json.dumps({
            "type": "input", "data": "\u001b[Z",
        }))
        self.assertEqual(self.app._selected, 0)

    def test_auto_toggle_from_web(self):
        self.assertTrue(self.app._auto)
        self.app.handle_web_message(json.dumps({
            "type": "input", "data": "a",
        }))
        self.assertFalse(self.app._auto)
        self.app.handle_web_message(json.dumps({
            "type": "command", "command": "auto",
        }))
        self.assertTrue(self.app._auto)

    def test_web_cannot_quit(self):
        self.assertTrue(self.app._running)
        for data in ("q", "\u0003"):
            self.app.handle_web_message(json.dumps({
                "type": "input", "data": data,
            }))
            self.assertTrue(self.app._running)

    def test_bad_messages_are_ignored(self):
        for raw in ("", "not-json", "[]", '{"type":"unknown"}',
                    '{"type":"command","command":"nope"}'):
            self.app.handle_web_message(raw)
        self.assertEqual(self.app.view_mode, "monitor")
        self.assertTrue(self.app._running)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.app = yamd.YAMDApplication(specs=[], cycle=5.0, hold=10.0)
        self.flags = [False, False, False]
        self.app._runtimes = [
            runtime("halogen", halogen_snapshot(), name=f"t{i}")
            for i in range(3)
        ]
        for i, rt in enumerate(self.app._runtimes):
            rt.last_used_ts = float(i + 1)
        self.app._views = lambda: [
            SimpleNamespace(active=self.flags[i], digest={})
            for i in range(3)
        ]

    def test_seeds_on_last_used(self):
        self.app._tick_selection(0.0, 0.0)
        self.assertEqual(self.app._selected, 2)

    def test_cycles_active_and_stops_on_last_used(self):
        self.app._tick_selection(0.0, 0.0)      # seed
        self.flags = [True, True, False]
        self.app._selected = 0
        self.app._tick_selection(100.0, 100.0)
        self.assertEqual(self.app._selected, 0)
        self.app._tick_selection(105.1, 105.1)
        self.assertEqual(self.app._selected, 1)

        # Everything goes idle: the view freezes on the target that ran last.
        self.flags = [False, False, False]
        self.app._runtimes[2].last_used_ts = 500.0
        self.app._tick_selection(106.0, 106.0)
        self.assertEqual(self.app._selected, 2)

    def test_scrolled_requests_pause_rotation(self):
        self.app._tick_selection(0.0, 0.0)      # seed on t2
        self.flags = [True, True, False]
        self.app.view_mode = "detail"
        self.app._selected = 0
        self.app._runtimes[0].req_anchor = "r0"
        self.app._tick_selection(100.0, 100.0)
        self.assertEqual(self.app._selected, 0)
        self.app._tick_selection(105.1, 105.1)
        self.assertEqual(self.app._selected, 0)

        # Returning to the newest row releases the hold; the next rotation
        # window then moves on as usual.
        self.app._runtimes[0].req_anchor = None
        self.app._tick_selection(105.2, 105.2)
        self.app._tick_selection(110.3, 110.3)
        self.assertEqual(self.app._selected, 1)

    def test_server_clock_skew_does_not_drive_last_used(self):
        for rt in self.app._runtimes:
            rt.last_used_ts = 0.0
        future = time.time() + 3600
        self.app._views = lambda: [
            SimpleNamespace(active=False, digest={"time": future}),
            SimpleNamespace(active=False, digest={}),
            SimpleNamespace(active=False, digest={}),
        ]
        self.app._tick_selection(10.0, 1000.0)
        # First sighting only records the digest key; it must not trust the
        # (skewed) server clock as "used now".
        self.assertEqual(self.app._runtimes[0].last_used_ts, 0.0)

        # A new completion is stamped with the dashboard's own clock.
        self.app._views = lambda: [
            SimpleNamespace(active=False, digest={"time": future + 1}),
            SimpleNamespace(active=False, digest={}),
            SimpleNamespace(active=False, digest={}),
        ]
        self.app._tick_selection(11.0, 1001.0)
        self.assertEqual(self.app._runtimes[0].last_used_ts, 1001.0)

    def test_selection_change_wakes_renderer(self):
        self.app._render_event.clear()
        self.app._selected = 0
        self.flags = [False, False, True]
        self.app._tick_selection(100.0, 100.0)
        self.assertEqual(self.app._selected, 2)
        self.assertTrue(self.app._render_event.is_set())

    def test_manual_hold(self):
        self.flags = [True, True, False]
        self.app._selected = 1
        self.app._manual_until = 100.0
        self.app._tick_selection(90.0, 90.0)
        self.assertEqual(self.app._selected, 1)
        self.app._tick_selection(101.0, 101.0)
        # Automatic mode resumes and selected is in the active set.
        self.assertEqual(self.app._selected, 1)

    def test_auto_toggle(self):
        self.app._handle_key("a")
        self.assertFalse(self.app._auto)
        self.app._handle_key("a")
        self.assertTrue(self.app._auto)

    def test_activity_vocabulary(self):
        self.assertEqual(yamd.YAMDApplication._unified_log_state(
            "strata", "GENERATING"), "BUSY")
        self.assertEqual(yamd.YAMDApplication._unified_log_state(
            "strata", "READING"), "BUSY")
        self.assertEqual(yamd.YAMDApplication._unified_log_state(
            "strata", "IDLE"), "IDLE")
        self.assertEqual(yamd.YAMDApplication._unified_log_state(
            "halogen", "QUEUED"), "QUEUED")

    def test_select_and_view_keys(self):
        self.app._handle_key("2")
        self.assertEqual(self.app._selected, 1)
        self.app._handle_key("t")
        self.assertEqual(self.app.view_mode, "monitor")
        self.app._handle_key("d")
        self.assertEqual(self.app.view_mode, "detail")
        self.app._handle_key("s")
        self.assertEqual(self.app.view_mode, "split")
        self.app._handle_key("q")
        self.assertFalse(self.app._running)


async def _noop(*args, **kwargs):
    return None


class BrokenCollector(FakeCollector):
    def get_snapshot(self):
        raise RuntimeError("boom")


class IsolationTests(unittest.TestCase):
    def test_bad_target_does_not_break_views(self):
        bad = runtime("strata", strata_snapshot(), name="bad")
        bad.collector = BrokenCollector(strata_snapshot())
        good = runtime("halogen", halogen_snapshot(), name="good")
        app = yamd.YAMDApplication(specs=[])
        app._runtimes = [bad, good]

        views = app._views()
        self.assertEqual(views[0].badge, "OFFLINE")
        self.assertIn("adapter error", views[0].error)
        self.assertEqual(views[1].badge, "IDLE")

        # The frame still renders for the healthy target.
        layout = app._layout_for(80, 24)
        buf = io.StringIO()
        Console(file=buf, width=80, height=24, force_terminal=False,
                color_system=None).print(layout)
        self.assertIn("good", buf.getvalue())


class RenderCadenceTests(unittest.TestCase):
    def test_blink_controls_idle_rate(self):
        off = yamd.YAMDApplication(specs=[], blink=False)
        self.assertEqual(off.render_interval, 1.0)
        self.assertEqual(off.web_fps, 1)
        on = yamd.YAMDApplication(specs=[], blink=True)
        self.assertEqual(on.render_interval, 0.5)
        self.assertEqual(on.web_fps, 2)

    def test_key_wakes_renderer(self):
        app = yamd.YAMDApplication(specs=[])
        app._runtimes = [runtime("halogen", halogen_snapshot(), name="t")]
        app._render_event.clear()
        app._handle_key("m")
        self.assertTrue(app._render_event.is_set())


class PrepareTargetsTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_detection_runs_concurrently(self):
        in_flight = 0
        peak = 0

        async def fake_detect(spec):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return "halogen"

        specs = [yamd.TargetSpec(name=f"t{i}", kind="auto",
                                 url=f"http://127.0.0.1:{9000 + i}")
                 for i in range(3)]
        app = yamd.YAMDApplication(specs=specs)
        original = yamd.detect_kind
        yamd.detect_kind = fake_detect
        try:
            await app._prepare_targets()
        finally:
            yamd.detect_kind = original
        self.assertEqual(peak, 3)
        self.assertEqual([rt.spec.kind for rt in app._runtimes],
                         ["halogen"] * 3)
        self.assertTrue(all(rt.auto for rt in app._runtimes))
        self.assertTrue(all(rt.auto_confident for rt in app._runtimes))

    async def test_unconfident_auto_target_is_rechecked(self):
        kinds = iter([None, "strata"])

        async def fake_detect(spec):
            return next(kinds)

        spec = yamd.TargetSpec(name="t", kind="auto",
                               url="http://127.0.0.1:9000")
        app = yamd.YAMDApplication(specs=[spec])
        original_detect = yamd.detect_kind
        original_make = app._make_collector

        def fake_make(spec):
            snapshot = (strata_snapshot() if spec.kind == "strata"
                        else halogen_snapshot())
            collector = FakeCollector(snapshot, connected=True)
            collector.start = _noop
            collector.stop = _noop
            return collector, False

        yamd.detect_kind = fake_detect
        app._make_collector = fake_make
        try:
            await app._prepare_targets()
            self.assertEqual(app._runtimes[0].spec.kind, "halogen")
            self.assertFalse(app._runtimes[0].auto_confident)
            await app._recheck_auto_targets()
        finally:
            yamd.detect_kind = original_detect
            app._make_collector = original_make
        self.assertEqual(app._runtimes[0].spec.kind, "strata")
        self.assertTrue(app._runtimes[0].rechecked)


if __name__ == "__main__":
    unittest.main()
