"""Integration tests: mock Strata/Halogen HTTP servers + real collectors.

Run with:  python -m unittest discover -s tests -v
"""

import asyncio
import unittest

from aiohttp import web

import yamd


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runners = []
        self.halogen_n = 0
        self.strata_rows = [{
            "id": f"r{i}",
            "time": 2000.0 - i,
            "prompt_tokens": 1000 + i,
            "reused": 800,
            "output_tokens": 120 if i == 0 else 10,
            "decode_tok_s": 24.5 if i == 0 else 20.0,
            "prompt_ms": 200.0,
            "finish": "stop",
        } for i in range(20)]
        self.strata_port = await self._serve({
            "/health": self._strata_health,
            "/metrics": self._strata_metrics,
            "/v1/status": self._strata_status,
        })
        self.halogen_port = await self._serve({
            "/health": self._halogen_health,
            "/metrics": self._halogen_metrics,
            "/cache": self._halogen_cache,
        })

    async def asyncTearDown(self):
        for runner in self.runners:
            await runner.cleanup()

    async def _serve(self, routes) -> int:
        app = web.Application()
        for path, handler in routes.items():
            app.router.add_get(path, handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return site._server.sockets[0].getsockname()[1]

    # --- mock routes ------------------------------------------------------

    async def _strata_health(self, request):
        return web.json_response({"loaded": True, "model": "mock-strata"})

    async def _strata_metrics(self, request):
        rows = (self.strata_rows if request.query.get("requests") == "all"
                else self.strata_rows[:12])
        return web.json_response({
            "engine": {
                "model": "mock-strata-70b", "version": "1.2.0",
                "max_context": 32768, "kv": "fp16",
                "spec": 8, "mtp_max": 6, "lookup": 3,
                "pool_workers": 15, "drafting": "dflash2",
            },
            "live": {
                "state": "generating", "queued": 0, "phase": "answering",
                "prompt_tokens": 1000, "prompt_read": 1000,
                "prompt_total": 1000, "generated": 120, "max_tokens": 512,
                "elapsed_s": 5.2, "tok_s": 24.5, "tok_s_mean": 20.0,
                "prefill_tok_s_mean": 640.0,
            },
            "hardware": {"gpu_util": 88.0, "gpu_temp": 72},
            "hardware_static": {"gpu_name": "Mock GPU", "gpu_count": 1},
            "requests": rows,
            "totals": {"requests": len(self.strata_rows),
                       "prompt_tokens": 5000,
                       "reused": 4000, "output_tokens": 500},
            "requests_kept": len(self.strata_rows),
            "history": {
                "cpu": [float(i) for i in range(60)],
                "gpu_util": [50.0] * 60,
            },
        })

    async def _strata_status(self, request):
        return web.json_response({
            "service": "strata", "uptime_s": 3600,
            "last_timings": {"draft_n": 100, "draft_n_accepted": 70},
            "concurrency": {"serving": 1},
        })

    async def _halogen_health(self, request):
        return web.json_response({
            "model": "mock-halogen-27b",
            "version": {"api": "2.0.0", "engine": "mock"},
            "in_flight": 2, "queued": 1, "context": 32768, "slot_ctx": 8192,
            "slots": 4, "kv_cache": "fp8",
            "drafter_default": "mtp", "drafter_weights_loaded": True,
            "prompt_lookup": {"ngram": 3, "chain": 2},
            "prompt_cache": {"enabled": True, "mode": 1, "cap_mb": 1024},
            "structured_output": {"enabled": True},
            "vision": {"enabled": False},
            "capability_probe": "ok",
        })

    async def _halogen_metrics(self, request):
        self.halogen_n += 1
        n = self.halogen_n
        body = "\n".join([
            f"llamacpp:prompt_tokens_total {1000 + 400 * n}",
            f"llamacpp:prompt_seconds_total {2.0 + 0.4 * n:.2f}",
            f"llamacpp:tokens_predicted_total {200 + 100 * n}",
            f"llamacpp:tokens_predicted_seconds_total {4.0 + 2.0 * n:.2f}",
            "llamacpp:requests_processing 2",
            "llamacpp:requests_deferred 1",
            "llamacpp:kv_cache_usage_ratio 0.42",
            "halogen:engine_up 1",
            f"halogen:requests_total {5 + n}",
            "halogen:busy_for_seconds 12.4",
            f"halogen:prompt_tokens_cached_total {700 + 300 * n}",
            f"halogen:draft_tokens_total {100 + 20 * n}",
            f"halogen:draft_tokens_accepted_total {71 + 14 * n}",
            "halogen:cache_entries 3",
            "halogen:cache_bytes 1024",
            "halogen:kv_pool_positions 100000",
        ])
        return web.Response(text=body, content_type="text/plain")

    async def _halogen_cache(self, request):
        return web.json_response({
            "entries": 3, "max_entries": 32, "bytes": 1024,
            "hits": 60, "misses": 40, "stores": 5, "evicted": 1,
            "hit_rate": 0.6, "token_hit_rate": 0.7,
            "prompt_tokens_saved": 700, "stale_s": 0.2,
            "pool": {"positions": 100000, "used": 42000,
                     "usage_ratio": 0.42, "waiting_for_room": 0,
                     "waiting_s": 0.0},
            "disk": {"on": True, "bytes": 2048, "budget_bytes": 4096,
                     "hits": 2},
        })

    # --- tests ------------------------------------------------------------

    async def test_auto_detection(self):
        strata = yamd.TargetSpec(
            name="s", kind="auto",
            url=f"http://127.0.0.1:{self.strata_port}").normalize()
        halogen = yamd.TargetSpec(
            name="h", kind="auto",
            url=f"http://127.0.0.1:{self.halogen_port}").normalize()
        self.assertEqual(await yamd.detect_kind(strata), "strata")
        self.assertEqual(await yamd.detect_kind(halogen), "halogen")

    async def test_strata_collector_through_view(self):
        spec = yamd.TargetSpec(
            name="s", kind="strata",
            url=f"http://127.0.0.1:{self.strata_port}").normalize()
        collector = yamd.StrataCollector(
            server_url=spec.url, poll_interval=0.1)
        await collector.start()
        try:
            for _ in range(50):
                if collector.is_connected():
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(collector.is_connected())
            snapshot = collector.get_snapshot()
            self.assertEqual(snapshot.drafting, "dflash2")
            self.assertEqual(snapshot.drafting_note, "")
            self.assertGreaterEqual(len(snapshot.client_history["cpu"]), 60)
            self.assertEqual(snapshot.client_history["cpu"][-1], 59.0)
            self.assertEqual(len(snapshot.client_history["gpu_util"]),
                             len(snapshot.client_history["cpu"]))
            view = yamd.make_view(yamd.TargetRuntime(
                spec=spec, collector=collector))
            self.assertEqual(view.badge, "BUSY")
            self.assertEqual(view.model, "mock-strata-70b")
            self.assertEqual(view.digest["output_tokens"], 120)
            self.assertEqual(view.digest["cached_tokens"], 800)
        finally:
            await collector.stop()

    async def test_strata_seeds_full_ring_then_polls_default(self):
        spec = yamd.TargetSpec(
            name="s", kind="strata",
            url=f"http://127.0.0.1:{self.strata_port}").normalize()
        collector = yamd.StrataCollector(
            server_url=spec.url, poll_interval=0.1,
            req_rows=12, seed_all=True)
        await collector.start()
        try:
            for _ in range(60):
                snapshot = collector.get_snapshot()
                if collector.is_connected() and len(snapshot.requests) >= 20:
                    break
                await asyncio.sleep(0.1)
            snapshot = collector.get_snapshot()
            self.assertEqual(len(snapshot.requests), 20)
            self.assertEqual(snapshot.requests_kept, 20)
            self.assertEqual([row["id"] for row in snapshot.requests[:3]],
                             ["r0", "r1", "r2"])

            # Later polls fetch only the server's 12-row default; merging by
            # id must keep the seeded rows instead of shrinking the ring.
            await asyncio.sleep(0.35)
            self.assertEqual(len(collector.get_snapshot().requests), 20)
        finally:
            await collector.stop()

    async def test_strata_without_seed_keeps_default_window(self):
        spec = yamd.TargetSpec(
            name="s", kind="strata",
            url=f"http://127.0.0.1:{self.strata_port}").normalize()
        collector = yamd.StrataCollector(
            server_url=spec.url, poll_interval=0.1,
            req_rows=12, seed_all=False)
        await collector.start()
        try:
            for _ in range(60):
                snapshot = collector.get_snapshot()
                if collector.is_connected() and snapshot.requests:
                    break
                await asyncio.sleep(0.1)
            snapshot = collector.get_snapshot()
            self.assertEqual(len(snapshot.requests), 12)
            self.assertEqual(snapshot.requests_kept, 20)
        finally:
            await collector.stop()

    async def test_halogen_collector_through_view(self):
        spec = yamd.TargetSpec(
            name="h", kind="halogen",
            url=f"http://127.0.0.1:{self.halogen_port}").normalize()
        collector = yamd.HalogenCollector(
            server_url=spec.url, poll_interval=0.1, sample_host=False)
        await collector.start()
        try:
            for _ in range(60):
                snapshot = collector.get_snapshot()
                if collector.is_connected() and snapshot.requests:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(collector.is_connected())
            view = yamd.make_view(yamd.TargetRuntime(
                spec=spec, collector=collector))
            self.assertEqual(view.badge, "QUEUED")
            self.assertEqual(view.model, "mock-halogen-27b")
            self.assertTrue(view.digest)
            self.assertEqual(view.digest["output_tokens"], 100)
            self.assertEqual(view.digest["cached_tokens"], 300)
        finally:
            await collector.stop()


if __name__ == "__main__":
    unittest.main()
