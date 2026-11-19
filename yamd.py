"""
YAMD - Yet Another Multi Dashboard (v2.2.0)

A real-time terminal UI that monitors several Strata and Halogen instances at
once. This file is the single source for both collectors, their detail panels
(the former YASD and YAHD dashboards) and the multi-target frontend.

Copyright 2026 Antani Technologies BV

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import argparse
import asyncio
import io
import json
import math
import os
import select
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Shared endpoints and constants
# ---------------------------------------------------------------------------

# Samples kept per graph series. At the 1 s poll interval this is a two-minute
# ring; each card draws its newest 2 * width samples, one per braille column.
HISTORY_MAX = 120

# Fallback graph width in braille cells; the panels compute a real width from
# the terminal.
HISTORY_WIDTH = 24

# One hue per graph card, in card order, so no two lines read as the same
# series. Warm hues carry the thermal/power or saturation cards, cool hues the
# capacity ones; Rich folds them down to the terminal's own palette when
# truecolor is not available. Strata and Halogen share the palette.
GRAPH_COLORS = (
    "#4ec9b0",
    "#5aa9e6",
    "#9d8df1",
    "#f2777a",
    "#f5a25d",
    "#f7d154",
    "#a9e34b",
    "#6fd86f",
    "#4fbfe0",
    "#c79be0",
)

# Consecutive failed poll cycles required before reporting OFFLINE. Strata
# polls at 500 ms (~2 s of unreachability), Halogen at 1 s (~4 s).
OFFLINE_THRESHOLD = 4

# Body split: the right column carries the request table and the graphs, so it
# needs more width than the label/value stack on the left. Both detail bodies
# use the same ratio.
LEFT_RATIO, RIGHT_RATIO = 3, 4

# Metrics is the only flex panel on the left; the other panels are title +
# 3 content rows + border. Metrics always draws METRIC_ROWS rows, and the
# METRIC_SPACERS blank separators are the first to be dropped on a short
# terminal (Windows' default console is 80x25).
PANEL_ROWS    = 5
METRIC_ROWS   = 12
METRIC_SPACERS = 5

# Returned by the fetchers instead of a body when the server answers 401.
# Distinct from None ("not fetched"), so the poll loop can tell a bad key
# apart from an outage.
_AUTH = object()


class ApiKeyRejected(Exception):
    """The server is reachable but rejected our Authorization header."""


class MetricsUnavailable(Exception):
    """The server is reachable but /metrics did not answer. Not an outage."""


def _col_widths(term_width: int) -> tuple[int, int]:
    """Inner widths of the left and right body columns, borders removed.

    Rich's row splitter distributes ``term_width - 1`` columns over the
    ratios (one column is left over), so mirror that here instead of
    guessing: the panels then never overflow their column and nothing is
    truncated with an ellipsis.
    """
    avail = max(4, term_width - 1)
    left = int(avail * LEFT_RATIO / (LEFT_RATIO + RIGHT_RATIO))
    right = avail - left
    return max(12, left - 2), max(12, right - 2)


# ---------------------------------------------------------------------------
# Shared formatting and braille helpers
# ---------------------------------------------------------------------------

def _num(n, d: int = 0) -> str:
    """Thousands separators, em-dash for missing."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(n) or math.isinf(n):
        return "—"
    return f"{n:,.{d}f}"


def _kfmt(n) -> str:
    """Short form for token counts: 1,234 -> 1.2k, 1,234,567 -> 1.2M."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(n) or math.isinf(n):
        return "—"
    if n < 1000:
        return f"{n:,.0f}"
    if n < 1_000_000:
        return f"{n / 1000:.{1 if n < 10000 else 0}f}k"
    return f"{n / 1_000_000:.1f}M"


def _gb(b, d: int = 1) -> str:
    """Binary gigabytes, as Windows shows them."""
    if b is None:
        return "—"
    try:
        return f"{float(b) / 1073741824:,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def _secs(s) -> str:
    """Seconds with one decimal, em-dash when absent."""
    if s is None:
        return "—"
    try:
        return f"{float(s):.1f} s"
    except (TypeError, ValueError):
        return "—"


# Braille cell (U+2800 + bits) as btop uses it: two virtual columns and four
# dot rows per character. Bits are per dot, filled from the bottom up, so a
# graph of `width` cells plots 2 * width samples and each sample gets four
# vertical steps instead of one block.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40),   # left  column, bottom -> top
                 (0x08, 0x10, 0x20, 0x80))   # right column, bottom -> top


def _braille_cell(left: int, right: int) -> str:
    """One cell with `left`/`right` dot rows lit from the bottom up.

    An unlit cell is a space, as in btop's own symbol table: a series of all
    zeros then renders as blank space instead of 21 empty braille glyphs, which
    fonts without a Braille Patterns face would draw as tofu boxes.
    """
    bits = 0
    for column, level in enumerate((left, right)):
        for row in range(level):
            bits |= _BRAILLE_DOTS[column][row]
    return chr(0x2800 + bits) if bits else " "


# All 25 fill combinations, indexed as btop does: left * 5 + right, where each
# level is 0-4 lit dot rows out of the cell's four.
_BRAILLE_UP = tuple(_braille_cell(left, right)
                    for left in range(5) for right in range(5))


def _braille_level(value: float, top: float) -> int:
    """How many of a cell's four dot rows one sample fills (btop's mapping).

    btop's slight upward bias and C++-style rounding are kept so the curves
    match what btop shows; on top of that a sample that is not zero always
    lights one row, the way the old block sparkline drew a ▁ tick, so a busy
    card never reads as "no data".
    """
    if value <= 0:
        return 0
    scaled = min(1.0, value / top) * 4.0 + 0.3
    return max(1, min(4, int(scaled + 0.5)))


def _braille_graph(values, fixed_max: Optional[float] = None,
                   width: int = HISTORY_WIDTH) -> str:
    """Recent samples -> braille: the terminal equivalent of a sparkline.

    Braille cells carry two samples side by side, so a `width`-wide card plots
    the newest 2 * width samples with one sample per virtual column, and the
    curve scrolls one column per poll. Older samples stay in the ring for
    wider cards but are not bucketed into narrow ones (that made a 10-cell
    graph advance only once every few seconds). No resampling is done: a short
    ring is left-padded while it fills, so the curve scrolls one column per
    poll without dots changing shape. Strata now keeps the same 120-sample
    client ring as Halogen, so both graphs use the same time window.
    """
    vals = []
    for v in (values or []):
        try:
            f = 0.0 if v is None else float(v)
        except (TypeError, ValueError):
            f = 0.0
        vals.append(0.0 if (math.isnan(f) or math.isinf(f)) else f)
    if width < 1:
        return ""
    if not vals:
        return " " * width                 # no history yet: keep the column full
    top = float(fixed_max or 0.0) or (max(vals) or 1e-9)
    if top <= 0:
        top = 1e-9

    cols = width * 2                       # virtual samples per cell row
    if len(vals) >= cols:
        series = vals[-cols:]              # newest samples, one per column
    else:
        # Still warming up: keep the newest samples flush right.
        series = [0.0] * (cols - len(vals)) + vals

    out = []
    for i in range(0, cols, 2):
        left = _braille_level(series[i], top)
        right = _braille_level(series[i + 1], top)
        out.append(_BRAILLE_UP[left * 5 + right])
    return "".join(out)


# ---------------------------------------------------------------------------
# Shared panel helpers
# ---------------------------------------------------------------------------

def _left_panel_geometry(term_width: int) -> tuple[int, bool, bool, int]:
    """(grid width, narrow, tiny, label width) for the left detail column.

    The panel border is already excluded from ``_col_widths``, but its
    one-column padding on each side is not, so the grid itself gets two
    columns less. An 80-column terminal leaves 31 characters inside the left
    panel, so narrow terminals use abbreviated labels and tiny ones drop the
    least informative digits.
    """
    inner = _col_widths(term_width)[0]
    narrow = inner < 40
    return inner - 2, narrow, inner < 30, (12 if narrow else 15)


def _detail_table(label_w: int, spacers: bool, narrow: bool):
    """Label/value grid plus the narrow-label and spacer closures."""
    def label(wide: str, short: str) -> str:
        return short if narrow else wide

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=label_w)
    table.add_column(style="bold white")

    def gap() -> None:
        if spacers:
            table.add_row("")

    return table, label, gap


def _usage_bar(ratio: float, grid_w: int, label_w: int, tiny: bool,
               narrow: bool, *, blink: bool = False,
               available: bool = True) -> str:
    """Render the context/KV-pool bar as Rich markup.

    The bar fills whatever is left of the label column so it never wraps:
    brackets (2) + space + "99.5%" (5) = 8 characters. The ratio is capped at
    99.5 % so the gauge never reads exactly 100 %, and the green/orange/red
    thresholds are the same for both dashboards. ``blink`` is only used by
    Strata's >95 % warning; Halogen's KV pool is expected to fill.
    """
    ratio = min(max(ratio, 0.0), 1.0)
    if ratio >= 1.0:
        ratio = 0.995
    bar_w = max(3 if tiny else (4 if narrow else 8), grid_w - label_w - 8)
    filled = int(ratio * bar_w)
    pct = f"{ratio:.0%}" if tiny else f"{ratio:.1%}"

    if not available:
        return "[dim]" + "░" * bar_w + " —[/dim]"

    pct_value = round(ratio * 100, 1)
    if pct_value > 85:
        style = "red"
    elif pct_value >= 70:
        style = "dark_orange"
    else:
        style = "green"

    if blink and pct_value > 95 and int(time.monotonic()) % 2 == 0:
        return f"[dim][{'░' * bar_w}] {pct}[/dim]"
    bar = f'[{style}]{"█" * filled}{"░" * (bar_w - filled)}[/{style}]'
    return f"[{bar}] {pct}"


def _draft_style(acc: float) -> str:
    """Colour for draft acceptance: red <50 %, orange <60 %, green otherwise."""
    if acc <= 49.9:
        return "red"
    if acc <= 59.9:
        return "dark_orange"
    return "green"


def _speed_text(value: Optional[float], active_style: str,
                unit_style: str) -> Text:
    """One throughput value with its unit, dimmed while zero/missing."""
    if value is None:
        return Text("— tok/s", style="dim")
    text = Text(f"{value:.1f}", style=active_style if value > 0 else "dim")
    text.append(" tok/s", style=unit_style if value > 0 else "dim")
    return text


def _make_performance_panel(*, prefill: Optional[float], decode: Optional[float],
                            label_w: int, lab_l: str, lab_r: str,
                            sub_l: str, sub_r: str) -> Panel:
    """Shared Performance panel body: label | divider | value, three rows."""
    perf_table = Table.grid(expand=True)
    perf_table.add_column(style="cyan", width=label_w)
    perf_table.add_column(style="dim", width=1)
    perf_table.add_column(style="bold white")

    perf_table.add_row(lab_l, " ", lab_r)
    perf_table.add_row(_speed_text(prefill, "bold magenta", "dim magenta"), " ",
                       _speed_text(decode, "bold green", "dim green"))
    perf_table.add_row(sub_l, " ", sub_r)

    return Panel(perf_table, title="[bold]Performance[/bold]",
                 border_style="green", padding=(0, 0))


def _card_geometry(inner: int) -> tuple[bool, int, int, int]:
    """(narrow, label width, value width, graph width) for a card list."""
    narrow = inner < 40
    label_w = 12 if narrow else 13
    value_w = 18
    graph_w = max(4, inner - label_w - 3 - value_w)
    value_w = max(6, inner - label_w - 3 - graph_w)
    return narrow, label_w, value_w, graph_w


def _make_card_factory(series: dict, label_w: int, value_w: int, graph_w: int):
    """Return the per-card builder shared by Hardware and Cache & Pool."""
    colors = iter(GRAPH_COLORS)

    def card(label: str, value: str, sub: str, series_key: Optional[str],
             fixed_max: Optional[float], style: str) -> tuple[Text, Text, Text]:
        graph = (_braille_graph(series.get(series_key, []), fixed_max, graph_w)
                 if series_key else " " * graph_w)
        left = Text(label[:label_w], style="cyan")
        mid = Text(value, style=style)
        if sub:
            mid.append("  " + sub, style="dim")
        if len(mid) > value_w:          # keep one line per card
            mid = Text(str(mid)[:value_w - 1] + "…", style=style)
        return left, mid, Text(graph, style=next(colors))

    return card


def _render_card_panel(rows: list[tuple[Text, Text, Text]], inner: int,
                       graph_w: int, title: str) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=13, justify="left")
    table.add_column(style="", width=max(4, inner - 13 - graph_w - 2),
                     justify="left")
    # No column style: the graph cells bring their own per-card colour.
    table.add_column(style="", width=graph_w, justify="left")
    for left, mid, graph in rows:
        table.add_row(left, mid, graph)
    return Panel(table, title=title, border_style="yellow")


def _request_row_key(row: dict) -> str:
    """Stable key for one request row, used to anchor a scrolled table.

    Strata publishes an `id`; Halogen's completion windows are derived from
    counter deltas, so their timestamp plus token totals is the best identity
    available.
    """
    row_id = row.get("id")
    if row_id:
        return str(row_id)
    return (f"{row.get('time')}:{row.get('prompt_tokens')}:"
            f"{row.get('output_tokens')}")


def _request_offset(requests: list[dict], rows: int,
                    anchor: Optional[str]) -> int:
    """Resolve a scrolled table's first row, newest-first.

    The anchor is the key of the row that was at the top of the panel. Keeping
    that row fixed is what stops the view from jumping when a new completion
    arrives at the head of the ring. If the row was pushed out of the retained
    ring, fall back to the oldest page.
    """
    if not requests:
        return 0
    max_start = max(0, len(requests) - max(1, rows))
    if not anchor:
        return 0
    for index, row in enumerate(requests):
        if _request_row_key(row) == anchor:
            return min(index, max_start)
    return max_start


def _request_table(inner: int, cols: list[tuple[str, str]], requests: list[dict],
                   rows: int, *, num_min: int, status_header: str,
                   status, cell, header=None, start: int = 0) -> Table:
    """Shared table skeleton for the Strata and Halogen request panels."""
    num_w = max(num_min, (inner - 8 - 7 - len(cols)) // len(cols))

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8, justify="left")
    table.add_column(style="bold white", width=7, justify="left")
    for _ in cols:
        table.add_column(width=num_w, justify="right")

    def label(name: str, key: str) -> str:
        return header(name, key, num_w) if header else name

    table.add_row("Time", status_header,
                  *(label(name, key) for name, key in cols))
    for row in requests[start:start + rows]:
        when = (datetime.fromtimestamp(row["time"]).strftime("%H:%M:%S")
                if row.get("time") else "—")
        table.add_row(when, status(row), *(cell(row, key) for _, key in cols))
    return table


# ---------------------------------------------------------------------------
# Endpoints and polling
# ---------------------------------------------------------------------------

# /metrics carries everything (engine, live, requests, totals, hardware,
# history). /health is the only Strata route that skips _authorized(), so it
# doubles as the connectivity probe: it answers even when an API key is set.
# /v1/status adds the draft-acceptance counters (last_timings.draft_n*).
# Strata publishes at 500 ms; its graph ring is extended once per second so
# the hardware cards share Halogen's 120-second window.
STRATA_POLL_INTERVAL = 0.5

# How many request rows /metrics returns without ?requests=all.
DEFAULT_REQ_ROWS = 12

# Locale-independent month abbreviations, so the Session "since" stamp reads
# the same on every platform (Windows strftime %b follows the active locale).
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# Data model — one flat snapshot. Strata's concurrency is 1 behind a FIFO, so
# there is no per-slot structure to carry.
# ---------------------------------------------------------------------------

@dataclass
class StrataSnapshot:
    """Immutable snapshot of Strata's /metrics + /health + /v1/status at one instant."""
    timestamp: float

    # connectivity
    loaded: bool = True

    # state — `state` and `queued` are the only fields never null
    state: str = "idle"          # idle | reading | generating | unloaded
    queued: int = 0
    phase: Optional[str] = None

    # running request (null while idle — "no request yet" is not "zero tokens")
    prompt_tokens:    Optional[int] = None
    prompt_read:      Optional[int] = None
    prompt_total:     Optional[int] = None
    generated:        Optional[int] = None
    max_tokens:       Optional[int] = None
    elapsed_s:        Optional[float] = None

    # speeds — computed by the server, never re-derived here
    tok_s:              Optional[float] = None
    tok_s_mean:         Optional[float] = None
    prefill_tok_s_mean: Optional[float] = None

    # context
    max_context: int = 0
    ctx_used: int = 0            # prompt+generated, with last-request fallback

    # hardware
    gpu_util:         Optional[float] = None
    gpu_mem_used:     Optional[int] = None
    gpu_mem_total:    Optional[int] = None
    gpu_temp:         Optional[int] = None
    gpu_power:        Optional[float] = None
    gpu_power_limit:  Optional[float] = None
    gpu_pcie_gen:     Optional[int] = None
    gpu_pcie_gen_max: Optional[int] = None
    gpu_pcie_width:   Optional[int] = None
    gpu_pcie_rx_mb:   Optional[float] = None
    gpu_pcie_tx_mb:   Optional[float] = None
    cpu:              Optional[float] = None
    ram_used:         Optional[int] = None
    ram_total:        Optional[int] = None
    disk_read_mb:     Optional[float] = None
    disk_write_mb:    Optional[float] = None
    gpus: list[dict] = None      # type: ignore[assignment]

    # engine facts
    model: str = ""
    engine_version: str = ""
    kv: str = ""
    kv_resident: int = 0
    expert_slots: int = 0
    expert_cache_mib: int = 0
    expert_slots_primary: int = 0
    expert_cache_primary_mib: int = 0
    spec: int = 0
    mtp_max: int = 0
    drafting: str = ""           # mtp | dflash | dflash2 | off (new Strata)
    drafting_note: str = ""
    lookup: int = 0
    pool_workers: int = 0
    conversation_cache_slots: int = 0
    images: bool = False
    cvec: Optional[str] = None

    # static hardware identity
    gpu_name: Optional[str] = None
    gpu_count: int = 1
    cpu_name: Optional[str] = None
    cores: int = 0
    threads: int = 0
    psutil: bool = False

    # session totals, request ring, server-side history series
    totals: dict = None          # type: ignore[assignment]
    requests: list[dict] = None  # type: ignore[assignment]
    requests_kept: int = 0
    history: dict[str, list[float]] = None  # type: ignore[assignment]
    # 120-sample ring seeded from the server's 60-sample history and extended
    # once per second; graph cards use this so they match Halogen's window.
    client_history: dict[str, list[float]] = None  # type: ignore[assignment]

    # draft acceptance, from /v1/status.last_timings
    draft_n: int = 0
    draft_n_accepted: int = 0
    draft_valid: bool = True

    # service facts, from /v1/status
    service: str = ""
    uptime_s: Optional[float] = None
    concurrency_serving: int = 1

    def __post_init__(self):
        if self.totals is None:
            self.totals = {}
        if self.requests is None:
            self.requests = []
        if self.history is None:
            self.history = {}
        if self.client_history is None:
            self.client_history = {}
        if self.gpus is None:
            self.gpus = []


# ---------------------------------------------------------------------------
# Badge — Strata's own vocabulary plus SLEEPING for an unloaded model
# ---------------------------------------------------------------------------

def _strata_state_badge(snapshot: StrataSnapshot) -> str:
    """Strata badge for a snapshot.

    live.state is idle | reading | generating | unloaded, and queued > 0
    overrides it, exactly as serve/web/app.js picks the lit badge.
    """
    if not snapshot.loaded:
        return "SLEEPING"
    if snapshot.queued > 0:
        return "QUEUED"
    return {
        "reading":    "READING",
        "generating": "GENERATING",
        "unloaded":   "SLEEPING",
    }.get(snapshot.state, "IDLE")


# ---------------------------------------------------------------------------
# Collector base — shared lifecycle, HTTP plumbing and state log
# ---------------------------------------------------------------------------

class _CollectorBase:
    """Common plumbing for the Strata and Halogen collectors.

    Both poll a server in a background asyncio task, share the same hardened
    aiohttp session, the same offline-failure counter and the same state log;
    only the endpoint fan-out and the snapshot parsing differ.
    """

    HEALTH_ENDPOINT = "/health"
    METRICS_ENDPOINT = "/metrics"
    OFFLINE_THRESHOLD = OFFLINE_THRESHOLD

    def _init_base(self, server_url: str, poll_interval: float,
                   api_key: Optional[str]) -> None:
        self.server_url = server_url
        self.poll_interval = poll_interval
        self.api_key = api_key

        self._running = False
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._session: Optional[aiohttp.ClientSession] = None

        self._connected = False
        self._ever_connected = False
        self._last_error: Optional[str] = None
        self._consecutive_failures: int = 0
        self._success_after_offline: int = 0
        self._key_rejected = False

        self._state_log: list[tuple[float, str]] = []
        self._log_max = 10
        self._last_state = ""       # empty so the first IDLE is recorded
        self._last_health: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._session = self._make_session()
        self._before_poll_start()
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _before_poll_start(self) -> None:
        """Hook for collectors with extra background tasks."""

    def _background_tasks(self) -> tuple[Optional[asyncio.Task[None]], ...]:
        return (self._poll_task,)

    async def stop(self) -> None:
        self._running = False
        await self._cancel_tasks(*self._background_tasks())
        if self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    async def _cancel_tasks(*tasks: Optional[asyncio.Task[None]]) -> None:
        pending = [t for t in tasks if t is not None and not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _make_session() -> aiohttp.ClientSession:
        """Create a session with a hardened connector.

        * limit_per_host keeps connection churn bounded.
        * enable_cleanup_closed proactively discards half-closed sockets,
          which servers without keep-alive produce when they drop idle
          connections (a classic source of spurious resets).
        """
        connector = aiohttp.TCPConnector(
            limit_per_host=8,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        return aiohttp.ClientSession(connector=connector)

    async def _recreate_session(self) -> None:
        """Discard all pooled connections and start fresh.

        Used after repeated connection failures — stale/poisoned keep-alive
        sockets would otherwise keep failing even though the server is fine.
        """
        if self._session:
            await self._session.close()
        self._session = self._make_session()

    # ------------------------------------------------------------------
    # aiohttp helpers
    # ------------------------------------------------------------------

    async def _fetch_body(
        self, session: aiohttp.ClientSession, endpoint: str,
        auth: bool = True,
    ) -> str:
        """Fetch a response body from *endpoint*.

        *auth* is False only for /health, the one route both servers answer
        without checking the API key — sending a bad key there would turn a
        key error into a false OFFLINE.
        """
        # Generous timeouts: a prefilling engine can legitimately take seconds
        # to answer. A short total timeout made healthy servers look offline.
        timeout = aiohttp.ClientTimeout(total=6.0, connect=2.0, sock_read=4.0)
        headers = {"Authorization": f"Bearer {self.api_key}"} \
            if (auth and self.api_key) else None
        async with session.get(
            f"{self.server_url}{endpoint}", timeout=timeout, headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def _fetch_json(
        self, session: aiohttp.ClientSession, endpoint: str,
        auth: bool = True,
    ) -> Optional[dict]:
        return json.loads(await self._fetch_body(session, endpoint, auth=auth))

    async def _fetch_text(
        self, session: aiohttp.ClientSession, endpoint: str,
    ) -> str:
        return await self._fetch_body(session, endpoint)

    async def _fetch_optional(
        self, session: aiohttp.ClientSession, endpoint: str,
        *, text: bool = False,
    ):
        """Fetch an optional endpoint; 401 -> _AUTH, other errors -> None."""
        try:
            if text:
                return await self._fetch_text(session, endpoint)
            return await self._fetch_json(session, endpoint)
        except aiohttp.ClientResponseError as exc:
            return _AUTH if exc.status == 401 else None
        except Exception:
            return None

    async def _fetch_health(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """Fetch /health without an Authorization header.

        Both servers keep /health open even when an API key is set; sending a
        bad key there would turn a key error into a false OFFLINE.
        """
        return await self._fetch_json(session, self.HEALTH_ENDPOINT, auth=False)

    # ------------------------------------------------------------------
    # Connectivity and state log
    # ------------------------------------------------------------------

    async def _handle_poll_error(self, exc: Exception) -> None:
        """Apply the shared poll-loop error policy to one failed cycle."""
        if isinstance(exc, asyncio.TimeoutError):
            # Checked before ClientConnectionError: aiohttp.ServerTimeoutError
            # inherits from both.
            self._register_failure("Request timeout")
        elif isinstance(exc, ApiKeyRejected):
            # /health answered, so the server is provably reachable: stay
            # ONLINE and blame the key, not the connection.
            self._key_rejected = True
            self._connected = True
            self._ever_connected = True
            self._consecutive_failures = 0
            self._last_error = "API key rejected (HTTP 401)"
        elif isinstance(exc, MetricsUnavailable):
            # /health answered but /metrics did not: reachable, no data.
            self._connected = True
            self._ever_connected = True
            self._consecutive_failures = 0
            self._last_error = "Metrics endpoint unavailable"
        elif isinstance(exc, aiohttp.ClientResponseError):
            self._register_failure(f"HTTP {exc.status}")
        elif isinstance(exc, aiohttp.ClientConnectionError):
            self._register_failure(f"Connection error ({type(exc).__name__})")
            # Repeated resets usually mean stale pooled sockets — rebuild the
            # session so the next cycle starts clean.
            if self._consecutive_failures >= 2:
                await self._recreate_session()
        elif isinstance(exc, (json.JSONDecodeError, ValueError)):
            # Malformed / truncated body: not a connectivity problem. Keep
            # the last good snapshot and retry next cycle.
            self._last_error = f"Bad response from server ({exc})"
            self._consecutive_failures = 0
        else:
            self._register_failure(f"{type(exc).__name__}: {exc}")

    def _register_failure(self, message: str) -> None:
        """Count a failed poll cycle; flip OFFLINE only after a sustained outage."""
        self._consecutive_failures += 1
        self._last_error = message
        if self._consecutive_failures >= self.OFFLINE_THRESHOLD:
            self._connected = False

    def _state_badge(self, snapshot) -> str:
        raise NotImplementedError

    def _update_state(self, snapshot) -> None:
        current_state = self._state_badge(snapshot)
        if current_state != self._last_state:
            self._state_log.append((snapshot.timestamp, current_state))
            if len(self._state_log) > self._log_max:
                self._state_log.pop(0)
            self._last_state = current_state

    # ------------------------------------------------------------------
    # Public accessors (no locks — single-threaded event loop)
    # ------------------------------------------------------------------

    def get_snapshot(self):
        return self._current

    def is_connected(self) -> bool:
        return self._connected

    def is_key_rejected(self) -> bool:
        """True while /metrics answers 401 for our key."""
        return self._key_rejected

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def get_state_log(self) -> list[tuple[float, str]]:
        return list(self._state_log)


# ---------------------------------------------------------------------------
# Strata collector
# ---------------------------------------------------------------------------

class StrataCollector(_CollectorBase):
    """Async background collector for Strata's HTTP interface.

    Replaces threading.Lock + polling Thread with a single asyncio loop.
    Because the event loop is single-threaded, all mutations to shared state
    are inherently serial — no lock is ever needed.
    """

    STATUS_ENDPOINT = "/v1/status"
    _state_badge = staticmethod(_strata_state_badge)

    def __init__(self, server_url: str = "http://127.0.0.1:8080",
                 poll_interval: float = STRATA_POLL_INTERVAL,
                 api_key: Optional[str] = None,
                 req_rows: Optional[int] = None,
                 seed_all: bool = False):
        self._init_base(server_url, poll_interval, api_key)
        # ?requests=all asks the server for its whole ring (up to 500 kept)
        # instead of the DEFAULT_REQ_ROWS it sends by default.
        self._all_requests = req_rows is None or req_rows > DEFAULT_REQ_ROWS
        # The server only accepts "all" or its 12-row default; numeric request
        # limits are ignored. With seed_all, fetch the whole ring once and
        # then poll the cheap default while merging new rows into our own ring.
        self._allow_seed = bool(seed_all)
        self._seed_all = self._allow_seed and not self._all_requests
        self._last_fetch_all = False
        self._req_ring: dict[str, dict] = {}
        self._req_ring_max = 500
        self._prev_total: Optional[int] = None

        self._current: StrataSnapshot = StrataSnapshot(timestamp=time.time())
        self._history: list[StrataSnapshot] = []
        self._history_max = 60          # ~30 s of history at 500 ms poll

        # Graph ring: seed from the server's 60-sample history, then append
        # one sample per wall-clock second until it matches Halogen's 120.
        self._graph_ring: dict[str, list[float]] = {}
        self._graph_max = 120
        self._graph_last_sec: Optional[int] = None

        # True while the model is unloaded — the server is reachable, not offline.
        self._server_sleeping = False

        # Raw responses, for --debug
        self._last_metrics: Optional[dict] = None
        self._last_status: Optional[dict] = None

    # ------------------------------------------------------------------
    # Collection loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Async polling loop — runs until *stop()* cancels this task.

        Error-handling policy:
        * Transport errors (timeout / connection reset) count toward the
          OFFLINE threshold (OFFLINE_THRESHOLD consecutive failures).
        * /health.loaded == false means "unloaded" — reachable, so it never
          counts as offline and is shown as SLEEPING instead.
        * A 401 on /metrics while /health answers is a key problem, not an
          outage: connectivity stays ONLINE and the key error is reported.
        * Data errors (bad JSON) keep the last good snapshot and never flip
          connectivity — they are a parsing problem, not an outage.
        """
        while self._running:
            try:
                snapshot = await self._fetch_and_parse()
                self._update_state(snapshot)

                self._current = snapshot
                self._history.append(snapshot)
                if len(self._history) > self._history_max:
                    self._history.pop(0)
                self._consecutive_failures = 0
                self._server_sleeping = not snapshot.loaded
                self._key_rejected = False
                if not self._connected:
                    self._success_after_offline += 1
                    if self._success_after_offline >= 2:
                        self._connected = True
                        self._success_after_offline = 0
                        self._last_error = None
                else:
                    self._last_error = None

            except Exception as exc:
                await self._handle_poll_error(exc)

            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Fetch + parse
    # ------------------------------------------------------------------

    async def _fetch_and_parse(self) -> StrataSnapshot:
        """Fan out /health, /metrics and /v1/status concurrently, then parse."""
        now = time.time()
        snapshot = StrataSnapshot(timestamp=now)

        session = self._session  # guaranteed non-None after start()

        # /health is the primary endpoint — Strata answers it even when an API
        # key is set, so its failure is the only honest signal of an outage.
        # /metrics carries the data (optional: a 401 is handled below), /v1/status
        # only adds draft acceptance.
        health, metrics, status = await asyncio.gather(
            self._fetch_health(session),
            self._fetch_metrics(session),
            self._fetch_status(session),
        )
        self._last_health = health

        if metrics is _AUTH:
            raise ApiKeyRejected("API key rejected (HTTP 401)")
        if metrics is None:
            raise MetricsUnavailable("Metrics endpoint unavailable")
        self._last_metrics = metrics

        engine = metrics.get("engine") or {}
        live   = metrics.get("live") or {}
        hw     = metrics.get("hardware") or {}
        static = metrics.get("hardware_static") or {}
        last   = (metrics.get("requests") or [None])[0]

        # --- connectivity: /health.loaded ----------------------------------
        snapshot.loaded = bool(health.get("loaded", True)) if health else True

        # --- state ----------------------------------------------------------
        # live.state in idle | reading | generating | unloaded; queued > 0
        # overrides it, as the web app does.
        snapshot.state = live.get("state") or "idle"
        snapshot.queued = live.get("queued") or 0
        snapshot.phase = live.get("phase")

        # --- running request (nulls stay nulls) ----------------------------
        snapshot.prompt_tokens = live.get("prompt_tokens")
        snapshot.prompt_read   = live.get("prompt_read")
        snapshot.prompt_total  = live.get("prompt_total")
        snapshot.generated     = live.get("generated")
        snapshot.max_tokens    = live.get("max_tokens")
        snapshot.elapsed_s     = live.get("elapsed_s")

        # --- speeds: read, never re-derived --------------------------------
        snapshot.tok_s              = live.get("tok_s")
        snapshot.tok_s_mean         = live.get("tok_s_mean")
        snapshot.prefill_tok_s_mean = live.get("prefill_tok_s_mean")

        # --- context --------------------------------------------------------
        snapshot.max_context = int(engine.get("max_context") or 0)
        # app.js: the running request while busy, else the last finished one, so
        # the gauge does not drop to 0 between requests.
        if snapshot.state != "idle":
            snapshot.ctx_used = (live.get("prompt_tokens") or 0) + (live.get("generated") or 0)
        elif last:
            snapshot.ctx_used = (last.get("prompt_tokens") or 0) + (last.get("output_tokens") or 0)

        # --- hardware --------------------------------------------------------
        for k in ("gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_power",
                  "gpu_power_limit", "gpu_pcie_gen", "gpu_pcie_gen_max", "gpu_pcie_width",
                  "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "ram_total",
                  "disk_read_mb", "disk_write_mb"):
            setattr(snapshot, k, hw.get(k))
        snapshot.gpus = hw.get("gpus") or []

        # --- engine facts ----------------------------------------------------
        snapshot.model          = engine.get("model") or (health or {}).get("model") or ""
        snapshot.engine_version = engine.get("version") or engine.get("engine") or ""
        snapshot.kv             = engine.get("kv") or ""
        snapshot.kv_resident    = engine.get("kv_resident") or 0
        snapshot.expert_slots   = engine.get("expert_slots") or 0
        snapshot.expert_cache_mib      = engine.get("expert_cache_mib") or 0
        snapshot.expert_slots_primary  = engine.get("expert_slots_primary") or 0
        snapshot.expert_cache_primary_mib = engine.get("expert_cache_primary_mib") or 0
        snapshot.spec           = engine.get("spec") or 0
        snapshot.mtp_max        = engine.get("mtp_max") or 0
        snapshot.drafting       = engine.get("drafting") or ""
        snapshot.drafting_note  = engine.get("drafting_note") or ""
        snapshot.lookup         = engine.get("lookup") or 0
        snapshot.pool_workers   = engine.get("pool_workers") or 0
        snapshot.conversation_cache_slots = engine.get("conversation_cache_slots") or 0
        snapshot.images         = bool(engine.get("images"))
        snapshot.cvec           = engine.get("cvec")

        # --- static hardware --------------------------------------------------
        snapshot.gpu_name  = static.get("gpu_name")
        snapshot.gpu_count = static.get("gpu_count") or 1
        snapshot.cpu_name  = static.get("cpu_name")
        snapshot.cores     = static.get("cores") or 0
        snapshot.threads   = static.get("threads") or 0
        snapshot.psutil    = bool(static.get("psutil"))

        # --- session totals, request ring, history series ---------------------
        snapshot.totals         = metrics.get("totals") or {}

        # Merge the server's current window into the local ring. The server
        # returns either its 12 newest rows or the whole ring, so a local ring
        # lets the panel fill the terminal without asking for ?requests=all on
        # every poll. A counter reset means the server restarted: drop the
        # stale rows and seed the full ring again.
        total = snapshot.totals.get("requests")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        if (self._prev_total is not None and total is not None
                and total < self._prev_total):
            self._req_ring.clear()
            self._graph_ring.clear()
            self._graph_last_sec = None
            self._seed_all = self._allow_seed and not self._all_requests
        self._prev_total = total

        self._merge_requests(metrics.get("requests") or [])
        snapshot.requests = self._ring_requests()
        try:
            server_kept = int(metrics.get("requests_kept") or 0)
        except (TypeError, ValueError):
            server_kept = 0
        snapshot.requests_kept = max(len(snapshot.requests), server_kept)
        snapshot.history        = metrics.get("history") or {}
        self._update_graph_ring(snapshot, metrics)

        # --- draft acceptance (/v1/status) ------------------------------------
        if status:
            self._last_status = status
            timings = status.get("last_timings") or {}
            snapshot.draft_n          = timings.get("draft_n") or 0
            snapshot.draft_n_accepted = timings.get("draft_n_accepted") or 0
            snapshot.draft_valid = snapshot.draft_n_accepted <= snapshot.draft_n
            snapshot.service = status.get("service") or ""
            snapshot.uptime_s = status.get("uptime_s")
            snapshot.concurrency_serving = (status.get("concurrency") or {}).get("serving") or 1

        # The full ring only needs to be fetched once; later polls use the
        # server's cheap default window and merge into _req_ring. If a restart
        # was detected during a default poll, keep the seed request pending
        # for the next cycle.
        if self._last_fetch_all:
            self._seed_all = False
        return snapshot

    @staticmethod
    def _request_key(row: dict) -> str:
        """Stable key for merging a request row across polls."""
        return _request_row_key(row)

    def _merge_requests(self, rows: list[dict]) -> None:
        for row in rows:
            if isinstance(row, dict):
                self._req_ring[self._request_key(row)] = row
        if len(self._req_ring) > self._req_ring_max:
            newest = sorted(
                self._req_ring.items(),
                key=lambda item: item[1].get("time") or 0,
                reverse=True,
            )[:self._req_ring_max]
            self._req_ring = dict(newest)

    def _ring_requests(self) -> list[dict]:
        """Newest-first view of the merged client-side request ring."""
        return sorted(
            self._req_ring.values(),
            key=lambda row: row.get("time") or 0,
            reverse=True,
        )

    @staticmethod
    def _series_values(values) -> list[float]:
        out: list[float] = []
        for value in values:
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                out.append(0.0)
        return out

    def _update_graph_ring(self, snapshot: StrataSnapshot, metrics: dict) -> None:
        """Keep a 120-sample graph ring, seeded from the server's 60.

        Strata publishes 60 one-second history samples. Halogen keeps 120 in
        its client ring; extending Strata's history lets both card panels draw
        the same time window and scroll one sample per second without the
        resampling that made braille dots appear to grow and shrink.
        """
        server = metrics.get("history") or {}
        second = int(snapshot.timestamp)

        if not self._graph_ring:
            for key, values in server.items():
                if isinstance(values, list) and values:
                    self._graph_ring[key] = (
                        self._series_values(values)[-self._graph_max:])
            self._graph_last_sec = second
        else:
            # A sensor exposed later still gets the server's remaining history.
            new_keys = set()
            for key, values in server.items():
                if (key not in self._graph_ring
                        and isinstance(values, list) and values):
                    self._graph_ring[key] = (
                        self._series_values(values)[-self._graph_max:])
                    new_keys.add(key)

            if (self._graph_last_sec is None
                    or second != self._graph_last_sec):
                for key, values in server.items():
                    if key in new_keys:
                        continue        # the seed already ends with this value
                    if not isinstance(values, list) or not values:
                        continue
                    try:
                        value = float(values[-1])
                    except (TypeError, ValueError):
                        continue
                    ring = self._graph_ring.get(key)
                    if ring is None:
                        continue
                    ring.append(value)
                    del ring[:-self._graph_max]
                self._graph_last_sec = second

        snapshot.client_history = {
            key: list(values) for key, values in self._graph_ring.items()
        }

    async def _fetch_metrics(self, session: aiohttp.ClientSession):
        """Fetch /metrics. Returns the _AUTH sentinel on 401, None on error."""
        want_all = self._all_requests or self._seed_all
        self._last_fetch_all = want_all
        endpoint = self.METRICS_ENDPOINT + ("?requests=all" if want_all else "")
        return await self._fetch_optional(session, endpoint)

    async def _fetch_status(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """Fetch /v1/status. Optional — it only adds draft acceptance."""
        try:
            return await self._fetch_json(session, self.STATUS_ENDPOINT)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Public accessors (no locks — single-threaded event loop)
    # ------------------------------------------------------------------

    def is_sleeping(self) -> bool:
        """True while Strata reports the model unloaded — reachable, not offline."""
        return self._server_sleeping

    def get_raw_responses(self) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
        """Return the most recent raw /health, /metrics and /v1/status bodies."""
        return self._last_health, self._last_metrics, self._last_status


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

# One line per hardware card, see strata_make_hardware_panel.
HARDWARE_ROWS = 10


def strata_make_metrics_panel(snapshot: StrataSnapshot, term_width: int,
                       _frame: int = 0, spacers: bool = True,
                       blink: bool = True) -> Panel:
    """Left panel: state, context, progress, token counts, queue, draft acceptance.

    *spacers* is False on short terminals: the blank separator lines are the
    only rows that carry no information, so they are the first to go.

    *blink* enables the >95 % context warning. It is time-based so the same
    1 s on / 1 s off pattern works at any refresh rate (YAMD renders at 1–2 Hz
    instead of 10 FPS). *_frame* is kept for existing callers but no longer
    drives the blink.
    """

    # --- State badge ---------------------------------------------------
    badge = _strata_state_badge(snapshot)
    badge_style = {
        "SLEEPING":   "dim",
        "QUEUED":     "bold black on dark_orange",
        "READING":    "bold white on magenta",
        "GENERATING": "bold white on green",
    }.get(badge, "bold white on blue")
    grid_w, narrow, tiny, label_w = _left_panel_geometry(term_width)
    state_text = Text(badge, style=badge_style)
    table, _lab, _gap = _detail_table(label_w, spacers, narrow)

    table.add_row("State:", state_text)
    _gap()

    # --- Context usage bar -----------------------------------------------
    # Context occupancy = full prompt length + generated tokens, with the
    # last finished request used while idle (app.js ctx-card).
    max_ctx   = snapshot.max_context if snapshot.max_context > 0 else 1
    ctx_ratio = min(snapshot.ctx_used / max_ctx, 1.0)
    context_row = _usage_bar(ctx_ratio, grid_w, label_w, tiny, narrow,
                             blink=blink)

    table.add_row(_lab("Max context:", "Max ctx:"),   f"{snapshot.max_context:,}")
    table.add_row(_lab("Context usage:", "Context:"), context_row)
    _gap()

    # --- Progress --------------------------------------------------------
    # Prompt progress is prompt_read/prompt_total, generation progress
    # generated/max_tokens — both computed by the server already.
    if snapshot.state == "reading" and snapshot.prompt_total:
        if tiny:
            table.add_row("Prompt:", f"{_kfmt(snapshot.prompt_read)}/"
                                     f"{_kfmt(snapshot.prompt_total)}")
        else:
            table.add_row("Prompt:", f"{snapshot.prompt_read or 0:,}/{snapshot.prompt_total:,} tok")
    elif snapshot.state == "generating" and snapshot.max_tokens:
        if tiny:
            table.add_row("Gen:", f"{_kfmt(snapshot.generated)}/"
                                  f"{_kfmt(snapshot.max_tokens)}")
        else:
            table.add_row("Generated:", f"{snapshot.generated or 0:,}/{snapshot.max_tokens:,} tok")
    else:
        last = (snapshot.requests or [None])[0]
        if last:
            if tiny:
                # The speed already has its own card, so only the length stays.
                table.add_row("Last req:", f"{_kfmt(last.get('output_tokens'))} tok")
            elif narrow:
                table.add_row("Last req:", f"{_kfmt(last.get('output_tokens'))} tok "
                                           f"@{_num(last.get('decode_tok_s'), 1)}t/s")
            else:
                table.add_row("Last request:", f"{_kfmt(last.get('output_tokens'))} tok "
                                               f"@ {_num(last.get('decode_tok_s'), 1)} t/s")
        else:
            table.add_row(_lab("Last request:", "Last req:"), "—")
    phase = snapshot.phase or "—"
    if tiny:
        phase = {"reading the prompt": "reading", "tool call complete": "tool",
                 "answering": "answer", "thinking": "think"}.get(phase, phase)
    elif narrow:
        phase = {"reading the prompt": "reading prompt",
                 "tool call complete": "tool call"}.get(phase, phase)
    table.add_row("Phase:", phase)
    _gap()

    # --- Token counts ----------------------------------------------------
    table.add_row(_lab("Prompt total:", "Prompt tot:"), f"{_kfmt(snapshot.prompt_tokens)}")
    table.add_row("Generated:", f"{_kfmt(snapshot.generated)}")
    table.add_row("Elapsed:",   f"{snapshot.elapsed_s:.1f} s"
                      if snapshot.elapsed_s is not None else "—")
    _gap()

    # --- Concurrency -----------------------------------------------------
    if tiny:
        table.add_row("Queue:", f"{snapshot.queued}q · {snapshot.concurrency_serving}srv")
    elif narrow:
        # "serving" alone does not fit the 16-character value column.
        table.add_row("Queue:", f"{snapshot.queued} queued · {snapshot.concurrency_serving} srv")
    else:
        table.add_row("Queue:", f"{snapshot.queued} queued · "
                                f"{snapshot.concurrency_serving} serving")
    _gap()

    # --- Draft acceptance (speculation) ----------------------------------
    if not snapshot.draft_valid:
        table.add_row(_lab("Draft accept:", "Draft acc:"), "invalid")
    elif snapshot.draft_n > 0:
        _acc = snapshot.draft_n_accepted / snapshot.draft_n * 100
        # ponytail: color thresholds are a UI heuristic; tune if draft-acc
        # semantics warrant different bands.
        _acc_style = _draft_style(_acc)
        if tiny:
            # "181/255 (71.0%)" needs 16 columns; the parentheses and the
            # decimal are what do not fit, so they go.
            table.add_row("Draft acc:",
                          f"[{_acc_style}]{snapshot.draft_n_accepted:,}/{snapshot.draft_n:,} "
                          f"{_acc:.0f}%[/{_acc_style}]")
        else:
            table.add_row(_lab("Draft accept:", "Draft acc:"),
                          f"[{_acc_style}]{snapshot.draft_n_accepted:,}/{snapshot.draft_n:,} "
                          f"({_acc:.1f}%)[/{_acc_style}]")
    else:
        table.add_row(_lab("Draft accept:", "Draft acc:"), "—")

    # --- Model identification -------------------------------------------
    if snapshot.model:
        if spacers:
            # Roomy terminal: the name gets its own line at full panel width.
            table.add_row("Model:", "")
            name = snapshot.model if len(snapshot.model) <= grid_w \
                   else snapshot.model[:grid_w - 1] + "…"
            model_table = Table(expand=True, show_header=False, show_footer=False, box=None)
            model_table.add_column(style="dark_orange")
            model_table.add_row(name)
            return Panel(Group(table, model_table),
                         title="[bold]Metrics[/bold]", border_style="cyan")
        # Short terminal: inline, so the panel does not need a 13th row.
        room = grid_w - label_w
        table.add_row("Model:", snapshot.model if len(snapshot.model) <= room
                      else snapshot.model[:room - 1] + "…")

    return Panel(table, title="[bold]Metrics[/bold]", border_style="cyan")


# ---------------------------------------------------------------------------
# Display speeds — read from the server, with the web Monitor's idle fallbacks
# ---------------------------------------------------------------------------

def _display_speeds(snapshot: StrataSnapshot) -> tuple[Optional[float], Optional[float], str, str]:
    """(decode, prefill, decode label, prefill label) as app.js shows them."""
    last = (snapshot.requests or [None])[0]

    if snapshot.state == "generating":
        decode, decode_sub = snapshot.tok_s, "now"
    elif last:
        decode, decode_sub = last.get("decode_tok_s"), "last req"
    else:
        decode, decode_sub = None, ""

    if snapshot.state != "idle":
        prefill, prefill_sub = snapshot.prefill_tok_s_mean, "this run"
    elif last and (last.get("prompt_ms") or 0) > 0:
        read = (last.get("prompt_tokens") or 0) - (last.get("reused") or 0)
        prefill = read / (last["prompt_ms"] / 1000) if read > 0 else None
        prefill_sub = "last req"
    else:
        prefill, prefill_sub = None, ""

    return decode, prefill, decode_sub, prefill_sub


def strata_make_performance_panel(snapshot: StrataSnapshot,
                                  term_width: int = 100) -> Panel:
    """Left panel: prompt & generation speeds side by side on one line."""
    decode, prefill, decode_sub, prefill_sub = _display_speeds(snapshot)

    # The right column has to hold "GENERATION SPEED:", so on narrow
    # terminals the labels give way and the sub-lines name themselves instead.
    narrow = _col_widths(term_width)[0] < 40
    label_w = 9 if narrow else 22
    lab_l = "PROMPT:" if narrow else "PROMPT SPEED:"
    lab_r = "GENERATE:" if narrow else "GENERATION SPEED:"
    sub_l = prefill_sub if narrow else (f"Prefill {prefill_sub}" if prefill_sub else "")
    sub_r = decode_sub if narrow else (f"Decode {decode_sub}" if decode_sub else "")
    return _make_performance_panel(
        prefill=prefill, decode=decode, label_w=label_w,
        lab_l=lab_l, lab_r=lab_r, sub_l=sub_l, sub_r=sub_r)


def strata_make_session_panel(snapshot: StrataSnapshot,
                       term_width: int = 100) -> Panel:
    """Session totals — Strata's cache reuse is a first-class number."""
    t = snapshot.totals or {}
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")

    if not t or not t.get("requests"):
        table.add_row("Reqs:", "no requests yet")
        table.add_row("Read:", "—")
        table.add_row("Written:", "—")
        return Panel(table, title="[bold]Session[/bold]", border_style="cyan")

    read = (t.get("prompt_tokens") or 0) - (t.get("reused") or 0)
    reused = t.get("reused") or 0
    rate = reused / t["prompt_tokens"] * 100 if t.get("prompt_tokens") else None

    # The "since" stamp now carries the date too, but the Session panel is a
    # fixed 5-row panel, so the row must never wrap: pick the widest date+time
    # form that fits the value column (left inner width minus the 8-col label).
    if t.get("since"):
        dt = datetime.fromtimestamp(t["since"])
        val_w = _col_widths(term_width)[0] - 8
        since = f"{dt:%H:%M}"  # time-only floor, always tried last
        for cand in (f"{MONTHS[dt.month - 1]} {dt.day:>2} {dt:%H:%M}",
                     f"{dt:%m-%d} {dt:%H:%M}"):
            if len(f"{t['requests']:,} since {cand}") <= val_w:
                since = cand
                break
    else:
        since = ""

    table.add_row("Reqs:", f"{t['requests']:,} since {since}")
    if _col_widths(term_width)[0] < 40:
        # "reused" is the wordiest part of the line; "of" keeps the same pair.
        table.add_row("Read:", f"{_kfmt(read)} of {_kfmt(t.get('prompt_tokens'))}"
                      + (f" ({rate:.0f}%)" if rate is not None else ""))
    else:
        table.add_row("Read:", f"{_kfmt(read)} · reused {_kfmt(reused)}"
                      + (f" ({rate:.0f}%)" if rate is not None else ""))
    table.add_row("Written:", f"{_kfmt(t.get('output_tokens'))} tokens")

    return Panel(table, title="[bold]Session[/bold]", border_style="cyan")


def _drafting_name(raw: str) -> str:
    """Human wording for Strata's engine.drafting field."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return {
        "mtp": "MTP",
        "dflash": "DFlash",
        "dflash2": "DFlash2",
        "off": "off",
        "none": "off",
    }.get(raw.lower(), raw)


def _fit_parts(parts: list[str], room: int) -> str:
    """Join ` · `-separated parts, dropping the least important tail."""
    text = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{text} · {part}" if text else part
        if len(candidate) <= room:
            text = candidate
        elif not text:
            text = part[:room]
    return text


def strata_make_engine_panel(snapshot: StrataSnapshot,
                      term_width: int = 100) -> Panel:
    """Engine facts Strata exposes and llama.cpp never did.

    Newer Strata builds name the active drafter in `engine.drafting` (`mtp`,
    `dflash2`, `off`, ...). When present it replaces the old generic `Spec:`
    row; older builds keep that row unchanged.
    """
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")

    kv = {"int8": "8-bit", "q4_0": "4-bit", "fp16": "16-bit"}.get(snapshot.kv, snapshot.kv)
    table.add_row("KV:", kv or "—")
    table.add_row("Experts:", f"{snapshot.expert_slots} slots · {snapshot.expert_cache_mib} MB")

    drafts = max(0, (snapshot.mtp_max or snapshot.spec) - 1) if snapshot.spec else 0
    room = max(10, _col_widths(term_width)[0] - 10)
    mode = _drafting_name(getattr(snapshot, "drafting", ""))
    lookup = f"lookup {snapshot.lookup}"
    pool = f"pool {snapshot.pool_workers}"

    if mode:
        label = "Draft:"
        low = mode.lower()
        if low == "off":
            parts = ["off", lookup, pool]
        elif low.startswith("dflash"):
            parts = [mode, lookup]
            if snapshot.spec:
                parts.append(f"spec {snapshot.spec}")
            parts.append(pool)
        elif low == "mtp":
            parts = [f"MTP ≤{drafts}", lookup, pool]
        else:
            parts = [mode, lookup]
            if snapshot.spec:
                parts.append(f"spec {snapshot.spec}")
            parts.append(pool)
    else:
        # Older Strata builds do not report the drafter name.
        label = "Spec:"
        parts = [f"MTP ≤{drafts}", lookup, pool]

    table.add_row(label, _fit_parts(parts, room))
    return Panel(table, title="[bold]Engine[/bold]", border_style="magenta")


def strata_make_hardware_panel(snapshot: StrataSnapshot, term_width: int) -> Panel:
    """The eight Monitor cards plus Experts in VRAM and System RAM.

    One line each: label, value, and a braille graph of the dashboard's
    120-sample ring (seeded from the server's 60-sample history) — the
    terminal equivalent of the web app's SVG, drawn the way btop draws its
    graphs: two samples per character, four dot rows per cell, and a distinct
    colour per card.
    """
    # Prefer the client-extended 120-sample ring, but keep any series the
    # server reported that the ring has not seen yet.
    h = dict(snapshot.history or {})
    h.update(getattr(snapshot, "client_history", None) or {})
    decode, prefill, decode_sub, prefill_sub = _display_speeds(snapshot)

    # Right column is ratio RIGHT_RATIO; subtract its borders.
    inner = _col_widths(term_width)[1]
    narrow, label_w, value_w, graph_w = _card_geometry(inner)
    card = _make_card_factory(h, label_w, value_w, graph_w)

    rows = []

    speed_sub = decode_sub if not narrow else ""
    rows.append(card("Speed", f"{decode:.1f} t/s" if decode is not None else "—",
                      speed_sub, "tok_s", None, "bold green"))

    multi = len(snapshot.gpus) > 1
    gpu_sub = (" · ".join(f"G{g.get('index')} {g.get('util')}%" for g in snapshot.gpus)
               if multi and not narrow else "")
    rows.append(card("GPU load", f"{_num(snapshot.gpu_util)} %", gpu_sub,
                      "gpu_util", 100.0, "bold white"))

    if narrow:
        # "of 8 GB" does not fit beside the value, so the total is folded in.
        vram_val = f"{_gb(snapshot.gpu_mem_used)}/{_gb(snapshot.gpu_mem_total, 0)} GB" \
                   if snapshot.gpu_mem_total else f"{_gb(snapshot.gpu_mem_used)} GB"
    else:
        vram_val = f"{_gb(snapshot.gpu_mem_used)} GB"
    vram_sub = f"of {_gb(snapshot.gpu_mem_total, 0)} GB" \
               if (snapshot.gpu_mem_total and not narrow) else ""
    rows.append(card("VRAM", vram_val, vram_sub,
                      "gpu_mem_used", float(snapshot.gpu_mem_total or 0), "bold white"))

    rows.append(card("GPU temp", f"{_num(snapshot.gpu_temp)} °C", "",
                      "gpu_temp", 90.0, "bold white"))

    if narrow:
        power_val = f"{_num(snapshot.gpu_power, 1)}/{_num(snapshot.gpu_power_limit)} W" \
                    if snapshot.gpu_power_limit else f"{_num(snapshot.gpu_power, 1)} W"
        power_sub = ""
    else:
        power_val = f"{_num(snapshot.gpu_power, 1)} W"
        power_sub = f"of {_num(snapshot.gpu_power_limit)} W" if snapshot.gpu_power_limit else ""
    rows.append(card("Power", power_val, power_sub,
                      "gpu_power", snapshot.gpu_power_limit, "bold white"))

    gen = snapshot.gpu_pcie_gen_max or snapshot.gpu_pcie_gen
    if narrow:
        # The rate already has its graph; the link description is what
        # has to stay readable.
        pcie_val = (f"Gen{gen} x{snapshot.gpu_pcie_width}"
                    if gen and snapshot.gpu_pcie_width else
                    f"Gen{gen}" if gen else "—")
        pcie_sub = ""
    else:
        pcie_val = (f"Gen{gen}"
                    + (f" x{snapshot.gpu_pcie_width}" if snapshot.gpu_pcie_width else "")
                    if gen else "—")
        pcie_sub = (f"{_num(snapshot.gpu_pcie_rx_mb, 1)} MB/s"
                    if snapshot.gpu_pcie_rx_mb is not None else "")
    rows.append(card("PCIe", pcie_val, pcie_sub, "gpu_pcie_rx_mb", None, "bold white"))

    cpu_sub = f"{snapshot.cores}c · {snapshot.threads}t" if (snapshot.threads and not narrow) else ""
    rows.append(card("CPU", f"{_num(snapshot.cpu, 1)} %", cpu_sub, "cpu", 100.0, "bold white"))

    if snapshot.disk_read_mb is None:
        disk_val, disk_sub = "—", ("needs psutil" if not snapshot.psutil else "")
    else:
        big = snapshot.disk_read_mb >= 1000
        disk_val = _num(snapshot.disk_read_mb / 1024, 2) if big else \
                   _num(snapshot.disk_read_mb, 1 if snapshot.disk_read_mb < 10 else 0)
        disk_sub = ("GB/s" if big else "MB/s")
        if snapshot.disk_write_mb is not None and not narrow:
            disk_sub += f" · w {_num(snapshot.disk_write_mb, 1)}"
    rows.append(card("Disk read", disk_val, disk_sub, "disk_read_mb", None, "bold white"))

    exp_val = (f"{snapshot.expert_slots} × {snapshot.expert_cache_mib} MB"
               if snapshot.expert_slots else "—")
    rows.append(card("Experts VRAM", exp_val, "", None, None, "bold white"))

    if narrow:
        ram_val = f"{_gb(snapshot.ram_used)}/{_gb(snapshot.ram_total, 0)} GB" \
                  if snapshot.ram_total else f"{_gb(snapshot.ram_used)} GB"
        ram_sub = ""
    else:
        ram_val = f"{_gb(snapshot.ram_used)} GB"
        ram_sub = f"of {_gb(snapshot.ram_total, 0)} GB" if snapshot.ram_total else ""
    rows.append(card("System RAM", ram_val, ram_sub,
                      "ram_used", float(snapshot.ram_total or 0), "bold white"))

    # Exactly HARDWARE_ROWS lines: the eight Monitor cards plus Experts and RAM.
    return _render_card_panel(rows[:HARDWARE_ROWS], inner, graph_w,
                              "[bold]Hardware[/bold]")


def strata_make_requests_panel(snapshot: StrataSnapshot, term_width: int,
                               rows: int, anchor: Optional[str] = None) -> Panel:
    """Recent requests — one page of Strata's ring (newest first)."""
    requests = snapshot.requests or []
    kept = snapshot.requests_kept or len(requests)
    start = _request_offset(requests, rows, anchor)
    shown = requests[start:start + rows]

    inner = _col_widths(term_width)[1]
    # Narrow boxes lose the columns whose numbers duplicate others: Reused is
    # Hit's complement, and Hit needs a percent sign. Time 8 + status 7 plus
    # one separator per numeric column must still leave room for the digits,
    # and 7 is also the narrowest width that keeps "Prompt" off its neighbour.
    cols = [("Prompt", "prompt_tokens"), ("Output", "output_tokens"),
            ("Tok/s", "decode_tok_s")]
    if inner >= 47:
        cols += [("Hit", "hit_rate")]
    if inner >= 55:
        cols.insert(1, ("Reused", "reused"))
    badge = {"stop": "Done", "length": "Max tok", "cancel": "Stop",
             "disconnect": "Closed", "error": "Error"}

    def _cell(row: dict, key: str) -> str:
        if key == "hit_rate":
            return f"{row[key] * 100:.1f}%" if row.get(key) is not None else "—"
        if key == "decode_tok_s":
            return _num(row.get(key), 1)
        return _kfmt(row.get(key))

    table = _request_table(
        inner, cols, requests, rows, num_min=4, status_header="Status",
        status=lambda row: badge.get(row.get("finish"),
                                     row.get("finish") or "—"),
        cell=_cell, start=start,
    )

    # The offline path swaps in strata_make_offline_panel instead, so reaching this
    # function always means the last poll answered.
    if start:
        span = f"{start + 1}–{start + len(shown)} of {kept} kept"
    else:
        span = f"{min(rows, kept)} of {kept} kept"
    title = f"[bold]Requests[/bold] [dim]· {span}[/dim]"
    return Panel(table, title=title, border_style="yellow")


def strata_make_offline_panel(error: str) -> Panel:
    text = Text()
    text.append("⚠  SERVER OFFLINE\n\n",         style="bold red")
    text.append(f"Error: {error}\n\n",            style="yellow")
    text.append("Ensure Strata is running and reachable at the address in the header:\n", style="dim")
    text.append("  python serve/server.py --port 8080\n", style="cyan")
    text.append("If the server has an API key, pass it with --api-key.\n", style="dim")
    return Panel(text, title="[bold red]CONNECTION LOST[/bold red]", border_style="red")


# ---------------------------------------------------------------------------
# Endpoints and polling
# ---------------------------------------------------------------------------

# Halogen's API port answers three routes a dashboard can use:
#   /health   the build's own account (version, context, slots, queue, cache
#             settings); always open, even with HALOGEN_API_KEY set, so it is
#             also the connectivity probe.
#   /metrics  Prometheus text: llama-server's counters plus halogen: ones.
#   /cache    the prompt cache's full report (hit rates, entries, pool).
# There is no per-request ring and no hardware telemetry: recent requests and
# every history series below are built by this client from the counters.
# Halogen's counters update at request completion, so a one-second poll is
# enough for the dashboard while keeping the graphs calm.
HALOGEN_POLL_INTERVAL = 1.0

# /health can block for ~1 s while the server re-probes the engine, and almost
# everything it carries is static (model, version, context, slots, drafter,
# cache settings). Refresh it every few seconds and take in-flight/queue state
# from /metrics, which stays on the 1 s cadence.
HEALTH_REFRESH = 10.0

# Optional xterm.js web view. Off by default; --web / --port enables it.
WEB_DEFAULT_PORT = 8080
WEB_FPS = 5
WEB_MIN_COLS, WEB_MAX_COLS = 30, 300
WEB_MIN_ROWS, WEB_MAX_ROWS = 15, 120

# How many request windows the collector keeps in its own ring. The server has
# no request list to ask for, so `--requests all` means this whole ring.
REQ_EVENT_MAX = 500

# Prometheus metric names are stable API (llama-server's shape plus halogen:).
M_PROMPT_TOKENS       = "llamacpp:prompt_tokens_total"
M_PROMPT_SECONDS      = "llamacpp:prompt_seconds_total"
M_PREDICTED_TOKENS    = "llamacpp:tokens_predicted_total"
M_PREDICTED_SECONDS   = "llamacpp:tokens_predicted_seconds_total"
M_REQUESTS_PROCESSING = "llamacpp:requests_processing"
M_REQUESTS_DEFERRED   = "llamacpp:requests_deferred"
M_KV_CACHE_TOKENS     = "llamacpp:kv_cache_tokens"
M_KV_CACHE_RATIO      = "llamacpp:kv_cache_usage_ratio"
M_REQUESTS_TOTAL      = "halogen:requests_total"
M_CACHED_TOKENS       = "halogen:prompt_tokens_cached_total"
M_DRAFT_TOKENS        = "halogen:draft_tokens_total"
M_DRAFT_ACCEPTED      = "halogen:draft_tokens_accepted_total"
M_DRAFT_LIST_TOKENS   = "halogen:draft_list_tokens_total"
M_DRAFT_LIST_MISSES   = "halogen:draft_list_misses_total"
M_STRUCTURED          = "halogen:structured_requests_total"
M_STRICT_TOOLS        = "halogen:strict_tool_requests_total"
M_STRICT_UNENFORCED   = "halogen:strict_tools_unenforced_total"
M_KV_POOL_POSITIONS   = "halogen:kv_pool_positions"
M_KV_POOL_RESERVED    = "halogen:kv_pool_reserved_tokens"
M_CACHE_STATS_AGE     = "halogen:cache_stats_age_seconds"
M_CACHE_HITS          = "halogen:cache_hits_total"
M_CACHE_MISSES        = "halogen:cache_misses_total"
M_CACHE_SAVED         = "halogen:cache_prompt_tokens_saved_total"
M_CACHE_ENTRIES       = "halogen:cache_entries"
M_CACHE_BYTES         = "halogen:cache_bytes"
M_CACHE_EVICTED       = "halogen:cache_evicted_total"
M_DISK_ENABLED        = "halogen:cache_disk_enabled"
M_DISK_BYTES          = "halogen:cache_disk_bytes"
M_DISK_BUDGET         = "halogen:cache_disk_budget_bytes"
M_DISK_HITS           = "halogen:cache_disk_hits_total"
M_MEANING_REWRITES    = "halogen:cache_last_message_rewrites_total"
M_DISK_CUT_BACK       = "halogen:cache_disk_cut_back_total"
M_POOL_WAITING        = "halogen:kv_pool_waiting_for_room"
M_ENGINE_UP           = "halogen:engine_up"
M_BUSY_FOR            = "halogen:busy_for_seconds"
M_QUEUE_WAITED        = "halogen:queue_waited_total"
M_QUEUE_WAIT_SECONDS  = "halogen:queue_wait_seconds_total"
M_QUEUE_WAIT_LONGEST  = "halogen:queue_wait_longest_seconds"
M_QUEUE_GAVE_UP       = "halogen:queue_gave_up_total"
M_GPU_EVICTED         = "halogen:gpu_queues_evicted_seconds_total"


# ---------------------------------------------------------------------------
# Data model — one flat snapshot of Halogen's three observable routes.
# ---------------------------------------------------------------------------

@dataclass
class HalogenSnapshot:
    """Immutable snapshot of Halogen's /health + /metrics + /cache at one instant."""
    timestamp: float

    # --- engine / connectivity ---------------------------------------------
    engine_up: bool = True
    capability_probe: str = "ok"      # ok | failed
    model: str = ""
    api_version: str = ""
    engine_version: str = ""

    # --- state (derived from in-flight / queue + counter deltas) -----------
    state: str = "idle"               # idle | busy | queued
    in_flight: int = 0
    queued: int = 0
    busy_for_s: Optional[float] = None
    longest_wait_s: Optional[float] = None

    # --- context and KV pool ----------------------------------------------
    context: int = 0                  # max context per slot
    slot_ctx: int = 0
    slots: int = 0
    kv_cache: str = ""                # bf16, fp8, ...
    kv_pool_positions: int = 0
    kv_pool_used: int = 0
    kv_pool_reserved: int = 0
    kv_usage_ratio: float = 0.0

    # --- throughput (cumulative counters + derived last-request speed) -----
    prompt_tokens_total: int = 0      # processed tokens (cache misses)
    predicted_tokens_total: int = 0
    prompt_seconds_total: float = 0.0
    predicted_seconds_total: float = 0.0
    prompt_tokens_cached_total: int = 0
    requests_total: int = 0
    decode_tok_s: Optional[float] = None
    prefill_tok_s: Optional[float] = None
    decode_source: str = ""
    prefill_source: str = ""

    # --- speculative decoding ---------------------------------------------
    draft_tokens_total: int = 0
    draft_tokens_accepted: int = 0
    draft_list_tokens: int = 0
    draft_list_misses: int = 0
    drafter_default: str = ""
    drafter_loaded: bool = False
    lookup_ngram: int = 0
    lookup_chain: int = 0

    # --- structured output / tools / vision -------------------------------
    structured_requests: int = 0
    strict_tool_requests: int = 0
    strict_tools_unenforced: int = 0
    structured_enabled: bool = False
    vision: bool = False
    vision_reason: str = ""
    composable_context: bool = False

    # --- prompt cache (/cache, metrics fallback) --------------------------
    cache_enabled: bool = False
    cache_mode: int = 0
    cache_cap_mb: int = 0
    cache_entries: int = 0
    cache_max_entries: int = 0
    cache_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_stores: int = 0
    cache_evicted: int = 0
    cache_dropped: int = 0
    cache_rerendered: int = 0
    cache_tokens_saved: int = 0
    cache_store_ms_total: float = 0.0
    cache_restore_ms_total: float = 0.0
    cache_hit_rate: Optional[float] = None       # 0..1, requests
    cache_token_hit_rate: Optional[float] = None  # 0..1, prompt tokens
    cache_stats_age: Optional[float] = None

    # --- KV pool details (/cache.pool) ------------------------------------
    pool_waiting_room: int = 0
    pool_waiting_s: float = 0.0
    pool_relocated: int = 0
    pool_cold_resorts: int = 0
    pool_busy_regions: int = 0
    pool_held_regions: int = 0

    # --- disk cache tier ---------------------------------------------------
    disk_enabled: bool = False
    disk_bytes: int = 0
    disk_budget_bytes: int = 0
    disk_hits: int = 0

    # --- queue counters ----------------------------------------------------
    queue_waited: int = 0
    queue_wait_seconds: float = 0.0
    queue_gave_up: int = 0
    gpu_evicted_seconds: float = 0.0

    # --- local host sensors (sampled only when the server is loopback) -----
    host_cpu_pct:   Optional[float] = None
    host_cpu_temp:  Optional[float] = None
    host_gpu_pct:   Optional[float] = None
    host_gpu_temp:  Optional[float] = None
    host_mem_used:  Optional[int] = None
    host_mem_total: Optional[int] = None

    # --- client-built session data ----------------------------------------
    session_requests: int = 0
    requests: list[dict] = None          # type: ignore[assignment]
    series: dict[str, list[float]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.requests is None:
            self.requests = []
        if self.series is None:
            self.series = {}


# ---------------------------------------------------------------------------
# Badge — Halogen's states plus the queue and probe conditions
# ---------------------------------------------------------------------------

def _halogen_state_badge(snapshot: HalogenSnapshot) -> str:
    """Halogen badge for a snapshot.

    Halogen exposes no live per-request phase: its token counters are written
    when a request finishes and neither /health nor /metrics says whether an
    in-flight request is prefilling or decoding. The badge therefore reports
    what the server does expose: queue, in-flight work, engine and probe state.
    """
    if not snapshot.engine_up:
        return "NO ENGINE"
    if snapshot.capability_probe not in ("", "ok"):
        return "DEGRADED"
    if snapshot.queued > 0:
        return "QUEUED"
    if snapshot.in_flight > 0:
        return "BUSY"
    return "IDLE"


# ---------------------------------------------------------------------------
# Prometheus parsing
# ---------------------------------------------------------------------------

def _parse_prometheus(text: str) -> dict[str, float]:
    """Parse the exposition format Halogen's /metrics answers with.

    Metric names keep their colons (`llamacpp:prompt_tokens_total`), labels
    are discarded, and a malformed line is skipped rather than failing the
    whole scrape. Last value wins, which is what a scraped counter should do.
    """
    out: dict[str, float] = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # name{labels} value   — labels may contain spaces inside strings, so
        # split the value off from the right and strip labels from the left.
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name_part, value_part = parts
        name = name_part.split("{", 1)[0]
        try:
            out[name] = float(value_part)
        except ValueError:
            continue
    return out


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _pick(*values, default=None):
    """First value that is not None (0 and False are kept)."""
    for v in values:
        if v is not None:
            return v
    return default


def _normalize_server(url: str) -> str:
    """Accept both the API root and the OpenAI-style `/v1` base.

    Halogen serves `/health`, `/metrics` and `/cache` at the root, while clients
    often call the API at `http://host:8731/v1`. Strip that suffix (and any
    trailing slash) so `-s host:8731/v1` reads the dashboard routes, not
    `/v1/health`. A missing scheme defaults to http://.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def _is_local_server(url: str) -> bool:
    """True when the server URL points at this machine's loopback.

    The dashboard only shows host sensors when it is running on the machine it
    monitors. Loopback names/IPs and the local hostname are accepted; a LAN IP
    is deliberately not, because the dashboard may run on a different box.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("127."):
        return True
    try:
        return host == socket.gethostname().lower()
    except OSError:
        return False


def _read_text(path) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _read_number(path) -> Optional[float]:
    text = _read_text(path)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class HostSampler:
    """Read CPU/GPU/RAM figures from Linux /proc and /sys.

    Only instantiated when the monitored server is loopback. The GPU paths are
    amdgpu's sysfs interface (Strix Halo), and the CPU temperature prefers
    k10temp/coretemp/zenpower over the ACPI thermal zone. Any missing source
    simply leaves that field as None, so the Host panel can degrade to "—".
    """

    def __init__(self) -> None:
        self._prev_cpu: Optional[tuple[int, int]] = None
        self._cpu_temp_path = self._find_cpu_temp()
        self._gpu_dir = self._find_gpu()
        self._gpu_busy_path = (self._gpu_dir / "gpu_busy_percent"
                               if self._gpu_dir else None)
        self._gpu_temp_path = self._find_gpu_temp()

    # ------------------------------------------------------------------
    # Path discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_cpu_temp() -> Optional[Path]:
        preferred = ("k10temp", "coretemp", "zenpower", "cpu", "soc")
        for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            name = (_read_text(hwmon / "name") or "").lower()
            if not any(p in name for p in preferred):
                continue
            candidates = sorted(hwmon.glob("temp*_input"))
            if not candidates:
                continue
            for cand in candidates:
                label = (_read_text(hwmon / cand.name.replace(
                    "_input", "_label")) or "").lower()
                if label in ("tctl", "tdie", "package", "cpu"):
                    return cand
            return candidates[0]
        # ACPI/thermal-zone fallback when no CPU hwmon driver is present.
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            kind = (_read_text(zone / "type") or "").lower()
            if any(k in kind for k in ("cpu", "x86_pkg", "soc")):
                return zone / "temp"
        return None

    @staticmethod
    def _find_gpu() -> Optional[Path]:
        cards = sorted(Path("/sys/class/drm").glob("card[0-9]*/device"))
        amd: list[Path] = []
        for card in cards:
            if not (card / "gpu_busy_percent").exists():
                continue
            vendor = (_read_text(card / "vendor") or "").lower()
            if vendor == "0x1002":
                amd.append(card)
        if amd:
            return amd[0]
        for card in cards:
            if (card / "gpu_busy_percent").exists():
                return card
        return None

    def _find_gpu_temp(self) -> Optional[Path]:
        if self._gpu_dir is None:
            return None
        hwmons = sorted((self._gpu_dir / "hwmon").glob("hwmon*"))
        first: Optional[Path] = None
        for hwmon in hwmons:
            for cand in sorted(hwmon.glob("temp*_input")):
                label = (_read_text(hwmon / cand.name.replace(
                    "_input", "_label")) or "").lower()
                if label == "edge":
                    return cand
                if first is None:
                    first = cand
        return first

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def _cpu_percent(self) -> Optional[float]:
        try:
            line = Path("/proc/stat").read_text().splitlines()[0]
            fields = [int(x) for x in line.split()[1:9]]
        except (OSError, ValueError, IndexError):
            return None
        total = sum(fields)
        idle = fields[3] + fields[4]          # idle + iowait
        if self._prev_cpu is None:
            self._prev_cpu = (total, idle)
            return None
        delta_total = total - self._prev_cpu[0]
        delta_idle = idle - self._prev_cpu[1]
        self._prev_cpu = (total, idle)
        if delta_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - delta_idle / delta_total)))

    @staticmethod
    def _memory() -> tuple[Optional[int], Optional[int]]:
        try:
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])
        except (OSError, ValueError, IndexError):
            return None, None
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if available is None:
            available = (info.get("MemFree", 0) + info.get("Buffers", 0)
                         + info.get("Cached", 0))
        if not total:
            return None, None
        used = max(0, total - available)
        return used * 1024, total * 1024

    def sample(self) -> dict[str, object]:
        cpu_temp = _read_number(self._cpu_temp_path) if self._cpu_temp_path else None
        gpu_pct = _read_number(self._gpu_busy_path) if self._gpu_busy_path else None
        gpu_temp = _read_number(self._gpu_temp_path) if self._gpu_temp_path else None
        mem_used, mem_total = self._memory()
        return {
            "host_cpu_pct": self._cpu_percent(),
            "host_cpu_temp": cpu_temp / 1000.0 if cpu_temp is not None else None,
            "host_gpu_pct": gpu_pct,
            "host_gpu_temp": gpu_temp / 1000.0 if gpu_temp is not None else None,
            "host_mem_used": mem_used,
            "host_mem_total": mem_total,
        }


# ---------------------------------------------------------------------------
# Halogen collector
# ---------------------------------------------------------------------------

class HalogenCollector(_CollectorBase):
    """Async background collector for Halogen's HTTP interface.

    Replaces threading.Lock + polling Thread with a single asyncio loop.
    Because the event loop is single-threaded, all mutations to shared state
    are inherently serial — no lock is ever needed.

    Halogen keeps no request ring and no hardware telemetry, so the collector
    also derives two things the server does not publish:

    * per-request figures from the cumulative counters' deltas (one row per
      completion window), and
    * a two-minute history ring for the braille graphs.
    """

    CACHE_ENDPOINT = "/cache"
    _state_badge = staticmethod(_halogen_state_badge)

    def __init__(self, server_url: str = "http://127.0.0.1:8731",
                 poll_interval: float = HALOGEN_POLL_INTERVAL,
                 api_key: Optional[str] = None,
                 sample_host: bool = False):
        self._init_base(_normalize_server(server_url), poll_interval, api_key)
        # Host sensors are only meaningful when the dashboard runs on the
        # machine it monitors; the application enables this for loopback URLs.
        self._host_sampler = HostSampler() if sample_host else None

        self._current: HalogenSnapshot = HalogenSnapshot(timestamp=time.time())

        # Cached /health body, refreshed by its own task.
        self._health: Optional[dict] = None
        self._health_error: Optional[str] = None
        self._health_task: Optional[asyncio.Task[None]] = None

        # Cumulative-counter baseline for the next delta. None until the first
        # successful scrape, so the server's lifetime totals never turn into
        # one giant phantom request row.
        self._prev: Optional[dict[str, float]] = None
        self._last_decode: Optional[float] = None
        self._last_prefill: Optional[float] = None
        self._session_requests = 0

        # Client-side request ring (newest last internally) and graph series.
        self._req_events: list[dict] = []
        self._series: dict[str, list[float]] = {}
        self._series_max = HISTORY_MAX

        # Raw responses, for --debug
        self._last_metrics: Optional[str] = None
        self._last_cache: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _before_poll_start(self) -> None:
        # /health gets its own task: its engine probe can block for ~1 s, and
        # it must not stall the 1 s /metrics + /cache cadence.
        self._health_task = asyncio.create_task(self._health_loop())

    def _background_tasks(self) -> tuple[Optional[asyncio.Task[None]], ...]:
        return (self._poll_task, self._health_task)

    # ------------------------------------------------------------------
    # aiohttp helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Collection loop
    # ------------------------------------------------------------------


    async def _health_loop(self) -> None:
        """Keep a fresh /health body without blocking the fast poll loop.

        A successful refresh is cached for HEALTH_REFRESH seconds; a failure is
        retried after one second so the header recovers quickly once the server
        answers. Connectivity is still judged by the /metrics poll below, which
        runs every second.
        """
        while self._running:
            started = time.monotonic()
            try:
                health = await self._fetch_health(self._session)  # type: ignore[arg-type]
                self._health = health or {}
                self._last_health = self._health
                self._health_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._health_error = f"{type(e).__name__}: {e}"

            delay = HEALTH_REFRESH if self._health_error is None else 1.0
            await asyncio.sleep(max(0.0, delay - (time.monotonic() - started)))

    async def _poll_loop(self) -> None:
        """Async polling loop — runs until *stop()* cancels this task.

        Error-handling policy:
        * Transport errors (timeout / connection reset) count toward the
          OFFLINE threshold (OFFLINE_THRESHOLD consecutive failures).
        * A 401 on /metrics while /health answers is a key problem, not an
          outage: connectivity stays ONLINE and the key error is reported.
        * Data errors (bad JSON / bad Prometheus) keep the last good snapshot
          and never flip connectivity — they are a parsing problem, not an
          outage. An engine that is down is reported as NO ENGINE, not as an
          unreachable server, because /health still answers.
        * /health runs in its own task, so its occasional 1 s engine probe
          cannot stretch this 1 s cadence.
        """
        next_at = time.monotonic()
        while self._running:
            try:
                snapshot = await self._fetch_and_parse()
                self._update_state(snapshot)

                self._current = snapshot
                self._consecutive_failures = 0
                self._key_rejected = False
                if not self._connected:
                    if not self._ever_connected:
                        # First successful cycle: come up immediately. Only a
                        # recovery from a real outage waits for two in a row.
                        self._connected = True
                        self._ever_connected = True
                        self._success_after_offline = 0
                    else:
                        self._success_after_offline += 1
                        if self._success_after_offline >= 2:
                            self._connected = True
                            self._success_after_offline = 0
                            self._last_error = None
                else:
                    self._last_error = None
                if self._health_error:
                    self._last_error = "Health endpoint unavailable"

            except Exception as exc:
                await self._handle_poll_error(exc)

            next_at += self.poll_interval
            delay = next_at - time.monotonic()
            if delay < 0:
                # A cycle overran (slow server, session rebuild): restart the
                # schedule instead of bursting to catch up.
                next_at = time.monotonic()
                delay = 0.0
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Fetch + parse
    # ------------------------------------------------------------------

    async def _fetch_and_parse(self) -> HalogenSnapshot:
        """Fan out /health, /metrics and /cache concurrently, then parse."""
        now = time.time()
        snapshot = HalogenSnapshot(timestamp=now)

        session = self._session  # guaranteed non-None after start()

        # /metrics carries the counters and /cache the full cache report. Both
        # must stay on the 1 s cadence; /health is refreshed by its own task
        # and only read here. Its failure is reported by that task, while this
        # loop's failures are what decide connectivity.
        metrics_text, cache = await asyncio.gather(
            self._fetch_metrics(session),
            self._fetch_cache(session),
        )
        health = self._health or {}

        if metrics_text is _AUTH:
            raise ApiKeyRejected("API key rejected (HTTP 401)")
        if metrics_text is None:
            raise MetricsUnavailable("Metrics endpoint unavailable")
        self._last_metrics = metrics_text  # type: ignore[assignment]

        if cache is _AUTH:
            raise ApiKeyRejected("API key rejected (HTTP 401)")
        if cache is None:
            cache = {}
        self._last_cache = cache  # type: ignore[assignment]

        m = _parse_prometheus(metrics_text)  # type: ignore[arg-type]
        health = health or {}

        # --- engine identity and state --------------------------------------
        up = m.get(M_ENGINE_UP)
        if up is None:
            # /metrics carries the liveness gauge, but fall back to the probe
            # /health reports if a build omits it.
            responds = (health.get("engine") or {}).get("responds")
            snapshot.engine_up = True if responds is None else bool(responds)
        else:
            snapshot.engine_up = bool(up)
        snapshot.capability_probe = health.get("capability_probe") or "ok"
        snapshot.model = health.get("model") or ""
        version = health.get("version") or {}
        snapshot.api_version = version.get("api") or ""
        snapshot.engine_version = version.get("engine") or snapshot.api_version

        snapshot.in_flight = _as_int(_pick(
            m.get(M_REQUESTS_PROCESSING), health.get("in_flight"), default=0))
        snapshot.queued = _as_int(_pick(
            m.get(M_REQUESTS_DEFERRED), health.get("queued"), default=0))
        snapshot.busy_for_s = _as_float(_pick(
            m.get(M_BUSY_FOR), health.get("busy_for_s")))
        snapshot.longest_wait_s = _as_float(_pick(
            m.get(M_QUEUE_WAIT_LONGEST), health.get("longest_wait_s")))

        # --- context and slots ----------------------------------------------
        snapshot.context = _as_int(health.get("context"))
        snapshot.slot_ctx = _as_int(health.get("slot_ctx"))
        snapshot.slots = _as_int(health.get("slots"))
        snapshot.kv_cache = health.get("kv_cache") or ""
        snapshot.kv_pool_positions = _as_int(_pick(
            health.get("kv_pool_positions"), m.get(M_KV_POOL_POSITIONS), default=0))
        snapshot.kv_pool_reserved = _as_int(m.get(M_KV_POOL_RESERVED))

        # --- throughput counters --------------------------------------------
        snapshot.prompt_tokens_total = _as_int(m.get(M_PROMPT_TOKENS))
        snapshot.predicted_tokens_total = _as_int(m.get(M_PREDICTED_TOKENS))
        snapshot.prompt_seconds_total = _as_float(m.get(M_PROMPT_SECONDS)) or 0.0
        snapshot.predicted_seconds_total = _as_float(m.get(M_PREDICTED_SECONDS)) or 0.0
        snapshot.prompt_tokens_cached_total = _as_int(m.get(M_CACHED_TOKENS))
        snapshot.requests_total = _as_int(m.get(M_REQUESTS_TOTAL))

        # --- speculative decoding -------------------------------------------
        snapshot.draft_tokens_total = _as_int(m.get(M_DRAFT_TOKENS))
        snapshot.draft_tokens_accepted = _as_int(m.get(M_DRAFT_ACCEPTED))
        snapshot.draft_list_tokens = _as_int(m.get(M_DRAFT_LIST_TOKENS))
        snapshot.draft_list_misses = _as_int(m.get(M_DRAFT_LIST_MISSES))
        snapshot.drafter_default = health.get("drafter_default") or ""
        snapshot.drafter_loaded = bool(health.get("drafter_weights_loaded"))
        lookup = health.get("prompt_lookup") or {}
        snapshot.lookup_ngram = _as_int(lookup.get("ngram"))
        snapshot.lookup_chain = _as_int(lookup.get("chain"))

        # --- structured output / tools / vision -----------------------------
        snapshot.structured_requests = _as_int(m.get(M_STRUCTURED))
        snapshot.strict_tool_requests = _as_int(m.get(M_STRICT_TOOLS))
        snapshot.strict_tools_unenforced = _as_int(m.get(M_STRICT_UNENFORCED))
        structured = health.get("structured_output") or {}
        snapshot.structured_enabled = bool(structured.get("enabled"))
        vision = health.get("vision") or {}
        snapshot.vision = bool(vision.get("enabled"))
        reasons = vision.get("disabled_because") or []
        snapshot.vision_reason = reasons[0] if reasons else ""
        snapshot.composable_context = bool(
            (health.get("composable_context") or {}).get("enabled"))

        # --- prompt cache: /cache is richer, metrics is the fallback --------
        prompt_cache = health.get("prompt_cache") or {}
        snapshot.cache_enabled = bool(prompt_cache.get("enabled"))
        snapshot.cache_mode = _as_int(prompt_cache.get("mode"))
        snapshot.cache_cap_mb = _as_int(prompt_cache.get("cap_mb"))

        snapshot.cache_entries = _as_int(_pick(
            cache.get("entries"), m.get(M_CACHE_ENTRIES), default=0))
        snapshot.cache_max_entries = _as_int(cache.get("max_entries"))
        snapshot.cache_bytes = _as_int(_pick(
            cache.get("bytes"), m.get(M_CACHE_BYTES), default=0))
        snapshot.cache_hits = _as_int(_pick(
            cache.get("hits"), m.get(M_CACHE_HITS), default=0))
        snapshot.cache_misses = _as_int(_pick(
            cache.get("misses"), m.get(M_CACHE_MISSES), default=0))
        snapshot.cache_stores = _as_int(cache.get("stores"))
        snapshot.cache_evicted = _as_int(_pick(
            cache.get("evicted"), m.get(M_CACHE_EVICTED), default=0))
        snapshot.cache_dropped = _as_int(cache.get("dropped"))
        snapshot.cache_rerendered = _as_int(cache.get("rerendered"))
        snapshot.cache_tokens_saved = _as_int(_pick(
            cache.get("prompt_tokens_saved"), m.get(M_CACHE_SAVED), default=0))
        snapshot.cache_store_ms_total = _as_float(cache.get("store_ms_total")) or 0.0
        snapshot.cache_restore_ms_total = _as_float(cache.get("restore_ms_total")) or 0.0
        snapshot.cache_hit_rate = _as_float(cache.get("hit_rate"))
        snapshot.cache_token_hit_rate = _as_float(cache.get("token_hit_rate"))
        snapshot.cache_stats_age = _as_float(_pick(
            cache.get("stale_s"), m.get(M_CACHE_STATS_AGE)))

        # /cache can be unavailable while /metrics still answers; recompute
        # the two rates from the counters so the panel does not go blind.
        if snapshot.cache_hit_rate is None:
            lookups = snapshot.cache_hits + snapshot.cache_misses
            if lookups > 0:
                snapshot.cache_hit_rate = snapshot.cache_hits / lookups
        if snapshot.cache_token_hit_rate is None:
            seen = (snapshot.prompt_tokens_total
                    + snapshot.prompt_tokens_cached_total)
            if seen > 0:
                snapshot.cache_token_hit_rate = (
                    snapshot.prompt_tokens_cached_total / seen)

        # --- KV pool details ------------------------------------------------
        pool = cache.get("pool") or {}
        snapshot.kv_pool_used = _as_int(_pick(
            pool.get("used"), m.get(M_KV_CACHE_TOKENS), default=0))
        snapshot.kv_pool_positions = _as_int(_pick(
            pool.get("positions"), snapshot.kv_pool_positions,
            m.get(M_KV_POOL_POSITIONS), default=0))
        snapshot.kv_usage_ratio = _as_float(_pick(
            pool.get("usage_ratio"), m.get(M_KV_CACHE_RATIO))) or 0.0
        snapshot.pool_waiting_room = _as_int(_pick(
            pool.get("waiting_for_room"), m.get(M_POOL_WAITING), default=0))
        snapshot.pool_waiting_s = _as_float(pool.get("waiting_s")) or 0.0
        snapshot.pool_relocated = _as_int(pool.get("relocated"))
        snapshot.pool_cold_resorts = _as_int(pool.get("cold_resorts"))
        snapshot.pool_busy_regions = _as_int(pool.get("busy_regions"))
        snapshot.pool_held_regions = _as_int(pool.get("held_regions"))

        # --- disk cache tier -------------------------------------------------
        disk = cache.get("disk") or {}
        snapshot.disk_enabled = bool(_pick(
            disk.get("on"), m.get(M_DISK_ENABLED), default=False))
        snapshot.disk_bytes = _as_int(_pick(
            disk.get("bytes"), m.get(M_DISK_BYTES), default=0))
        snapshot.disk_budget_bytes = _as_int(_pick(
            disk.get("budget_bytes"), m.get(M_DISK_BUDGET), default=0))
        snapshot.disk_hits = _as_int(_pick(
            disk.get("hits"), m.get(M_DISK_HITS), default=0))

        # --- queue counters --------------------------------------------------
        snapshot.queue_waited = _as_int(m.get(M_QUEUE_WAITED))
        snapshot.queue_wait_seconds = _as_float(m.get(M_QUEUE_WAIT_SECONDS)) or 0.0
        snapshot.queue_gave_up = _as_int(m.get(M_QUEUE_GAVE_UP))
        snapshot.gpu_evicted_seconds = _as_float(m.get(M_GPU_EVICTED)) or 0.0

        # --- local host sensors (loopback monitoring only) --------------------
        if self._host_sampler is not None:
            for key, value in self._host_sampler.sample().items():
                setattr(snapshot, key, value)

        # --- deltas, state, request rows, history ----------------------------
        self._derive(snapshot, m)
        return snapshot

    # ------------------------------------------------------------------
    # Deltas -> state, request rows, graph series
    # ------------------------------------------------------------------

    def _derive(self, snapshot: HalogenSnapshot, m: dict[str, float]) -> None:
        """Turn the cumulative counters into per-request figures and state.

        Halogen only writes its token counters when a request finishes (the
        engine's own per-request line), so a poll window with a positive delta
        is exactly one or more completed requests. in_flight/queued still say
        whether work is running right now.
        """
        prompt_delta = 0
        predicted_delta = 0
        cached_delta = 0
        req_delta = 0
        draft_delta = 0
        accepted_delta = 0
        prefill_now: Optional[float] = None
        decode_now: Optional[float] = None

        # A server restart resets every counter. Drop the cached speeds so the
        # panel does not keep advertising the previous engine's numbers.
        if (self._prev is not None
                and snapshot.requests_total < _as_int(
                    self._prev.get(M_REQUESTS_TOTAL))):
            self._last_decode = None
            self._last_prefill = None

        if self._prev is not None:
            def _delta(key: str, current: int) -> int:
                before = _as_int(self._prev.get(key))  # type: ignore[union-attr]
                return max(0, current - before)

            prompt_delta = _delta(M_PROMPT_TOKENS, snapshot.prompt_tokens_total)
            predicted_delta = _delta(M_PREDICTED_TOKENS,
                                     snapshot.predicted_tokens_total)
            cached_delta = _delta(M_CACHED_TOKENS,
                                  snapshot.prompt_tokens_cached_total)
            req_delta = _delta(M_REQUESTS_TOTAL, snapshot.requests_total)
            draft_delta = _delta(M_DRAFT_TOKENS,
                                 snapshot.draft_tokens_total)
            accepted_delta = _delta(M_DRAFT_ACCEPTED,
                                    snapshot.draft_tokens_accepted)

            prompt_s_delta = max(
                0.0,
                snapshot.prompt_seconds_total
                - (_as_float(self._prev.get(M_PROMPT_SECONDS)) or 0.0))
            predicted_s_delta = max(
                0.0,
                snapshot.predicted_seconds_total
                - (_as_float(self._prev.get(M_PREDICTED_SECONDS)) or 0.0))

            if prompt_delta > 0 and prompt_s_delta > 0:
                prefill_now = prompt_delta / prompt_s_delta
            if predicted_delta > 0 and predicted_s_delta > 0:
                decode_now = predicted_delta / predicted_s_delta

        # A request that completed since the previous poll tells us what the
        # engine was doing; without a delta, in-flight work stays generic BUSY.
        # There is no live phase signal in Halogen's API, so the badge does
        # not pretend to know whether an in-flight request is prefilling or
        # decoding (see _halogen_state_badge).
        if snapshot.queued > 0:
            snapshot.state = "queued"
        elif snapshot.in_flight > 0:
            snapshot.state = "busy"
        else:
            snapshot.state = "idle"

        # Speeds: the last completed request's own averages, falling back to
        # the server-lifetime average until something has finished.
        if decode_now is not None:
            self._last_decode = decode_now
        if prefill_now is not None:
            self._last_prefill = prefill_now

        cum_decode = (snapshot.predicted_tokens_total
                      / snapshot.predicted_seconds_total
                      if snapshot.predicted_seconds_total > 0 else None)
        cum_prefill = (snapshot.prompt_tokens_total
                       / snapshot.prompt_seconds_total
                       if snapshot.prompt_seconds_total > 0 else None)

        snapshot.decode_tok_s = _pick(self._last_decode, cum_decode)
        snapshot.prefill_tok_s = _pick(self._last_prefill, cum_prefill)
        snapshot.decode_source = ("last req" if self._last_decode is not None
                                  else ("session avg" if cum_decode else ""))
        snapshot.prefill_source = ("last req" if self._last_prefill is not None
                                   else ("session avg" if cum_prefill else ""))

        # One row per completion window (multiple requests can share a poll).
        if req_delta > 0:
            self._session_requests += req_delta
            self._req_events.append({
                "time": snapshot.timestamp,
                "requests": req_delta,
                "prompt_tokens": prompt_delta,
                "cached_tokens": cached_delta,
                "output_tokens": predicted_delta,
                "prefill_tok_s": prefill_now,
                "decode_tok_s": decode_now,
                "draft_tokens": draft_delta,
                "draft_accepted": accepted_delta,
                "draft_hit": (min(accepted_delta, draft_delta) / draft_delta
                              if draft_delta > 0 else None),
            })
            if len(self._req_events) > REQ_EVENT_MAX:
                self._req_events.pop(0)

        snapshot.session_requests = self._session_requests
        # Newest first for the panel's `[:rows]`.
        snapshot.requests = list(reversed(self._req_events))

        # --- history series --------------------------------------------------
        draft_acc = ((snapshot.draft_tokens_accepted / snapshot.draft_tokens_total
                      * 100) if snapshot.draft_tokens_total > 0 else 0.0)
        # Throughput graphs plot each poll window's own rate, not the retained
        # "last req" display value: a completed request is one spike, and the
        # line returns to 0 between requests instead of looking frozen.
        self._push("decode", decode_now or 0.0)
        self._push("prefill", prefill_now or 0.0)
        self._push("kv_usage", snapshot.kv_usage_ratio * 100)
        self._push("hit_rate", (snapshot.cache_hit_rate or 0.0) * 100)
        self._push("token_hit_rate", (snapshot.cache_token_hit_rate or 0.0) * 100)
        self._push("entries", snapshot.cache_entries)
        self._push("cache_bytes", snapshot.cache_bytes)
        self._push("draft_acc", draft_acc)
        self._push("in_flight", snapshot.in_flight)
        self._push("queue_wait", snapshot.longest_wait_s or 0.0)
        snapshot.series = {k: list(v) for k, v in self._series.items()}

        # Baseline for the next cycle. Only a successful scrape reaches here.
        self._prev = m

    def _push(self, key: str, value) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if math.isnan(v) or math.isinf(v):
            v = 0.0
        series = self._series.setdefault(key, [])
        series.append(v)
        if len(series) > self._series_max:
            series.pop(0)

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_metrics(self, session: aiohttp.ClientSession):
        """Fetch /metrics. Returns the _AUTH sentinel on 401, None on error."""
        return await self._fetch_optional(
            session, self.METRICS_ENDPOINT, text=True)

    async def _fetch_cache(self, session: aiohttp.ClientSession):
        """Fetch /cache. Returns the _AUTH sentinel on 401, None on error."""
        return await self._fetch_optional(session, self.CACHE_ENDPOINT)

    # ------------------------------------------------------------------
    # Public accessors (no locks — single-threaded event loop)
    # ------------------------------------------------------------------

    def get_raw_responses(self) -> tuple[Optional[dict], Optional[str], Optional[dict]]:
        """Return the most recent raw /health, /metrics and /cache bodies."""
        return self._last_health, self._last_metrics, self._last_cache


# ---------------------------------------------------------------------------
# Layout helpers and panel builders
# ---------------------------------------------------------------------------

# One line per card in the Cache & Pool panel, see halogen_make_cache_panel.
CACHE_ROWS = 10


def halogen_layout_mode(body: int, local: bool) -> str:
    """Choose how many fixed panels the left column can afford.

    full       Metrics + Performance + Session + Engine (+ Host when local)
    no_session Metrics + Performance + Engine + Host (local, medium height)
    compact    Metrics + Performance

    Metrics needs METRIC_ROWS + 2 rows (content + borders). When the pane is
    too short, the information panels are dropped rather than clipping it.
    """
    metric_min = METRIC_ROWS + 2
    if local:
        if body >= metric_min + 4 * PANEL_ROWS:
            return "full"
        if body >= metric_min + 3 * PANEL_ROWS:
            return "no_session"
        return "compact"
    if body >= metric_min + 3 * PANEL_ROWS:
        return "full"
    return "compact"


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def halogen_make_metrics_panel(snapshot: HalogenSnapshot, term_width: int,
                       spacers: bool = True) -> Panel:
    """Left panel: state, KV-pool gauge, throughput, queue, cache, draft.

    *spacers* is False on short terminals: the blank separator lines are the
    only rows that carry no information, so they are the first to go.
    """

    # --- State badge ---------------------------------------------------
    badge = _halogen_state_badge(snapshot)
    badge_style = {
        "NO ENGINE":  "bold white on red",
        "DEGRADED":   "bold black on dark_orange",
        "QUEUED":     "bold black on dark_orange",
        "BUSY":       "bold white on blue",
    }.get(badge, "bold white on blue")

    grid_w, narrow, tiny, label_w = _left_panel_geometry(term_width)
    state_text = Text(badge, style=badge_style)
    table, _lab, _gap = _detail_table(label_w, spacers, narrow)

    table.add_row("State:", state_text)
    _gap()

    # --- KV-pool usage bar -----------------------------------------------
    # Halogen's gauge is the KV pool, not a single request's context: the
    # engine reserves positions for every warm conversation, and /cache's
    # pool.usage_ratio is the honest occupancy number (kv_cache_usage_ratio
    # is the same figure from /metrics).
    positions = snapshot.kv_pool_positions
    has_pool = positions > 0
    kv_ratio = min(max(snapshot.kv_usage_ratio, 0.0), 1.0) if has_pool else 0.0
    context_row = _usage_bar(kv_ratio, grid_w, label_w, tiny, narrow,
                             available=has_pool)

    table.add_row(_lab("KV pool:", "KV:"), context_row)
    if snapshot.context:
        if tiny:
            ctx_val = f"{_kfmt(snapshot.context)} · {snapshot.slots}" \
                      if snapshot.slots else _kfmt(snapshot.context)
        else:
            ctx_val = (f"{_kfmt(snapshot.context)} · {snapshot.slots} slots"
                       if snapshot.slots else _kfmt(snapshot.context))
    else:
        ctx_val = "—"
    table.add_row(_lab("Context:", "Ctx:"), ctx_val)
    _gap()

    # --- Last request / queue --------------------------------------------
    last = (snapshot.requests or [None])[0]
    if last:
        out_tok = _kfmt(last.get("output_tokens"))
        speed = last.get("decode_tok_s")
        if speed is not None:
            req_val = f"{out_tok} tok @ {_num(speed, 1)} t/s"
        else:
            req_val = f"{out_tok} tok"
        if tiny:
            req_val = out_tok
    else:
        req_val = "—"
    table.add_row(_lab("Last request:", "Last req:"), req_val)

    if tiny:
        flight_val = f"{snapshot.in_flight}/{snapshot.slots or '?'} · {snapshot.queued}q"
    elif narrow:
        flight_val = f"{snapshot.in_flight}/{snapshot.slots or '?'} · {snapshot.queued} queued"
    else:
        flight_val = (f"{snapshot.in_flight} of {snapshot.slots or '?'} slots · "
                      f"{snapshot.queued} queued")
    table.add_row(_lab("In flight:", "Flight:"), flight_val)
    table.add_row(_lab("Busy for:", "Busy:"), _secs(snapshot.busy_for_s))
    _gap()

    # --- Token totals ------------------------------------------------------
    table.add_row(_lab("Prompt total:", "Prompt tot:"),
                  _kfmt(snapshot.prompt_tokens_total) if tiny
                  else f"{_kfmt(snapshot.prompt_tokens_total)} processed")
    table.add_row("Generated:", _kfmt(snapshot.predicted_tokens_total))
    _gap()

    # --- Draft acceptance (speculation) -----------------------------------
    if snapshot.draft_tokens_total > 0:
        _acc = snapshot.draft_tokens_accepted / snapshot.draft_tokens_total * 100
        _acc_style = _draft_style(_acc)
        if tiny:
            acc_val = f"{_acc:.0f}%"
        elif narrow:
            acc_val = (f"{_kfmt(snapshot.draft_tokens_accepted)}/"
                       f"{_kfmt(snapshot.draft_tokens_total)} ({_acc:.0f}%)")
        else:
            acc_val = (f"{snapshot.draft_tokens_accepted:,}/"
                       f"{snapshot.draft_tokens_total:,} ({_acc:.1f}%)")
        table.add_row(_lab("Draft accept:", "Draft acc:"),
                      f"[{_acc_style}]{acc_val}[/{_acc_style}]")
    else:
        table.add_row(_lab("Draft accept:", "Draft acc:"), "—")

    # --- Prompt-cache hit rates -------------------------------------------
    hit = snapshot.cache_hit_rate
    thit = snapshot.cache_token_hit_rate
    if hit is None and thit is None:
        cache_val = "—"
    elif tiny:
        cache_val = (f"{(hit or 0) * 100:.0f}%/{(thit or 0) * 100:.0f}%")
    elif narrow:
        cache_val = (f"{(hit or 0) * 100:.0f}% req · "
                     f"{(thit or 0) * 100:.0f}% tok")
    elif hit is not None and thit is not None:
        cache_val = f"{hit * 100:.1f}% req · {thit * 100:.1f}% tok"
    else:
        cache_val = f"{(hit if hit is not None else thit) * 100:.1f}%"
    table.add_row(_lab("Cache hit:", "Cache:"), cache_val)
    _gap()

    # --- Structured output / tools ----------------------------------------
    # On a 60-column console the 13-row Metrics panel cannot hold this row
    # and the model line; structured output is the least informative one.
    if not tiny:
        if narrow:
            struct_val = (f"{_kfmt(snapshot.structured_requests)} sch · "
                          f"{_kfmt(snapshot.strict_tool_requests)} strict")
        else:
            struct_val = (f"{_kfmt(snapshot.structured_requests)} schema · "
                          f"{_kfmt(snapshot.strict_tool_requests)} strict")
        if snapshot.strict_tools_unenforced:
            struct_val += f" · {snapshot.strict_tools_unenforced} loose"
        table.add_row(_lab("Structured:", "Struct:"), struct_val)

    # --- Model identification -------------------------------------------
    if snapshot.model:
        # Inline, not a second table: on the common 120x40 terminal the flex
        # Metrics panel is exactly 17 content rows tall, and a separate model
        # line would push the name past the border.
        room = grid_w - label_w
        table.add_row("Model:", snapshot.model if len(snapshot.model) <= room
                      else snapshot.model[:room - 1] + "…")

    return Panel(table, title="[bold]Metrics[/bold]", border_style="cyan")


# ---------------------------------------------------------------------------
# Display speeds — measured per completion window, with a session fallback
# ---------------------------------------------------------------------------

def halogen_make_performance_panel(snapshot: HalogenSnapshot,
                                   term_width: int = 100) -> Panel:
    """Left panel: prefill (prompt) & decode (generation) speeds side by side.

    Halogen writes its counters when a request finishes, so the figure is the
    last completed request's own average ("last req") or the server-lifetime
    average before anything has finished ("session avg").
    """
    narrow = _col_widths(term_width)[0] < 40
    label_w = 9 if narrow else 22
    lab_l = "PREFILL:" if narrow else "PREFILL SPEED:"
    lab_r = "DECODE:" if narrow else "DECODE SPEED:"
    if narrow:
        sub_l = ("pf " + snapshot.prefill_source.replace("last req", "last")
                 .replace("session avg", "avg")) if snapshot.prefill_source else ""
        sub_r = ("dec " + snapshot.decode_source.replace("last req", "last")
                 .replace("session avg", "avg")) if snapshot.decode_source else ""
    else:
        sub_l = f"Prefill {snapshot.prefill_source}" if snapshot.prefill_source else ""
        sub_r = f"Decode {snapshot.decode_source}" if snapshot.decode_source else ""
    return _make_performance_panel(
        prefill=snapshot.prefill_tok_s, decode=snapshot.decode_tok_s,
        label_w=label_w, lab_l=lab_l, lab_r=lab_r, sub_l=sub_l, sub_r=sub_r)


def halogen_make_session_panel(snapshot: HalogenSnapshot,
                       term_width: int = 100) -> Panel:
    """Session totals — Halogen's cached-token share is a first-class number.

    Halogen reports counters for the server's whole life; the "new" figure is
    what this dashboard has seen since it started, because the server has no
    start-time field to anchor a "since" stamp to.
    """
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")
    narrow = _col_widths(term_width)[0] < 40

    if snapshot.requests_total == 0 and snapshot.session_requests == 0:
        table.add_row("Reqs:", "no requests yet")
        table.add_row("Prompt:", "—")
        table.add_row("Output:", "—")
        return Panel(table, title="[bold]Session[/bold]", border_style="cyan")

    if narrow:
        req_val = f"{_kfmt(snapshot.session_requests)} / {_kfmt(snapshot.requests_total)}"
    else:
        req_val = (f"{_kfmt(snapshot.session_requests)} new · "
                   f"{_kfmt(snapshot.requests_total)} total")
    table.add_row("Reqs:", req_val)

    thit = snapshot.cache_token_hit_rate
    if narrow:
        prompt_val = (f"{_kfmt(snapshot.prompt_tokens_total)} · "
                      f"{_kfmt(snapshot.prompt_tokens_cached_total)} cached")
    else:
        prompt_val = (f"{_kfmt(snapshot.prompt_tokens_total)} processed · "
                      f"{_kfmt(snapshot.prompt_tokens_cached_total)} cached")
        if thit is not None:
            prompt_val += f" ({thit * 100:.0f}%)"
    table.add_row("Prompt:", prompt_val)

    table.add_row("Output:", f"{_kfmt(snapshot.predicted_tokens_total)} tokens")

    return Panel(table, title="[bold]Session[/bold]", border_style="cyan")


def halogen_make_engine_panel(snapshot: HalogenSnapshot,
                      term_width: int = 100) -> Panel:
    """Engine facts Halogen exposes: KV dtype/pool, drafter, prompt cache."""
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")

    kv = snapshot.kv_cache or "—"
    if snapshot.kv_pool_positions:
        kv += f" · {_kfmt(snapshot.kv_pool_positions)} pos"
    table.add_row("KV:", kv)

    if snapshot.drafter_default:
        drafter = snapshot.drafter_default
        if not snapshot.drafter_loaded:
            drafter += " (no head)"
        if snapshot.lookup_ngram:
            drafter += f" · lookup {snapshot.lookup_ngram}×{snapshot.lookup_chain}"
    else:
        drafter = "off"
    table.add_row("Draft:", drafter)

    if snapshot.cache_enabled:
        cache = f"mode {snapshot.cache_mode}"
        if snapshot.cache_cap_mb:
            # /health's cap_mb is per cache entry (24 entries by default), not
            # the whole cache, so say so on terminals wide enough to fit it.
            if _col_widths(term_width)[0] < 40:
                cache += f" · {snapshot.cache_cap_mb} MB"
            else:
                cache += f" · {snapshot.cache_cap_mb} MB/entry"
        if snapshot.disk_enabled:
            cache += " · disk"
    else:
        cache = "off"
    table.add_row("Cache:", cache)

    return Panel(table, title="[bold]Engine[/bold]", border_style="magenta")


def halogen_make_host_panel(snapshot: HalogenSnapshot,
                    term_width: int = 100) -> Panel:
    """Local host load/temperature/RAM — only shown for loopback servers.

    Values come from /proc/stat, /proc/meminfo, k10temp/coretemp and amdgpu
    sysfs (see HostSampler). Missing sources render as "—" rather than hiding
    the panel, so a partially supported machine still shows what it can.
    """
    inner = _col_widths(term_width)[0]
    narrow = inner < 40
    tiny = inner < 30

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=5)
    table.add_column(style="bold white")

    def _load_style(pct: float) -> str:
        if pct >= 90:
            return "bold red"
        if pct >= 70:
            return "bold dark_orange"
        return "bold green"

    def _temp_style(temp: float) -> str:
        if temp >= 90:
            return "bold red"
        if temp >= 75:
            return "dark_orange"
        return "dim"

    def _load_row(pct: Optional[float], temp: Optional[float]) -> Text:
        line = Text()
        if pct is None:
            line.append("—", style="dim")
        else:
            line.append(f"{pct:.1f} %", style=_load_style(pct))
        if temp is not None:
            line.append(f"  {temp:.0f} °C", style=_temp_style(temp))
        return line

    table.add_row("CPU:", _load_row(snapshot.host_cpu_pct,
                                    snapshot.host_cpu_temp))
    table.add_row("GPU:", _load_row(snapshot.host_gpu_pct,
                                    snapshot.host_gpu_temp))

    used, total = snapshot.host_mem_used, snapshot.host_mem_total
    if not total:
        ram = Text("—", style="dim")
    else:
        pct = min(100.0, max(0.0, used / total * 100)) if used is not None else None
        ram = Text()
        if pct is None:
            ram.append("—", style="dim")
        else:
            ram.append(f"{pct:.1f} %", style=_load_style(pct))
        if used is not None and not tiny:
            if narrow:
                ram.append(f"  {_gb(used, 0)}/{_gb(total, 0)} GB", style="dim")
            else:
                ram.append(f"  {_gb(used)}/{_gb(total, 0)} GB", style="dim")
    table.add_row("RAM:", ram)

    title = "[bold]Host[/bold]" if narrow else "[bold]Host[/bold] [dim]· local[/dim]"
    return Panel(table, title=title, border_style="blue")


def halogen_make_cache_panel(snapshot: HalogenSnapshot, term_width: int) -> Panel:
    """Halogen's efficiency cards: throughput, KV pool, prompt cache, drafts.

    One line each: label, value, and a braille graph of the dashboard's own
    polling ring — two samples per character, four dot rows per cell, and a
    distinct colour per card. Halogen publishes no hardware telemetry, so the
    graphs cover the server's own counters instead of GPU/CPU sensors.
    """
    series = snapshot.series or {}

    # Right column is ratio RIGHT_RATIO; subtract its borders.
    inner = _col_widths(term_width)[1]
    narrow, label_w, value_w, graph_w = _card_geometry(inner)
    card = _make_card_factory(series, label_w, value_w, graph_w)

    rows = []

    # 1-2: throughput, measured from counter deltas at request completion.
    # The source is named on the Performance panel; the value column here is
    # too narrow for a second label, and both cards share the same source.
    rows.append(card("Decode", f"{_num(snapshot.decode_tok_s, 1)} t/s",
                      "", "decode", None, "bold green"))
    rows.append(card("Prefill", f"{_num(snapshot.prefill_tok_s, 1)} t/s",
                      "", "prefill", None, "bold magenta"))

    # 3: KV-pool occupancy.
    kv = snapshot.kv_usage_ratio * 100 if snapshot.kv_pool_positions else None
    kv_sub = (f"{_kfmt(snapshot.kv_pool_used)}/{_kfmt(snapshot.kv_pool_positions)}"
              if snapshot.kv_pool_positions and not narrow else "")
    rows.append(card("KV pool", f"{_num(kv, 1)} %" if kv is not None else "—",
                      kv_sub, "kv_usage", 100.0, "bold white"))

    # 4-5: prompt-cache hit rates.
    hit = snapshot.cache_hit_rate
    hit_sub = (f"{_kfmt(snapshot.cache_hits)}/{_kfmt(snapshot.cache_hits + snapshot.cache_misses)}"
               if not narrow and (snapshot.cache_hits or snapshot.cache_misses) else "")
    rows.append(card("Cache hit", f"{_num((hit or 0) * 100, 1)} %"
                      if hit is not None else "—",
                      hit_sub, "hit_rate", 100.0, "bold white"))

    thit = snapshot.cache_token_hit_rate
    rows.append(card("Token hit", f"{_num((thit or 0) * 100, 1)} %"
                      if thit is not None else "—",
                      "", "token_hit_rate", 100.0, "bold white"))

    # 6: entries used against the configured maximum.
    entries = (f"{snapshot.cache_entries}/{snapshot.cache_max_entries}"
               if snapshot.cache_max_entries else f"{snapshot.cache_entries}")
    rows.append(card("Entries", entries, "",
                      "entries", float(snapshot.cache_max_entries or 0), "bold white"))

    # 7: cache size (bytes held). The sub-line surfaces a stale cache report
    # (/cache answers the last counters it had during a long cold prefill).
    age = snapshot.cache_stats_age
    if not narrow and age is not None and age >= 5:
        size_sub = f"age {age:.0f}s"
    else:
        size_sub = f"{_kfmt(snapshot.cache_stores)} st" if not narrow else ""
    rows.append(card("Cache size", f"{_gb(snapshot.cache_bytes, 2)} GB",
                      size_sub, "cache_bytes", None, "bold white"))

    # 8: speculative-decoding acceptance.
    if snapshot.draft_tokens_total > 0:
        acc = snapshot.draft_tokens_accepted / snapshot.draft_tokens_total * 100
        acc_val = f"{acc:.1f} %"
        acc_sub = (f"{_kfmt(snapshot.draft_tokens_accepted)}/"
                   f"{_kfmt(snapshot.draft_tokens_total)}" if not narrow else "")
    else:
        acc_val, acc_sub = "—", ""
    rows.append(card("Draft acc", acc_val, acc_sub,
                      "draft_acc", 100.0, "bold white"))

    # 9: slot occupancy.
    flight = (f"{snapshot.in_flight}/{snapshot.slots or '?'}"
              if snapshot.slots else str(snapshot.in_flight))
    rows.append(card("In flight", flight,
                      f"{snapshot.queued} queued" if not narrow else "",
                      "in_flight", float(snapshot.slots or 0), "bold white"))

    # 10: longest wait in the admission queue.
    wait = snapshot.longest_wait_s
    wait_val = f"{wait:.1f} s" if wait is not None else "—"
    wait_sub = (f"{snapshot.pool_waiting_room} waiting"
                if not narrow and snapshot.pool_waiting_room else "")
    rows.append(card("Queue wait", wait_val, wait_sub,
                      "queue_wait", None, "bold white"))

    # Exactly CACHE_ROWS lines.
    title = "[bold]Cache & Pool[/bold]" if not narrow else "[bold]Cache[/bold]"
    return _render_card_panel(rows[:CACHE_ROWS], inner, graph_w, title)


def halogen_make_requests_panel(snapshot: HalogenSnapshot, term_width: int,
                                rows: int,
                                anchor: Optional[str] = None) -> Panel:
    """Recent requests — completion windows this dashboard derived itself.

    Halogen exposes no request ring, so each row is a poll window in which
    `halogen:requests_total` moved; several completions inside one window
    share a row and are counted under Reqs. PP and TG are the window's own
    prefill/decode rates, and Draft is that window's speculative acceptance
    (`draft_tokens_accepted` / `draft_tokens`).
    """
    requests = snapshot.requests or []
    kept = len(requests)
    start = _request_offset(requests, rows, anchor)
    shown = requests[start:start + rows]

    inner = _col_widths(term_width)[1]

    # Required priority as the panel narrows: Draft goes first, then PP, and
    # GEN stays to the end. Prompt/Cached follow their own width thresholds;
    # Output is always present, as before.
    cols = [("Output", "output_tokens")]
    if inner >= 48:
        cols.append(("PP", "prefill_tok_s"))
    cols.append(("TG", "decode_tok_s"))
    if inner >= 64:
        cols.append(("Draft", "draft_hit"))
    if inner >= 42:
        cols.insert(0, ("Prompt", "prompt_tokens"))
    if inner >= 74:
        cols.insert(1, ("Cached", "cached_tokens"))

    def _header(name: str, key: str, num_w: int) -> str:
        # Keep the unit while it fits; on a tiny panel the short label avoids
        # a two-line header.
        if key == "prefill_tok_s":
            return "PP t/s" if num_w >= 6 else "PP"
        if key == "decode_tok_s":
            return "TG t/s" if num_w >= 6 else "TG"
        return name

    def _status(row: dict) -> str:
        n = row.get("requests") or 0
        return "Done" if n == 1 else f"{n} reqs"

    def _cell(row: dict, key: str) -> str:
        if key in ("decode_tok_s", "prefill_tok_s"):
            return _num(row.get(key), 1)
        if key == "draft_hit":
            hit = row.get(key)
            return f"{hit * 100:.1f}%" if hit is not None else "—"
        return _kfmt(row.get(key))

    table = _request_table(
        inner, cols, requests, rows, num_min=5, status_header="Reqs",
        status=_status, cell=_cell, header=_header, start=start,
    )

    if start:
        span = f"{start + 1}–{start + len(shown)}/{kept} windows"
    else:
        span = f"{kept} windows"
    title = (f"[bold]Requests[/bold] [dim]· {snapshot.session_requests:,} seen · "
             f"{span}[/dim]")
    return Panel(table, title=title, border_style="yellow")


def halogen_make_offline_panel(error: str) -> Panel:
    text = Text()
    text.append("⚠  SERVER OFFLINE\n\n",         style="bold red")
    text.append(f"Error: {error}\n\n",            style="yellow")
    text.append("Ensure the Halogen server is running and reachable at the address\n",
                style="dim")
    text.append("in the header (API port 8731 by default).\n", style="dim")
    text.append("If HALOGEN_API_KEY is set, pass it with --api-key.\n", style="dim")
    return Panel(text, title="[bold red]CONNECTION LOST[/bold red]", border_style="red")


# ---------------------------------------------------------------------------
# Optional web view — xterm.js receives Rich's ANSI frames over a WebSocket.
# ---------------------------------------------------------------------------

WEB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YAMD</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css">
<style>
  html, body { margin: 0; padding: 0; width: 100%; height: 100%;
               background: #1b1b1b; overflow: hidden; }
  #term { width: 100vw; height: 100vh; padding: 6px; box-sizing: border-box; }
  /* Shown by setLink(false) while the WebSocket to the dashboard is down;
     the server cannot render this itself because it is the side that died. */
  #net { position: fixed; top: 0; left: 0; right: 0; z-index: 10;
         display: none; padding: 4px 10px; box-sizing: border-box;
         background: #8b1e1e; color: #fff; border-bottom: 1px solid #d33;
         font: bold 13px/1.4 monospace; }
  .xterm { height: 100%; }
  .xterm-viewport { overflow: hidden !important; }
</style>
</head>
<body>
<div id="net">⚠ Dashboard disconnected — retrying…</div>
<div id="term"></div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>
<script>
  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
    document.getElementById("term").innerHTML =
      "<pre style='color:#ddd;padding:16px'>xterm.js could not be loaded " +
      "(the browser could not reach the CDN).</pre>";
  } else {
    const term = new Terminal({
      cursorBlink: false,
      disableStdin: true,
      convertEol: true,
      scrollback: 0,
      fontSize: 14,
      fontFamily: "'Cascadia Mono', 'DejaVu Sans Mono', monospace",
      theme: { background: "#1b1b1b", foreground: "#d4d4d4", cursor: "#1b1b1b" }
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("term"));
    fit.fit();

    let ws = null;
    let resizeTimer = null;

    // The banner covers the top of the terminal while the WebSocket is
    // closed. It is browser-side on purpose: if the dashboard server stops,
    // no frame (and so no server-rendered header) can be sent at all.
    const netBanner = document.getElementById("net");
    function setLink(up) {
      if (netBanner) netBanner.style.display = up ? "none" : "block";
    }

    function connect() {
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      fit.fit();
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(
        proto + "://" + location.host + "/ws?cols=" + term.cols + "&rows=" + term.rows
      );
      ws.onmessage = (event) => term.write(event.data);
      ws.onopen = () => setLink(true);
      ws.onclose = () => { setLink(false); setTimeout(connect, 1000); };
    }

    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(connect, 250);
    });
    connect();
  }
</script>
</body>
</html>
"""


class WebServer:
    """Minimal display-only web endpoint for the dashboard.

    `/` serves the xterm.js page; `/ws?cols=N&rows=M` sends the current Rich
    frame as ANSI text whenever it changes. No input is accepted, and a browser
    resize simply reconnects with the new terminal size.
    """

    # Used in the fallback frame when a render raises; subclasses (YAMD) can
    # name themselves without copying the broadcast loop.
    RENDER_ERROR_PREFIX = "YAMD"

    def __init__(self, app: "YAMDApplication", host: str, port: int):
        self.app = app
        self.host = host
        self.port = port
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._clients: dict[web.WebSocketResponse,
                            tuple[int, int, Optional[str]]] = {}
        # Wakes _broadcast for an immediate frame on connect or on a client
        # message; otherwise it sleeps for the app's idle interval.
        self._wake = asyncio.Event()

    async def start(self) -> None:
        webapp = web.Application()
        webapp.router.add_get("/", self._index)
        webapp.router.add_get("/ws", self._ws)
        self._add_routes(webapp)
        self._runner = web.AppRunner(webapp, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._running = True
        self._task = asyncio.create_task(self._broadcast())

    def _add_routes(self, webapp: web.Application) -> None:
        """Hook for subclasses to add routes before the runner starts."""

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=WEB_HTML, content_type="text/html")

    @staticmethod
    def _size(request: web.Request) -> tuple[int, int]:
        def _int(name: str, default: int) -> int:
            try:
                return int(request.query.get(name, default))
            except (TypeError, ValueError):
                return default

        cols = min(max(_int("cols", 120), WEB_MIN_COLS), WEB_MAX_COLS)
        rows = min(max(_int("rows", 40), WEB_MIN_ROWS), WEB_MAX_ROWS)
        return cols, rows

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        cols, rows = self._size(request)
        self._clients[ws] = (cols, rows, None)
        self._wake.set()
        try:
            # Display-only: drain any client frames without acting on them.
            async for _ in ws:
                pass
        finally:
            self._clients.pop(ws, None)
        return ws

    async def _broadcast(self) -> None:
        """Render once per unique terminal size per tick and send changes."""
        while self._running:
            # Keep --debug useful in --web-only mode even with no browser
            # connected; the app dedupes by snapshot timestamp.
            self.app._log_debug_for_web()
            if self._clients:
                cache: dict[tuple[int, int], str] = {}
                for ws, (cols, rows, last) in list(self._clients.items()):
                    key = (cols, rows)
                    if key not in cache:
                        try:
                            cache[key] = self.app.render_ansi(cols, rows)
                        except Exception as e:  # never kill the server loop
                            cache[key] = f"{self.RENDER_ERROR_PREFIX} render error: {e}"
                    frame = cache[key]
                    if frame == last:
                        continue
                    # Home + frame + erase-below; clear once on connect and
                    # hide the cursor (the page is display-only).
                    prefix = ("\x1b[2J\x1b[?25l\x1b[H" if last is None
                              else "\x1b[H")
                    try:
                        await ws.send_str(prefix + frame + "\x1b[0J")
                        self._clients[ws] = (cols, rows, frame)
                    except Exception:
                        self._clients.pop(ws, None)
                        try:
                            await ws.close()
                        except Exception:
                            pass
            fps = max(1, int(getattr(self.app, "web_fps", WEB_FPS)))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0 / fps)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "2.2.0"

# How long the detail follows the active target before moving on, and how long
# a manual TAB selection is kept before automatic rotation resumes.
DEFAULT_CYCLE = 5.0
DEFAULT_HOLD = 15.0

# Percentage of the terminal taken by the target list in the split view.
DEFAULT_SPLIT = 35

# A detail body keeps at least this many request rows before the fixed
# right-hand panel (Strata Hardware, Halogen Cache & Pool) is dropped. The
# single-server dashboards always show those panels; YAMD's Split view can
# leave the detail body much shorter, where Requests is the more useful one.
MIN_DETAIL_REQUEST_ROWS = 5

# Default single target when neither a config file nor -t/--server is given.
DEFAULT_TARGET = "http://127.0.0.1:8731"

VIEWS = ("monitor", "detail", "split")
# `auto` is a config/CLI convenience resolved once the target count is known;
# YAMDApplication itself only ever receives a concrete view.
CONFIG_VIEWS = VIEWS + ("auto",)
DEFAULT_AUTO_SPLIT_MAX = 3
KINDS = ("strata", "halogen", "auto")

STATE_STYLE = {
    "IDLE": "green",
    "BUSY": "bold cyan",
    "QUEUED": "dark_orange",
    "OFFLINE": "bold red",
    "DEGRADED": "dark_orange",
    "NO ENGINE": "bold red",
    "SLEEPING": "dim",
}

ACTIVE_STATES = ("BUSY", "QUEUED")


# ---------------------------------------------------------------------------
# Target specification and configuration
# ---------------------------------------------------------------------------

@dataclass
class TargetSpec:
    """One monitored server, as read from the config or the command line."""

    name: str
    kind: str                    # strata | halogen | auto
    url: str
    api_key: Optional[str] = None
    local: Optional[bool] = None

    def normalize(self) -> "TargetSpec":
        self.url = _normalize_server(self.url)
        self.kind = (self.kind or "auto").strip().lower()
        if self.kind not in KINDS:
            self.kind = "auto"
        self.name = (self.name or "").strip() or _default_name(self.url, self.kind)
        return self


@dataclass
class TargetRuntime:
    """A TargetSpec plus its collector and the small amount of client state."""

    spec: TargetSpec
    collector: object
    local: bool = False
    last_used_ts: float = 0.0
    was_active: bool = False
    # Newest digest timestamp seen so far. Used to notice completions without
    # trusting the (possibly skewed) clock of a remote server; see
    # _refresh_activity().
    last_digest_ts: Optional[object] = None
    # kind="auto" bookkeeping: re-probe once if the startup probe could not
    # decide (for example because the target was still down).
    auto: bool = False
    auto_confident: bool = True
    rechecked: bool = False
    # Requests panel: the key of the top visible row while scrolled, and the
    # number of rows the last frame had room for (used as the page step).
    req_anchor: Optional[str] = None
    req_page_rows: int = DEFAULT_REQ_ROWS


@dataclass
class TargetView:
    """A snapshot adapted to the multi-target vocabulary."""

    runtime: TargetRuntime
    snapshot: object
    connected: bool
    badge: str
    model: str
    live: str
    digest: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def name(self) -> str:
        return self.runtime.spec.name

    @property
    def kind(self) -> str:
        return self.runtime.spec.kind

    @property
    def url(self) -> str:
        return self.runtime.spec.url

    @property
    def active(self) -> bool:
        return self.badge in ACTIVE_STATES


def _default_name(url: str, kind: str = "") -> str:
    """A readable target name derived from the URL."""
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        host = parsed.hostname or url
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        host, port = url, ""
    label = "halogen" if kind == "halogen" else ("strata" if kind == "strata" else "")
    return f"{label}-{host}{port}" if label else f"{host}{port}"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yamd" / "targets.toml"


def resolve_config_path(path: Optional[Path]) -> Path:
    """Return the config path to read, preferring TOML then JSON."""
    if path is not None:
        return path
    candidate = default_config_path()
    if not candidate.exists():
        alt = candidate.with_suffix(".json")
        if alt.exists():
            return alt
    return candidate


def _read_config_data(path: Path):
    """Parse a TOML or JSON config file. Missing files return None."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    try:
        import tomllib
    except ImportError as exc:  # Python 3.10
        raise SystemExit(
            f"{path}: TOML needs Python 3.11+ (tomllib); use a .json config"
        ) from exc
    return tomllib.loads(text)


def _read_config(path: Path) -> list[dict]:
    """Return the target list from a config file. Missing files return []."""
    data = _read_config_data(path)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    targets = data.get("target") or data.get("targets") or []
    return targets if isinstance(targets, list) else []


def load_config_file(path: Optional[Path]) -> list[TargetSpec]:
    """Load TargetSpecs from *path* (or the default config when None)."""
    path = resolve_config_path(path)
    specs: list[TargetSpec] = []
    for item in _read_config(path):
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("server") or item.get("address")
        if not url:
            continue
        kind = item.get("kind") or "auto"
        api_key = item.get("api_key")
        env = item.get("api_key_env")
        if api_key is None and env:
            api_key = os.environ.get(str(env))
        local = item.get("local")
        specs.append(TargetSpec(
            name=item.get("name") or "",
            kind=str(kind),
            url=str(url),
            api_key=api_key,
            local=bool(local) if local is not None else None,
        ).normalize())
    return specs


def load_web_config(path: Optional[Path]) -> dict:
    """Return the `[web]` table from the config file, or {}."""
    data = _read_config_data(resolve_config_path(path))
    if not isinstance(data, dict):
        return {}
    web = data.get("web")
    return web if isinstance(web, dict) else {}


def load_ui_config(path: Optional[Path]) -> dict:
    """Return the `[ui]` table from the config file, or {}.

    A top-level `view = "..."` is accepted as a shorthand for `[ui].view`.
    """
    data = _read_config_data(resolve_config_path(path))
    if not isinstance(data, dict):
        return {}
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
    if "view" not in ui and isinstance(data.get("view"), str):
        ui = {**ui, "view": data["view"]}
    return ui


@dataclass
class WebOptions:
    enabled: bool = False
    port: Optional[int] = None
    listen: str = "0.0.0.0"
    only: bool = False


def resolve_web_options(args, cfg: dict) -> WebOptions:
    """Merge `[web]` config with the command line.

    CLI wins. Config `port` alone does not enable the view (unlike `--port`);
    `enabled` / `only` are the switches, and `--no-web` overrides both.
    """
    cfg_enabled = bool(cfg.get("enabled", False))
    cfg_only = bool(cfg.get("only", cfg.get("web_only", False)))
    cfg_port = cfg.get("port")
    cfg_listen = str(cfg.get("listen") or cfg.get("host") or "0.0.0.0")

    if cfg_port is not None:
        try:
            cfg_port = int(cfg_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("web.port must be an integer") from exc
        if not 1 <= cfg_port <= 65535:
            raise ValueError("web.port must be between 1 and 65535")

    enabled = args.web if args.web is not None else cfg_enabled
    only = args.web_only if args.web_only is not None else cfg_only

    if args.web is True:
        enabled = True
        # Explicit --web asks for terminal + browser; --web-only still wins.
        if args.web_only is not True:
            only = False
    elif args.web is False:
        enabled = False
        only = False

    if args.web is False and args.web_only is True:
        raise ValueError("--no-web cannot be combined with --web-only")
    if args.web_only is True:
        enabled = True
        only = True

    port = args.port if args.port is not None else cfg_port
    listen = args.listen if args.listen is not None else cfg_listen

    if args.port is not None and args.web is not False:
        # --port implies --web, but an explicit --no-web still wins.
        enabled = True
    if only:
        enabled = True

    return WebOptions(enabled=enabled, port=port, listen=listen, only=only)


def resolve_ui_options(args, cfg: dict, n_targets: int) -> str:
    """Pick the startup view: CLI > `[ui]` config > monitor.

    `view = "auto"` starts in split for a short target list and in monitor for
    a long one; `auto_split_max` (default 3) is the cut-off. The application
    itself only ever receives a concrete view, so the web/keys switching code
    keeps its existing monitor/detail/split vocabulary.
    """
    raw = args.view if args.view is not None else cfg.get("view", "monitor")
    view = str(raw or "monitor").strip().lower()
    if view not in CONFIG_VIEWS:
        raise ValueError(
            f"ui.view must be one of {', '.join(CONFIG_VIEWS)}")
    if view != "auto":
        return view

    limit = cfg.get("auto_split_max", DEFAULT_AUTO_SPLIT_MAX)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("ui.auto_split_max must be an integer") from exc
    if limit < 1:
        raise ValueError("ui.auto_split_max must be at least 1")
    return "split" if n_targets <= limit else "monitor"


def resolve_blink(cfg: dict) -> bool:
    """Return the `[ui] blink` flag (default False).

    When off, Strata's >95% context warning is a solid red bar like Halogen's,
    and the idle redraw rate is 1 Hz; when on, it blinks at 2 Hz.
    """
    raw = cfg.get("blink", False)
    if isinstance(raw, bool):
        return raw
    raise ValueError("ui.blink must be true or false")


def parse_target_arg(text: str) -> TargetSpec:
    """Parse `NAME=KIND@URL`, `KIND@URL`, `NAME=URL` or a bare URL."""
    text = text.strip()
    name = ""
    kind = "auto"
    url = text

    if "=" in text:
        left, right = text.split("=", 1)
        name, url = left.strip(), right.strip()

    if "@" in url:
        left, right = url.split("@", 1)
        if left.strip().lower() in KINDS:
            kind, url = left.strip().lower(), right.strip()
        elif not name:
            name, url = left.strip(), right.strip()
        else:
            # NAME=something@url: keep NAME and let the left side be a kind.
            if left.strip().lower() in KINDS:
                kind = left.strip().lower()
            url = right.strip()

    return TargetSpec(name=name, kind=kind, url=url).normalize()


def build_specs(args) -> list[TargetSpec]:
    """Merge the config file, --server and repeated -t options."""
    specs: list[TargetSpec] = []

    if args.server:
        # A single --server is the classic one-target invocation: it replaces
        # the config list unless -t targets were also supplied.
        specs = [TargetSpec(
            name=args.name or "",
            kind=args.kind,
            url=args.server,
            api_key=args.api_key,
        ).normalize()]
    else:
        if args.config and not Path(args.config).exists():
            raise SystemExit(f"config not found: {args.config}")
        specs = load_config_file(Path(args.config) if args.config else None)

    for raw in args.target:
        spec = parse_target_arg(raw)
        # Same name replaces an earlier target in place, so -t can override a
        # config entry without reordering the list.
        for i, existing in enumerate(specs):
            if existing.name == spec.name:
                specs[i] = spec
                break
        else:
            specs.append(spec)

    if not specs:
        specs = [TargetSpec(
            name="",
            kind="halogen",
            url=DEFAULT_TARGET,
            api_key=args.api_key,
        ).normalize()]

    for spec in specs:
        if spec.api_key is None:
            spec.api_key = args.api_key

    # Keep names unique even if the user repeated a bare URL.
    seen: dict[str, int] = {}
    for spec in specs:
        seen[spec.name] = seen.get(spec.name, 0) + 1
        if seen[spec.name] > 1:
            spec.name = f"{spec.name}-{seen[spec.name]}"
    return specs


async def detect_kind(spec: TargetSpec) -> Optional[str]:
    """Best-effort Strata/Halogen detection for kind='auto'.

    Halogen answers /cache with JSON; Strata's /metrics is a JSON object while
    Halogen's is Prometheus text. /health is the last resort. Returns None when
    nothing answered, so the caller can fall back to Halogen and re-probe once
    the target comes online.
    """
    headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else None
    timeout = aiohttp.ClientTimeout(total=3.0, connect=1.5, sock_read=2.0)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{spec.url}/cache", headers=headers) as resp:
                    ctype = resp.headers.get("Content-Type", "")
                    if resp.status == 200 and "json" in ctype:
                        return "halogen"
            except Exception:
                pass

            try:
                async with session.get(f"{spec.url}/metrics", headers=headers) as resp:
                    if resp.status == 200:
                        body = await resp.text()
                        head = body.lstrip()[:1]
                        if head in ("{", "["):
                            return "strata"
                        if ("llamacpp:" in body or "halogen:" in body
                                or "# HELP" in body or "# TYPE" in body):
                            return "halogen"
            except Exception:
                pass

            try:
                async with session.get(f"{spec.url}/health", headers=None) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict):
                            if any(k in data for k in
                                   ("slots", "in_flight", "capability_probe")):
                                return "halogen"
                            if "loaded" in data:
                                return "strata"
            except Exception:
                pass
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Snapshot -> TargetView adapters
# ---------------------------------------------------------------------------

def _strata_badge(collector) -> str:
    if not collector.is_connected():
        return "OFFLINE"
    if collector.is_sleeping():
        return "SLEEPING"
    # Delegate the rest of the state mapping to the native Strata badge so
    # both views keep the same QUEUED/SLEEPING/READING vocabulary, then
    # translate to YAMD's (READING and GENERATING are both "busy").
    badge = _strata_state_badge(collector.get_snapshot())
    return {"READING": "BUSY", "GENERATING": "BUSY"}.get(badge, badge)


def _halogen_badge(collector) -> str:
    if not collector.is_connected():
        return "OFFLINE"
    return _halogen_state_badge(collector.get_snapshot())


def _newest_request(snapshot) -> dict:
    """Newest request row from a snapshot, or {} when the ring is empty."""
    requests = getattr(snapshot, "requests", None) or []
    return (requests[0] or {}) if requests else {}


def _strata_digest(snapshot) -> dict:
    """Normalise Strata's newest request-ring entry."""
    last = _newest_request(snapshot)
    if not last:
        return {}
    prompt = last.get("prompt_tokens")
    cached = last.get("reused") or 0
    prefill = last.get("prefill_tok_s")
    if prefill is None and prompt:
        prompt_ms = last.get("prompt_ms")
        if prompt_ms and prompt_ms > 0:
            prefill = max(0, prompt - cached) / (prompt_ms / 1000.0)
    return {
        "time": last.get("time"),
        "requests": 1,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": last.get("output_tokens"),
        "prefill_tok_s": prefill,
        "decode_tok_s": last.get("decode_tok_s"),
        "draft_hit": None,
        "finish": last.get("finish"),
    }


def _halogen_digest(snapshot) -> dict:
    """Halogen's collector already stores compatible completion windows."""
    first = _newest_request(snapshot)
    if not first:
        return {}
    return {
        "time": first.get("time"),
        "requests": first.get("requests") or 1,
        "prompt_tokens": first.get("prompt_tokens"),
        "cached_tokens": first.get("cached_tokens") or 0,
        "output_tokens": first.get("output_tokens"),
        "prefill_tok_s": first.get("prefill_tok_s"),
        "decode_tok_s": first.get("decode_tok_s"),
        "draft_hit": first.get("draft_hit"),
        "finish": None,
    }


def make_view(runtime: TargetRuntime) -> TargetView:
    """Adapt one collector snapshot to the shared multi-target vocabulary."""
    collector = runtime.collector
    snapshot = collector.get_snapshot()
    connected = collector.is_connected()
    error = collector.get_last_error()

    if runtime.spec.kind == "strata":
        badge = _strata_badge(collector)
        digest = _strata_digest(snapshot)
        model = getattr(snapshot, "model", "") or ""
        active = 1 if getattr(snapshot, "state", "idle") in (
            "reading", "generating") else 0
        slots = max(1, getattr(snapshot, "concurrency_serving", 1) or 1)
        queued = getattr(snapshot, "queued", 0) or 0
    else:
        badge = _halogen_badge(collector)
        digest = _halogen_digest(snapshot)
        model = getattr(snapshot, "model", "") or ""
        active = getattr(snapshot, "in_flight", 0) or 0
        slots = getattr(snapshot, "slots", 0) or 0
        queued = getattr(snapshot, "queued", 0) or 0

    if connected:
        live = f"{active}/{slots} q{queued}"
    else:
        live = "—"

    return TargetView(
        runtime=runtime,
        snapshot=snapshot,
        connected=connected,
        badge=badge,
        model=model,
        live=live,
        digest=digest,
        error=error,
    )


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _clock(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "—"


def _rate(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}t/s"
    except (TypeError, ValueError):
        return "—"


def _digest_line(digest: dict) -> str:
    if not digest:
        return "no requests yet"
    prompt = _kfmt(digest.get("prompt_tokens"))
    cached = _kfmt(digest.get("cached_tokens"))
    output = _kfmt(digest.get("output_tokens"))
    pp = _num(digest.get("prefill_tok_s"), 1)
    tg = _num(digest.get("decode_tok_s"), 1)
    return (f"{_clock(digest.get('time'))}  {prompt} (cached {cached}) → {output}"
            f"  · PP {pp} / TG {tg} t/s")


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _engine_version_label(kind: str, snapshot, term_width: int) -> str:
    """Version text for the Engine panel title, shortened to fit.

    The single-server dashboards carried this in their header. In YAMD the
    header is shared by every target, so the version moves next to the Engine
    facts where it still has an obvious owner.
    """
    inner = _col_widths(term_width)[0]
    room = max(4, inner - len("Engine · ") - 2)

    if kind == "strata":
        value = getattr(snapshot, "engine_version", "") or ""
        if not value:
            return ""
        for candidate in (f"Strata {value}", value):
            if len(candidate) <= room:
                return candidate
        return ""

    api = getattr(snapshot, "api_version", "") or ""
    engine = getattr(snapshot, "engine_version", "") or ""
    if not api and not engine:
        return ""

    if api and engine and api != engine:
        candidates = [
            f"Halogen {api} · engine {engine}",
            f"API {api} · engine {engine}",
            f"{engine} · API {api}",
            engine,
        ]
    else:
        value = api or engine
        candidates = [f"Halogen {value}", value]
    for candidate in candidates:
        if len(candidate) <= room:
            return candidate
    return ""


def _make_engine_panel(kind: str, snapshot, term_width: int) -> Panel:
    """The Strata/Halogen Engine panel with the version in its title."""
    if kind == "strata":
        panel = strata_make_engine_panel(snapshot, term_width)
    else:
        panel = halogen_make_engine_panel(snapshot, term_width)
    label = _engine_version_label(kind, snapshot, term_width)
    if label:
        panel.title = f"[bold]Engine · {label}[/bold]"
    return panel


# ---------------------------------------------------------------------------
# Keyboard reader
# ---------------------------------------------------------------------------

class KeyReader:
    """Read single key presses without line buffering.

    POSIX uses termios cbreak + select; Windows falls back to msvcrt. The
    original terminal attributes are restored by stop() (called from a finally
    block), so Ctrl-C and normal exit both leave the shell usable.
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._saved = None
        self._enabled = False

    def start(self) -> None:
        if os.name != "posix":
            return
        try:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            if not os.isatty(self._fd):
                return
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._enabled = True
        except Exception:
            self._enabled = False

    def stop(self) -> None:
        if not self._enabled or self._fd is None:
            return
        try:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        finally:
            self._enabled = False

    def read(self) -> list[str]:
        if os.name == "posix":
            return self._read_posix()
        return self._read_windows()

    def _read_posix(self) -> list[str]:
        if not self._enabled or self._fd is None:
            return []
        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
            if not ready:
                return []
            data = os.read(self._fd, 1024)
            return self.parse_keys(data)
        except (OSError, ValueError):
            return []

    @staticmethod
    def _read_windows() -> list[str]:
        try:
            import msvcrt
        except ImportError:
            return []
        keys: list[str] = []
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    code = msvcrt.getwch()
                    keys.append({"H": "up", "P": "down",
                                 "K": "left", "M": "right",
                                 "I": "pgup", "Q": "pgdn",
                                 "G": "home", "O": "end"}.get(code, ""))
                continue
            keys.extend(KeyReader.parse_keys(ch.encode("utf-8", "ignore")))
        return [k for k in keys if k]

    @staticmethod
    def parse_keys(data: bytes) -> list[str]:
        """Decode a raw byte chunk into semantic key names."""
        keys: list[str] = []
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == 0x1B:                       # ESC / escape sequence
                four = data[i:i + 4]
                if four in (b"\x1b[5~", b"\x1b[6~", b"\x1b[1~", b"\x1b[4~",
                            b"\x1b[7~", b"\x1b[8~"):
                    keys.append({b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn",
                                 b"\x1b[1~": "home", b"\x1b[4~": "end",
                                 b"\x1b[7~": "home", b"\x1b[8~": "end"}[four])
                    i += 4
                    continue
                seq = data[i:i + 3]
                if seq == b"\x1b[Z":
                    keys.append("shift_tab")
                    i += 3
                    continue
                if seq in (b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D",
                           b"\x1b[H", b"\x1b[F"):
                    keys.append({b"\x1b[A": "up", b"\x1b[B": "down",
                                 b"\x1b[C": "right", b"\x1b[D": "left",
                                 b"\x1b[H": "home", b"\x1b[F": "end"}[seq])
                    i += 3
                    continue
                if data[i:i + 2] == b"\x1b[":
                    # Unknown CSI: consume the introducer and the final byte.
                    i += 2
                    while i < len(data) and not (0x40 <= data[i] <= 0x7E):
                        i += 1
                    i += 1
                    continue
                i += 1
                continue
            if ch == 0x09:
                keys.append("tab")
                i += 1
                continue
            if ch in (0x0A, 0x0D):
                keys.append("enter")
                i += 1
                continue
            if ch == 0x03:
                keys.append("quit")
                i += 1
                continue
            if ch < 0x20:
                i += 1
                continue
            # Decode one UTF-8 character (only ASCII keys matter here).
            try:
                keys.append(bytes([ch]).decode("utf-8").lower())
                i += 1
            except UnicodeDecodeError:
                i += 1
        return keys


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def _web_html() -> str:
    """YAMD's browser page: the xterm.js view plus a control bar.

    The base page is display-only (`disableStdin: true`, no focus). The
    replacements below enable keyboard input, send it over the existing
    WebSocket, and add buttons for the same operations plus browser-local
    font-size controls. Quit is deliberately not exposed to the browser.

    Every anchor is a literal from `WEB_HTML`; fail loudly if upstream
    changes one, because silently serving a display-only page (or one without
    the toolbar) is worse than a startup error.
    """
    html = WEB_HTML

    def replace(old: str, new: str) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError(
                "WEB_HTML changed: YAMD's web controls need an update "
                f"(missing anchor: {old!r})")
        html = html.replace(old, new)

    replace("<title>YAMD</title>", "<title>YAMD</title>")
    replace(
        "  #term { width: 100vw; height: 100vh; padding: 6px; box-sizing: border-box; }",
        "  #term { width: 100vw; height: calc(100vh - 34px); padding: 6px;"
        " box-sizing: border-box; }",
    )
    replace("</style>", """  #bar { position: fixed; left: 0; right: 0; bottom: 0; height: 34px;
         display: flex; gap: 6px; align-items: center; padding: 0 8px;
         box-sizing: border-box; background: #222; border-top: 1px solid #444;
         font-family: monospace; font-size: 13px; color: #bbb; overflow: hidden; }
  #bar button { background: #333; color: #ddd; border: 1px solid #555;
                border-radius: 3px; padding: 2px 10px; cursor: pointer;
                flex: 0 0 auto; }
  #bar button:hover { background: #444; }
  #bar button:disabled { opacity: 0.45; cursor: default; }
  #bar #fontsize { flex: 0 0 auto; min-width: 2ch; text-align: center;
                   color: #ddd; font-variant-numeric: tabular-nums; }
  #bar #mode { margin-left: auto; flex: 0 1 auto; min-width: 0;
               color: #9cdcfe; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
</style>""")
    replace('<div id="term"></div>', """<div id="term"></div>
<div id="bar">
  <button data-cmd="prev" title="Previous target (Shift-TAB)">◀ target</button>
  <button data-cmd="next" title="Next target (TAB)">target ▶</button>
  <button data-cmd="monitor" title="Monitor view (m)">Monitor</button>
  <button data-cmd="detail" title="Detail view (d)">Detail</button>
  <button data-cmd="split" title="Split view (s)">Split</button>
  <button data-cmd="auto" title="Toggle auto rotation (a)">Auto</button>
  <button data-cmd="req_newer" title="Newer requests (PgUp)">Req ▲</button>
  <button data-cmd="req_older" title="Older requests (PgDn)">Req ▼</button>
  <button data-font="-1" title="Smaller font">A−</button>
  <span id="fontsize" title="Terminal font size">14</span>
  <button data-font="1" title="Larger font">A+</button>
  <button data-fullscreen="1" id="fullscreen"
          title="Toggle fullscreen (Esc exits)">Full</button>
  <span id="mode" title="Rotation mode">auto</span>
</div>""")
    replace("      disableStdin: true,", "      disableStdin: false,")
    replace(
        "    let ws = null;\n    let resizeTimer = null;",
        """    let ws = null;
    let resizeTimer = null;

    // Font-size controls live entirely in the browser: change xterm's
    // fontSize, let it recompute the cell size, then reconnect so the server
    // renders at the new cols/rows. The choice is remembered per browser.
    const FONT_MIN = 8;
    const FONT_MAX = 28;

    function setFontLabel(size) {
      const label = document.getElementById("fontsize");
      if (label) label.textContent = String(size);
    }

    function stepFont(delta) {
      const current = Number(term.options.fontSize) || 14;
      const next = Math.max(FONT_MIN, Math.min(FONT_MAX, current + delta));
      if (next === current) return;
      term.options.fontSize = next;
      setFontLabel(next);
      try { localStorage.setItem("yamd.fontSize", String(next)); } catch (e) {}
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(connect, 60);
    }

    try {
      const saved = Number(localStorage.getItem("yamd.fontSize"));
      if (Number.isFinite(saved) && saved >= FONT_MIN && saved <= FONT_MAX) {
        term.options.fontSize = saved;
      }
    } catch (e) {}
    setFontLabel(Number(term.options.fontSize) || 14);

    // Fullscreen: the bar stays visible and the terminal fills the rest of
    // the viewport. Refit and reconnect afterwards so the server renders the
    // new cols/rows (a fullscreen change does not always fire `resize`).
    function isFullscreen() {
      return document.fullscreenElement ||
             document.webkitFullscreenElement || null;
    }

    function updateFullscreenButton() {
      const btn = document.getElementById("fullscreen");
      if (btn) btn.textContent = isFullscreen() ? "Exit" : "Full";
    }

    function toggleFullscreen() {
      const root = document.documentElement;
      try {
        if (isFullscreen()) {
          const exit = document.exitFullscreen ||
                       document.webkitExitFullscreen;
          if (exit) {
            const p = exit.call(document);
            if (p && p.catch) p.catch(() => {});
          }
        } else {
          const enter = root.requestFullscreen ||
                        root.webkitRequestFullscreen;
          if (enter) {
            const p = enter.call(root);
            if (p && p.catch) p.catch(() => {});
          }
        }
      } catch (e) {}
    }

    function fullscreenChanged() {
      updateFullscreenButton();
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(connect, 100);
    }

    document.addEventListener("fullscreenchange", fullscreenChanged);
    document.addEventListener("webkitfullscreenchange", fullscreenChanged);
    updateFullscreenButton();

    if (!(document.documentElement.requestFullscreen ||
          document.documentElement.webkitRequestFullscreen)) {
      const btn = document.getElementById("fullscreen");
      if (btn) {
        btn.disabled = true;
        btn.title = "Fullscreen is not available in this browser";
      }
    }

    // The rotation mode lives in the HTML bar now, so poll the small /state
    // endpoint instead of rendering the terminal key-help line in the frame.
    const modeLabel = document.getElementById("mode");
    async function refreshState() {
      try {
        const resp = await fetch("/state", {cache: "no-store"});
        if (!resp.ok) return;
        const state = await resp.json();
        if (modeLabel && state.mode) modeLabel.textContent = state.mode;
      } catch (e) {}
    }
    refreshState();
    setInterval(refreshState, 1000);

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
      }
    }

    term.onData((data) => {
      send({type: "input", data: data});
      setTimeout(refreshState, 150);
    });
    term.attachCustomKeyEventHandler((event) => {
      // Plain keys, including Tab, reach xterm.js; Ctrl/Alt/Meta chords stay
      // with the browser (there is no quit shortcut to forward anyway).
      return !(event.ctrlKey || event.altKey || event.metaKey);
    });
    document.getElementById("term").addEventListener("click", () => term.focus());
    document.querySelectorAll("#bar button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.font) {
          stepFont(Number(btn.dataset.font));
        } else if (btn.dataset.fullscreen) {
          toggleFullscreen();
        } else {
          send({type: "command", command: btn.dataset.cmd});
          setTimeout(refreshState, 150);
        }
        term.focus();
      });
    });""",
    )
    replace(
        "      ws.onopen = () => setLink(true);",
        "      ws.onopen = () => { setLink(true); term.focus(); };",
    )
    return html


class YAMDWebServer(WebServer):
    """The xterm.js server with YAMD controls.

    The browser can switch view/target and toggle rotation. `q`/Ctrl-C are
    filtered out on purpose: a web client must never stop the dashboard.
    """

    RENDER_ERROR_PREFIX = "YAMD"

    def _add_routes(self, webapp: web.Application) -> None:
        # The browser bar polls this for the rotation mode that the terminal
        # key-help line used to carry.
        webapp.router.add_get("/state", self._state)

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.app.web_state())

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=_web_html(), content_type="text/html")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        cols, rows = self._size(request)
        self._clients[ws] = (cols, rows, None)
        self._wake.set()
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    self.app.handle_web_message(msg.data)
                    # Redraw now rather than waiting for the idle tick.
                    self._wake.set()
        finally:
            self._clients.pop(ws, None)
        return ws


class YAMDApplication:
    def __init__(
        self,
        specs: list[TargetSpec],
        req_rows: Optional[int] = None,
        debug_file: Optional[str] = None,
        cycle: float = DEFAULT_CYCLE,
        hold: float = DEFAULT_HOLD,
        split_pct: int = DEFAULT_SPLIT,
        view: str = "monitor",
        blink: bool = False,
        force_local: Optional[bool] = None,
        web_port: Optional[int] = None,
        web_host: str = "0.0.0.0",
        web_only: bool = False,
    ) -> None:
        self.specs = specs
        self.req_rows = req_rows
        self.cycle = max(0.5, float(cycle))
        self.hold = max(0.0, float(hold))
        self.split_pct = min(70, max(15, int(split_pct)))
        self.view_mode = view if view in VIEWS else "monitor"
        self.force_local = force_local

        # Strata's >95% context blink is off by default: a solid red bar like
        # Halogen is enough. The idle redraw rate follows the setting: 1 Hz
        # without the blink, 2 Hz when it is enabled (the blink needs two
        # frames per second to be visible).
        self.blink = bool(blink)
        self.render_interval = 1.0 if not self.blink else 0.5
        self.web_fps = 1 if not self.blink else 2
        self._render_event = asyncio.Event()

        self._runtimes: list[TargetRuntime] = []

        self.console = Console()
        self._running = True
        self._frame = 0
        self._selected = 0
        self._scroll = 0
        self._auto = True
        self._manual_until = 0.0
        self._next_rotate = 0.0
        self._had_active = False
        self._seeded = False
        self._rotation_error: Optional[str] = None

        self.web_port = web_port
        self.web_host = web_host
        self.web_only = web_only

        self._debug_path = debug_file
        self._debug_fp = None  # type: ignore[assignment]
        self._debug_seen: dict[int, float] = {}

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _handle_signal(self, signum, frame) -> None:
        self._running = False

    def _make_collector(self, spec: TargetSpec) -> tuple[object, bool]:
        if spec.kind == "strata":
            # The display cap is self.req_rows; the collector always seeds the
            # full request ring once and then polls the server's cheap default
            # window. Strata ignores numeric request limits.
            collector = StrataCollector(
                server_url=spec.url,
                poll_interval=STRATA_POLL_INTERVAL,
                api_key=spec.api_key,
                req_rows=DEFAULT_REQ_ROWS,
                seed_all=True,
            )
            return collector, False

        local = spec.local
        if local is None:
            local = (self.force_local if self.force_local is not None
                     else _is_local_server(spec.url))
        collector = HalogenCollector(
            server_url=spec.url,
            poll_interval=HALOGEN_POLL_INTERVAL,
            api_key=spec.api_key,
            sample_host=bool(local),
        )
        return collector, bool(local)

    @staticmethod
    async def _resolve_kind(spec: TargetSpec) -> Optional[str]:
        if spec.kind != "auto":
            return spec.kind
        return await detect_kind(spec)

    async def _prepare_targets(self) -> None:
        # Probe auto targets concurrently: each probe can wait up to 3 s, so
        # doing them one by one would delay startup by that much per target.
        kinds = await asyncio.gather(
            *(self._resolve_kind(spec) for spec in self.specs))
        for spec, kind in zip(self.specs, kinds):
            was_auto = spec.kind == "auto"
            if was_auto:
                # An unreachable target falls back to Halogen and is probed
                # again once it answers; see _recheck_auto_targets().
                spec.kind = kind or "halogen"
            collector, local = self._make_collector(spec)
            self._runtimes.append(TargetRuntime(
                spec=spec, collector=collector, local=local,
                auto=was_auto, auto_confident=bool(kind),
            ))

    async def _recheck_auto_targets(self) -> None:
        """Re-probe an auto target whose startup detection could not decide."""
        for rt in self._runtimes:
            if (rt.auto_confident or rt.rechecked
                    or not rt.collector.is_connected()):
                continue
            rt.rechecked = True
            kind = await detect_kind(rt.spec)
            if kind and kind != rt.spec.kind:
                await self._switch_kind(rt, kind)

    async def _switch_kind(self, rt: TargetRuntime, kind: str) -> None:
        """Rebuild a runtime's collector after a late auto-detection."""
        old = rt.collector
        old_kind = rt.spec.kind
        rt.spec.kind = kind
        collector = None
        try:
            collector, local = self._make_collector(rt.spec)
            await collector.start()
        except BaseException:
            rt.spec.kind = old_kind
            if collector is not None:
                try:
                    await collector.stop()
                except Exception:
                    pass
            raise
        await old.stop()
        rt.collector = collector
        rt.local = local

    def _views(self) -> list[TargetView]:
        """Adapt every collector, isolating a target that fails to adapt.

        A malformed response on one server must not take the dashboard down
        (or stop the rotation loop) for the other targets.
        """
        views: list[TargetView] = []
        for rt in self._runtimes:
            try:
                views.append(make_view(rt))
            except Exception as exc:
                try:
                    snapshot = rt.collector.get_snapshot()
                except Exception:
                    snapshot = None
                views.append(TargetView(
                    runtime=rt,
                    snapshot=snapshot,
                    connected=False,
                    badge="OFFLINE",
                    model=getattr(snapshot, "model", "") or "",
                    live="—",
                    digest={},
                    error=f"adapter error: {type(exc).__name__}: {exc}",
                ))
        return views

    def _selected_view(self, views: list[TargetView]) -> Optional[TargetView]:
        if not views:
            return None
        self._selected = max(0, min(self._selected, len(views) - 1))
        return views[self._selected]

    # ------------------------------------------------------------------
    # Activity and rotation
    # ------------------------------------------------------------------

    def _refresh_activity(self, views: list[TargetView], now: float) -> None:
        for i, view in enumerate(views):
            rt = self._runtimes[i]
            digest_ts = view.digest.get("time") if view.digest else None
            if digest_ts is not None:
                if rt.last_digest_ts is None:
                    # First sighting: remember the key but do not treat the
                    # server's (possibly skewed) clock as "used now".
                    rt.last_digest_ts = digest_ts
                elif digest_ts != rt.last_digest_ts:
                    # A new completion arrived; stamp it with our own clock so
                    # targets on hosts with different clock settings still
                    # compare correctly.
                    rt.last_digest_ts = digest_ts
                    rt.last_used_ts = max(rt.last_used_ts, now)
            if view.active:
                rt.last_used_ts = max(rt.last_used_ts, now)
                rt.was_active = True
            elif rt.was_active:
                # The request just finished: this is now the most recently
                # used target, which is where rotation stops when all go idle.
                rt.last_used_ts = max(rt.last_used_ts, now)
                rt.was_active = False

    def _tick_selection(self, now_mono: float, now_wall: float) -> None:
        previous = self._selected
        views = self._views()
        if not views:
            return
        self._refresh_activity(views, now_wall)
        active = [i for i, view in enumerate(views) if view.active]

        if self._manual_until and now_mono >= self._manual_until:
            self._manual_until = 0.0

        manual = now_mono < self._manual_until
        if not self._seeded:
            # Start on the target that ran last instead of blindly on #1.
            self._seeded = True
            if not active and self._auto and not manual:
                best = max(range(len(self._runtimes)),
                           key=lambda i: self._runtimes[i].last_used_ts)
                if self._runtimes[best].last_used_ts > 0:
                    self._selected = best

        if self._scroll_hold():
            # Reading older request rows: do not rotate away from them. Home
            # (or moving to an unscrolled target) releases the hold.
            if self._selected != previous:
                self._render_event.set()
            return

        if not self._auto or manual:
            if self._selected != previous:
                self._render_event.set()
            return

        if active:
            if not self._had_active:
                self._had_active = True
                if self._selected not in active:
                    self._selected = active[0]
                self._next_rotate = now_mono + self.cycle
            if self._selected not in active:
                self._selected = active[0]
                self._next_rotate = now_mono + self.cycle
            elif now_mono >= self._next_rotate and len(active) > 1:
                pos = active.index(self._selected)
                self._selected = active[(pos + 1) % len(active)]
                self._next_rotate = now_mono + self.cycle
        else:
            if self._had_active:
                # All idle: freeze on the target used last.
                best = max(range(len(self._runtimes)),
                           key=lambda i: self._runtimes[i].last_used_ts)
                if self._runtimes[best].last_used_ts > 0:
                    self._selected = best
                self._had_active = False
                self._next_rotate = 0.0

        if self._selected != previous:
            self._render_event.set()

    def _manual_select(self, index: int) -> None:
        if not self._runtimes:
            return
        self._selected = index % len(self._runtimes)
        # The mode text changes too (manual hold), so wake even for the same
        # target.
        self._render_event.set()
        if self.hold > 0:
            self._manual_until = time.monotonic() + self.hold
        else:
            self._auto = False

    def _scroll_requests(self, key: str) -> bool:
        """Move the Requests table by one row/page, or jump to an end.

        Only detail/split show the Requests panel, so the keys are deliberately
        ignored in monitor. The top row is anchored by its key, which keeps the
        view fixed when a new completion arrives at the head of the ring.
        """
        if self.view_mode not in ("detail", "split") or not self._runtimes:
            return False
        index = max(0, min(self._selected, len(self._runtimes) - 1))
        rt = self._runtimes[index]
        try:
            snapshot = rt.collector.get_snapshot()
        except Exception:
            return False
        requests = list(getattr(snapshot, "requests", None) or [])
        if not requests:
            rt.req_anchor = None
            return False

        visible = max(1, rt.req_page_rows or DEFAULT_REQ_ROWS)
        start = _request_offset(requests, visible, rt.req_anchor)
        max_start = max(0, len(requests) - visible)
        if key == "home":
            start = 0
        elif key == "end":
            start = max_start
        elif key in ("pgdn", "j"):
            step = visible if key == "pgdn" else 1
            start = min(max_start, start + step)
        else:                                   # pgup or k
            step = visible if key == "pgup" else 1
            start = max(0, start - step)

        rt.req_anchor = None if start == 0 else _request_row_key(requests[start])
        return True

    def _scroll_hold(self) -> bool:
        """True while the selected target's Requests table is scrolled."""
        if self.view_mode == "monitor" or not self._runtimes:
            return False
        index = max(0, min(self._selected, len(self._runtimes) - 1))
        return self._runtimes[index].req_anchor is not None

    def _handle_key(self, key: str) -> None:
        if key in ("quit", "q"):
            self._running = False
            return
        if key in ("pgup", "pgdn", "home", "end", "j", "k"):
            if self._scroll_requests(key):
                self._render_event.set()
            return
        self._render_event.set()
        if key == "tab":
            self._manual_select(self._selected + 1)
        elif key in ("shift_tab", "up"):
            self._manual_select(self._selected - 1)
        elif key == "down":
            self._manual_select(self._selected + 1)
        elif key == "m":
            self.view_mode = "monitor"
        elif key == "d":
            self.view_mode = "detail"
        elif key == "s":
            self.view_mode = "split"
        elif key == "a":
            self._auto = not self._auto
            self._manual_until = 0.0
            self._next_rotate = time.monotonic() + self.cycle
        elif len(key) == 1 and key.isdigit():
            index = int(key) - 1
            if 0 <= index < len(self._runtimes):
                self._manual_select(index)

    def handle_web_key(self, key: str) -> None:
        """Apply a key from a browser, but never the quit shortcuts."""
        if key in ("quit", "q"):
            return
        self._handle_key(key)

    def handle_web_command(self, command: str) -> None:
        """Apply a toolbar command from the browser."""
        self._render_event.set()
        if command == "next":
            self._manual_select(self._selected + 1)
        elif command == "prev":
            self._manual_select(self._selected - 1)
        elif command in VIEWS:
            self.view_mode = command
        elif command == "auto":
            self._auto = not self._auto
            self._manual_until = 0.0
            self._next_rotate = time.monotonic() + self.cycle
        elif command in ("req_newer", "req_older"):
            self._scroll_requests(
                "pgup" if command == "req_newer" else "pgdn")

    def handle_web_message(self, raw: str) -> None:
        """Handle one JSON text frame from an xterm.js client."""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "input":
            data = payload.get("data")
            if isinstance(data, str):
                for key in KeyReader.parse_keys(
                        data.encode("utf-8", "ignore")):
                    self.handle_web_key(key)
        elif kind == "command":
            command = payload.get("command")
            if isinstance(command, str):
                self.handle_web_command(command)

    async def _key_loop(self, reader: KeyReader) -> None:
        while self._running:
            for key in reader.read():
                self._handle_key(key)
            await asyncio.sleep(0.05)

    async def _rotation_loop(self) -> None:
        while self._running:
            try:
                await self._recheck_auto_targets()
                self._tick_selection(time.monotonic(), time.time())
                self._log_debug()
            except Exception as exc:
                # Never let one target (or a debug write) stop rotation. The
                # message is surfaced in the header instead of printing over
                # the Live screen.
                self._rotation_error = f"{type(exc).__name__}: {exc}"
            else:
                self._rotation_error = None
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------
    # Debug log
    # ------------------------------------------------------------------

    def _log_debug(self) -> None:
        """Append raw responses for every target, once per poll snapshot."""
        if self._debug_fp is None:
            return
        for rt in self._runtimes:
            current = rt.collector.get_snapshot()
            stamp = getattr(current, "timestamp", 0.0)
            if self._debug_seen.get(id(rt)) == stamp:
                continue
            self._debug_seen[id(rt)] = stamp
            record = {
                "ts": datetime.now().isoformat(),
                "target": rt.spec.name,
                "kind": rt.spec.kind,
                "url": rt.spec.url,
            }
            if rt.spec.kind == "strata":
                health, metrics, status = rt.collector.get_raw_responses()
                if health is None and metrics is None and status is None:
                    # The collector has not answered yet; do not write a
                    # placeholder record on the first frame.
                    continue
                record["health"] = health
                record["metrics"] = metrics
                record["status"] = status
            else:
                health, metrics, cache = rt.collector.get_raw_responses()
                if health is None and metrics is None and cache is None:
                    continue
                record["health"] = health
                record["metrics"] = metrics
                record["cache"] = cache
                if rt.local:
                    record["host"] = {
                        "cpu_pct": current.host_cpu_pct,
                        "cpu_temp_c": current.host_cpu_temp,
                        "gpu_pct": current.host_gpu_pct,
                        "gpu_temp_c": current.host_gpu_temp,
                        "mem_used": current.host_mem_used,
                        "mem_total": current.host_mem_total,
                    }
            self._debug_fp.write(json.dumps(record, default=str) + "\n")

    def _log_debug_for_web(self) -> None:
        """Flush the debug log from the inherited web broadcast loop.

        YAMD logs every target and _log_debug already dedupes by snapshot, so
        the single-collector argument the base broadcast loop used is not needed here.
        """
        self._log_debug()

    # ------------------------------------------------------------------
    # Header, footer and status helpers
    # ------------------------------------------------------------------

    def _status_text(self, views: list[TargetView], compact: bool = False) -> Text:
        online = sum(1 for v in views if v.connected)
        active = sum(1 for v in views if v.active)
        if compact:
            text = Text(f"{online}/{len(views)} on", style="bold white")
            if active:
                text.append(f" · {active} busy", style="cyan")
            if self._rotation_error:
                text.append(" · err", style="bold red")
            return text
        text = Text()
        text.append(f"{len(views)} target{'s' if len(views) != 1 else ''}",
                    style="bold white")
        text.append(f" · {online} online", style="green")
        text.append(f" · {active} busy", style="cyan" if active else "dim")
        if self._rotation_error:
            text.append(" · rotation error", style="bold red")
        return text

    def _make_header(self, views: list[TargetView], width: int) -> Panel:
        # Table.grid columns run into each other when the three cells do not
        # fit, so pick the richest set of cells that fits by measured width.
        # The right-hand status is the part that changes; the tagline, clock
        # and version are dropped first.
        room = max(8, width - 4)       # panel borders + horizontal padding
        now = datetime.now()
        clock_full = Text(now.strftime("%Y-%m-%d %H:%M:%S"),
                          style="bold yellow")
        clock_short = Text(now.strftime("%H:%M:%S"), style="bold yellow")
        empty = Text("")
        online = sum(1 for view in views if view.connected)
        full = self._status_text(views)
        compact = self._status_text(views, compact=True)
        minimal = Text(f"{online}/{len(views)}", style="bold white")

        def title(tagline: bool, version: bool) -> Text:
            text = Text("YAMD", style="bold cyan")
            if tagline:
                text.append(" Yet Another Multi Dashboard",
                            style="bold bright_white")
            if version:
                text.append(f" · v{VERSION}", style="white")
            text.no_wrap = True
            text.overflow = "ellipsis"
            return text

        options = [
            (width >= 100, True, clock_full, full),
            (False, True, clock_short, full),
            (False, True, empty, full),
            (False, True, empty, compact),
            (False, False, empty, compact),
            (False, False, empty, minimal),
        ]
        chosen = (title(False, False), empty, minimal)
        for tagline, version, clock, status in options:
            candidate = title(tagline, version)
            # Keep two spare columns so the ratio column separates the cells
            # instead of letting them touch.
            if (candidate.cell_len + clock.cell_len + status.cell_len + 2
                    <= room):
                chosen = (candidate, clock, status)
                break
        chosen_title, chosen_clock, chosen_status = chosen
        for cell in (chosen_title, chosen_clock, chosen_status):
            cell.no_wrap = True
            cell.overflow = "ellipsis"

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="center", ratio=1)
        header.add_column(justify="right")
        header.add_row(chosen_title, chosen_clock, chosen_status)
        return Panel(header, style="bold white on blue")

    def _make_detail_header(self, view: TargetView, width: int) -> Panel:
        # Same measured-width approach as _make_header, but the badge is the
        # one cell that must always survive: it is the reason the user looks
        # at this header. The clock, address, error text and kind tag go in
        # that order.
        room = max(8, width - 4)
        now = datetime.now()
        clock_full = Text(now.strftime("%Y-%m-%d %H:%M:%S"),
                          style="bold yellow")
        clock_short = Text(now.strftime("%H:%M:%S"), style="bold yellow")
        empty = Text("")
        addr = view.url.removeprefix("https://").removeprefix("http://")
        error = f" ({view.error})" if view.error and view.connected else ""

        def title(show_kind: bool) -> Text:
            text = Text("YAMD", style="bold cyan")
            text.append(f" · {view.name}", style="bold white")
            if show_kind:
                text.append(f" [{view.kind.upper()}]", style="magenta")
            text.no_wrap = True
            text.overflow = "ellipsis"
            return text

        def right(show_addr: bool, show_error: bool) -> Text:
            text = Text()
            if show_addr:
                text.append(f"{addr}  ", style="dim yellow")
            text.append(view.badge,
                        style=STATE_STYLE.get(view.badge, "bold white"))
            if show_error:
                text.append(error, style="bold yellow")
            text.no_wrap = True
            text.overflow = "ellipsis"
            return text

        options = [
            (True, clock_full if width >= 110 else clock_short,
             True, bool(error)),
            (True, clock_short, True, bool(error)),
            (True, empty, True, bool(error)),          # drop the clock
            (True, empty, False, bool(error)),         # drop the address
            (True, empty, False, False),               # drop the error
            (False, empty, False, False),              # drop the kind tag
        ]
        chosen = (title(False), empty, right(False, False))
        for show_kind, clock, show_addr, show_error in options:
            candidate = title(show_kind)
            candidate_right = right(show_addr, show_error)
            # Two spare columns keep the ratio column as a visible gap.
            if (candidate.cell_len + clock.cell_len
                    + candidate_right.cell_len + 2 <= room):
                chosen = (candidate, clock, candidate_right)
                break
        chosen_title, chosen_clock, chosen_right = chosen
        for cell in (chosen_title, chosen_clock, chosen_right):
            cell.no_wrap = True
            cell.overflow = "ellipsis"

        # Even the minimal header can overflow on a tiny terminal; shorten
        # the title so the badge keeps its cells (and a gap).
        overflow = chosen_title.cell_len + chosen_right.cell_len + 2 - room
        if overflow > 0:
            chosen_title.truncate(
                max(4, chosen_title.cell_len - overflow), overflow="ellipsis")

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="center", ratio=1)
        header.add_column(justify="right")
        header.add_row(chosen_title, chosen_clock, chosen_right)
        return Panel(header, style="bold white on blue")

    def _mode_text(self, views: list[TargetView]) -> str:
        if not self._auto:
            text = "manual (a resumes)"
        else:
            remaining = self._manual_until - time.monotonic()
            if remaining > 0:
                text = f"manual {remaining:.0f}s"
            else:
                active = [i for i, v in enumerate(views) if v.active]
                if active:
                    try:
                        pos = active.index(self._selected) + 1
                    except ValueError:
                        pos = 1
                    text = f"auto {pos}/{len(active)}"
                else:
                    text = "auto · idle / last-used"
        return text

    def _keys_bar(self, views: list[TargetView], width: int) -> Text:
        reqs = self.view_mode in ("detail", "split")
        if width < 90:
            text = " TAB · 1-9 · m/d/s · a · "
            if reqs and width >= 75:
                text += "PgUp/Dn reqs · "
            text += "q "
        else:
            text = (" TAB target · 1-9 jump · m monitor · d detail · "
                    "s split · a auto · ")
            if reqs and width >= 114:
                text += "PgUp/Dn reqs · "
            text += "q quit "
        text += f"│ {self._mode_text(views)}"
        if len(text) > width:
            text = text[:max(0, width - 1)]
        return Text(text.ljust(width), style="dim", no_wrap=True)

    @staticmethod
    def _unified_log_state(kind: str, state: str) -> str:
        """Map the native collector words onto the monitor's vocabulary."""
        if kind == "strata":
            return {"READING": "BUSY", "GENERATING": "BUSY"}.get(state, state)
        return state

    def _make_activity_panel(self) -> Panel:
        entries: list[tuple[float, str, str]] = []
        for rt in self._runtimes:
            for ts, state in rt.collector.get_state_log():
                entries.append((ts, rt.spec.name,
                                self._unified_log_state(rt.spec.kind, state)))
        entries.sort(key=lambda item: item[0])

        text = Text(no_wrap=True, overflow="ellipsis")
        if not entries:
            text.append("Waiting for state transitions…", style="dim")
        else:
            for i, (ts, name, state) in enumerate(entries[-5:]):
                if i > 0:
                    text.append(" → ", style="dim")
                text.append(f"{_clock(ts)} {name} ", style="dim")
                text.append(state, style=STATE_STYLE.get(state, "green"))
        return Panel(text, title="[bold]Activity Log[/bold]", border_style="dim")

    # ------------------------------------------------------------------
    # Monitor view
    # ------------------------------------------------------------------

    @staticmethod
    def _monitor_columns(inner: int) -> list[list]:
        """Column layout for the target list, dropping low-value columns."""
        cols: list[list] = [["mark", "", 1], ["num", "#", 2],
                            ["name", "NAME", 8]]
        show_kind = inner >= 68
        show_model = inner >= 82
        show_live = inner >= 92
        show_rate = inner >= 60

        if show_kind:
            cols.append(["kind", "KIND", 7])
        if show_model:
            cols.append(["model", "MODEL", 10])
        if show_live:
            cols.append(["live", "LIVE", 10])
        cols.append(["state", "STATE", 9])
        if show_rate:
            cols.append(["rate", "TG t/s", 9])
        cols.append(["time", "TIME", 8])
        cols.append(["tokens", "TOKENS", 14])

        used = sum(c[2] for c in cols) + len(cols) - 1
        spare = max(0, inner - used)
        if show_model:
            grow = min(spare, 6)
            cols[2][2] += grow
            spare -= grow
            for col in cols:
                if col[0] == "model":
                    col[2] += spare
                    break
        else:
            cols[2][2] += spare
        return cols

    @staticmethod
    def _row_values(index: int, view: TargetView) -> dict[str, str]:
        digest = view.digest or {}
        prompt = digest.get("prompt_tokens")
        output = digest.get("output_tokens")
        tokens = "—"
        if prompt is not None or output is not None:
            tokens = f"{_kfmt(prompt)}→{_kfmt(output)}"
        return {
            "mark": "",
            "num": str(index + 1),
            "name": view.name,
            "kind": view.kind.upper(),
            "model": view.model or "—",
            "live": view.live,
            "state": view.badge,
            "rate": _rate(digest.get("decode_tok_s")),
            "time": _clock(digest.get("time")),
            "tokens": tokens,
        }

    def _targets_panel(
        self,
        views: list[TargetView],
        width: int,
        height: int,
        title: str = "Targets",
    ) -> Panel:
        inner = max(20, width - 4)
        cols = self._monitor_columns(inner)
        visible = max(1, height - 3)       # panel borders + column header

        if self._selected < self._scroll:
            self._scroll = self._selected
        elif self._selected >= self._scroll + visible:
            self._scroll = self._selected - visible + 1
        self._scroll = max(0, min(self._scroll, max(0, len(views) - visible)))

        body = Text(no_wrap=True, overflow="ellipsis")
        # Column header.
        for i, (_, label, col_w) in enumerate(cols):
            body.append(str(label)[:col_w].ljust(col_w), style="dim")
            if i < len(cols) - 1:
                body.append(" ")

        for row_index in range(self._scroll, min(len(views), self._scroll + visible)):
            view = views[row_index]
            values = self._row_values(row_index, view)
            selected = row_index == self._selected
            body.append("\n")
            for i, (key, _, col_w) in enumerate(cols):
                value = str(values.get(key, ""))[:col_w].ljust(col_w)
                if key == "mark":
                    style = "bold yellow" if selected else ""
                    value = "▸" if selected else " "
                    value = value.ljust(col_w)
                elif key == "state":
                    style = STATE_STYLE.get(view.badge, "bold white")
                elif key == "name":
                    style = "bold white" if selected else "white"
                elif key == "kind":
                    style = "magenta"
                elif key == "rate":
                    style = "green"
                elif key == "time":
                    style = "dim"
                else:
                    style = "white" if view.connected else "dim"
                body.append(value, style=style)
                if i < len(cols) - 1:
                    body.append(" ")

        if len(views) > visible:
            title = (f"[bold]{title}[/bold] [dim]· {self._scroll + 1}-"
                     f"{min(len(views), self._scroll + visible)} of "
                     f"{len(views)}[/dim]")
        else:
            title = f"[bold]{title}[/bold] [dim]· {len(views)}[/dim]"
        return Panel(body, title=title, border_style="cyan", padding=(0, 1))

    def _summary_panel(self, view: TargetView) -> Panel:
        # Five content rows; _render_monitor gives the panel at least seven
        # rows (borders included) so the last one is never clipped. no_wrap
        # keeps long model/Live lines on one row instead of stealing a row
        # from the Note/Endpoint line.
        table = Table.grid(expand=True)
        table.add_column(style="cyan", width=10)
        table.add_column(style="bold white", ratio=1, no_wrap=True,
                         overflow="ellipsis")

        table.add_row("State:", Text(view.badge,
                                     style=STATE_STYLE.get(view.badge, "bold white")))
        table.add_row("Model:", view.model or "—")

        snapshot = view.snapshot
        if view.kind == "strata":
            pp = _num(getattr(snapshot, "prefill_tok_s_mean", None), 1)
            tg = _num(getattr(snapshot, "tok_s", None), 1)
            table.add_row("Live:", (
                f"ctx {getattr(snapshot, 'ctx_used', 0):,}/"
                f"{getattr(snapshot, 'max_context', 0):,}"
                f" · q{getattr(snapshot, 'queued', 0)}"
                f" · PP {pp} / TG {tg} t/s"))
        else:
            draft = ((getattr(snapshot, "draft_tokens_accepted", 0)
                      / getattr(snapshot, "draft_tokens_total", 1))
                     if getattr(snapshot, "draft_tokens_total", 0) else None)
            table.add_row("Live:", (
                f"{view.live} · busy {_secs(getattr(snapshot, 'busy_for_s', None))}"
                f" · KV {_pct(getattr(snapshot, 'kv_usage_ratio', None))}"
                f" · cache {_pct(getattr(snapshot, 'cache_hit_rate', None))}"
                f" · draft {_pct(draft)}"))

        table.add_row("Last:", _digest_line(view.digest))
        if view.error:
            table.add_row("Note:", Text(view.error, style="bold yellow"))
        else:
            table.add_row("Endpoint:", f"{view.kind.upper()} · {view.url}")

        return Panel(table, title=f"[bold]Selected[/bold] [dim]· {view.name}[/dim]",
                     border_style="yellow")

    def _render_monitor(
        self, width: int, height: int, views: list[TargetView],
        web: bool = False,
    ) -> Layout:
        header_h = 3
        # The browser has its own button bar, so the terminal key-help line is
        # redundant there; dropping it gives the dashboard one more row.
        keys_h = 0 if web else 1
        activity_h = 3 if height >= 18 else 0
        # The Selected panel has five content rows, so it needs seven rows
        # including its borders. The old six-row tier silently clipped the
        # Note/Endpoint line on the common 24-row terminal.
        if height >= 30:
            summary_h = 8
        elif height >= 22:
            summary_h = 7
        else:
            summary_h = 0

        targets_h = height - header_h - keys_h - activity_h - summary_h
        if targets_h < 5:
            summary_h = 0
            targets_h = height - header_h - keys_h - activity_h
        if targets_h < 3:
            activity_h = 0
            targets_h = height - header_h - keys_h
        targets_h = max(3, targets_h)

        layout = Layout()
        parts = [Layout(name="header", size=header_h),
                 Layout(name="targets", size=targets_h)]
        if summary_h:
            parts.insert(2, Layout(name="summary", size=summary_h))
        if activity_h:
            parts.append(Layout(name="activity", size=activity_h))
        if keys_h:
            parts.append(Layout(name="keys", size=keys_h))
        layout.split(*parts)

        layout["header"].update(self._make_header(views, width))
        layout["targets"].update(self._targets_panel(views, width, targets_h))
        if summary_h:
            selected = self._selected_view(views)
            if selected:
                layout["summary"].update(self._summary_panel(selected))
        if activity_h:
            layout["activity"].update(self._make_activity_panel())
        if keys_h:
            layout["keys"].update(self._keys_bar(views, width))
        return layout

    # ------------------------------------------------------------------
    # Detail body (the Strata/Halogen panels)
    # ------------------------------------------------------------------

    def _fill_strata_body(
        self, container: Layout, view: TargetView, width: int, height: int,
    ) -> None:
        snapshot = view.snapshot
        container.split_row(
            Layout(name="left", ratio=LEFT_RATIO),
            Layout(name="right", ratio=RIGHT_RATIO),
        )

        compact = height < METRIC_ROWS + 3 * PANEL_ROWS
        left = [Layout(name="metrics"), Layout(name="performance", size=5)]
        if not compact:
            left += [Layout(name="session", size=5), Layout(name="engine", size=5)]
        container["left"].split(*left)

        show_hardware = (height
                         >= HARDWARE_ROWS + 2 + MIN_DETAIL_REQUEST_ROWS)
        if show_hardware:
            container["right"].split(
                Layout(name="hardware", size=HARDWARE_ROWS + 2),
                Layout(name="requests"),
            )
            rows = max(1, height - (HARDWARE_ROWS + 2) - 3)
        else:
            container["right"].split(Layout(name="requests"))
            rows = max(1, height - 3)
        if self.req_rows is not None:
            rows = min(rows, self.req_rows)
        view.runtime.req_page_rows = max(1, rows)
        if not (snapshot.requests or []):
            view.runtime.req_anchor = None

        fixed = PANEL_ROWS if compact else 3 * PANEL_ROWS
        spacers = (height - fixed) >= METRIC_ROWS + METRIC_SPACERS

        container["metrics"].update(
            strata_make_metrics_panel(snapshot, width, self._frame, spacers,
                                    blink=self.blink))
        container["performance"].update(
            strata_make_performance_panel(snapshot, width))
        if not compact:
            container["session"].update(strata_make_session_panel(snapshot, width))
            container["engine"].update(
                _make_engine_panel("strata", snapshot, width))
        if show_hardware:
            container["hardware"].update(
                strata_make_hardware_panel(snapshot, width))
        container["requests"].update(
            strata_make_requests_panel(
                snapshot, width, rows, view.runtime.req_anchor))

    def _fill_halogen_body(
        self, container: Layout, view: TargetView, width: int, height: int,
    ) -> None:
        snapshot = view.snapshot
        local = view.runtime.local
        mode = halogen_layout_mode(height, local)

        container.split_row(
            Layout(name="left", ratio=LEFT_RATIO),
            Layout(name="right", ratio=RIGHT_RATIO),
        )

        left = [Layout(name="metrics"), Layout(name="performance", size=5)]
        if mode != "compact":
            if mode == "full":
                left.append(Layout(name="session", size=5))
            left.append(Layout(name="engine", size=5))
            if local:
                left.append(Layout(name="host", size=5))
        container["left"].split(*left)

        show_cache = (height
                      >= CACHE_ROWS + 2 + MIN_DETAIL_REQUEST_ROWS)
        if show_cache:
            container["right"].split(
                Layout(name="cache", size=CACHE_ROWS + 2),
                Layout(name="requests"),
            )
            rows = max(1, height - (CACHE_ROWS + 2) - 3)
        else:
            container["right"].split(Layout(name="requests"))
            rows = max(1, height - 3)
        if self.req_rows is not None:
            rows = min(rows, self.req_rows)
        view.runtime.req_page_rows = max(1, rows)
        if not (snapshot.requests or []):
            view.runtime.req_anchor = None

        if mode == "compact":
            fixed = PANEL_ROWS
        elif mode == "no_session":
            fixed = 3 * PANEL_ROWS
        else:
            fixed = PANEL_ROWS * (4 if local else 3)
        spacers = (height - fixed) >= (
            METRIC_ROWS + METRIC_SPACERS + 2)

        container["metrics"].update(
            halogen_make_metrics_panel(snapshot, width, spacers))
        container["performance"].update(
            halogen_make_performance_panel(snapshot, width))
        if mode != "compact":
            if mode == "full":
                container["session"].update(
                    halogen_make_session_panel(snapshot, width))
            container["engine"].update(
                _make_engine_panel("halogen", snapshot, width))
            if local:
                container["host"].update(halogen_make_host_panel(snapshot, width))
        if show_cache:
            container["cache"].update(halogen_make_cache_panel(snapshot, width))
        container["requests"].update(
            halogen_make_requests_panel(
                snapshot, width, rows, view.runtime.req_anchor))

    def _fill_detail_body(
        self, container: Layout, view: TargetView, width: int, height: int,
    ) -> None:
        if not view.connected:
            # Match the single-server dashboards: a lost server replaces the
            # body instead of leaving stale panels on screen. Drop the Requests
            # scroll position too, so reconnecting does not resume on a page
            # from the old session (or hold auto rotation).
            view.runtime.req_anchor = None
            view.runtime.req_page_rows = DEFAULT_REQ_ROWS
            error = view.error or "Unknown error"
            if view.kind == "strata":
                container.update(strata_make_offline_panel(error))
            else:
                container.update(halogen_make_offline_panel(error))
            return
        if view.kind == "strata":
            self._fill_strata_body(container, view, width, height)
        else:
            self._fill_halogen_body(container, view, width, height)

    def _render_detail(
        self, width: int, height: int, views: list[TargetView],
        web: bool = False,
    ) -> Layout:
        view = self._selected_view(views)
        if view is None:
            return self._empty_layout(width, height, web=web)

        activity_h = 3 if height >= 16 else 0
        keys_h = 0 if web else 1
        body_h = max(4, height - 3 - activity_h - keys_h)

        layout = Layout()
        parts = [Layout(name="header", size=3), Layout(name="body")]
        if activity_h:
            parts.append(Layout(name="activity", size=activity_h))
        if keys_h:
            parts.append(Layout(name="keys", size=keys_h))
        layout.split(*parts)

        layout["header"].update(self._make_detail_header(view, width))
        self._fill_detail_body(layout["body"], view, width, body_h)
        if activity_h:
            layout["activity"].update(self._make_activity_panel())
        if keys_h:
            layout["keys"].update(self._keys_bar(views, width))
        return layout

    # ------------------------------------------------------------------
    # Split view: target list on top, selected detail below
    # ------------------------------------------------------------------

    def _render_split(
        self, width: int, height: int, views: list[TargetView],
        web: bool = False,
    ) -> Layout:
        view = self._selected_view(views)
        if view is None:
            return self._empty_layout(width, height, web=web)

        header_h = 3
        keys_h = 0 if web else 1
        title_h = 1
        usable = max(6, height - header_h - keys_h - title_h)
        # The configured percentage is the upper bound, not a fixed height:
        # a short target list uses only the rows it needs, and the detail
        # panel takes the rest. The list needs two borders, one column header
        # and one row per target.
        max_targets_h = max(4, int(usable * self.split_pct / 100))
        needed_h = max(4, len(views) + 3)
        targets_h = min(max_targets_h, needed_h)
        # Keep at least a little room for the detail panel on tiny terminals.
        targets_h = min(targets_h, max(4, usable - 8))
        targets_h = max(4, targets_h)
        detail_h = usable - targets_h

        layout = Layout()
        parts = [
            Layout(name="header", size=header_h),
            Layout(name="targets", size=targets_h),
            Layout(name="detail_title", size=title_h),
            Layout(name="detail"),
        ]
        if keys_h:
            parts.append(Layout(name="keys", size=keys_h))
        layout.split(*parts)
        layout["header"].update(self._make_header(views, width))
        layout["targets"].update(
            self._targets_panel(views, width, targets_h, "Targets"))
        title = Text(no_wrap=True, overflow="ellipsis")
        title.append(" Detail · ", style="bold cyan")
        title.append(view.name, style="bold white")
        title.append(f" [{view.kind.upper()}] · {view.badge}",
                     style=STATE_STYLE.get(view.badge, "white"))
        title.append(f" · {self._mode_text(views)}", style="dim")
        layout["detail_title"].update(title)
        self._fill_detail_body(layout["detail"], view, width, detail_h)
        if keys_h:
            layout["keys"].update(self._keys_bar(views, width))
        return layout

    # ------------------------------------------------------------------
    # Frame assembly
    # ------------------------------------------------------------------

    def _fallback_layout(self, width: int, web: bool, body: Panel) -> Layout:
        """Header + one message panel, used when there is nothing to draw."""
        keys_h = 0 if web else 1
        layout = Layout()
        parts = [Layout(name="header", size=3), Layout(name="body")]
        if keys_h:
            parts.append(Layout(name="keys", size=keys_h))
        layout.split(*parts)
        layout["header"].update(self._make_header([], width))
        layout["body"].update(body)
        if keys_h:
            layout["keys"].update(Text(" q quit ", style="dim"))
        return layout

    def _empty_layout(self, width: int, height: int,
                      web: bool = False) -> Layout:
        return self._fallback_layout(width, web, Panel(
            Text("No targets configured", style="bold red"),
            title="[bold red]YAMD[/bold red]", border_style="red"))

    def _error_layout(self, width: int, height: int, exc: Exception,
                      web: bool = False) -> Layout:
        """Last-resort frame so a render bug does not kill the dashboard."""
        message = Text(f"{type(exc).__name__}: {exc}", style="bold red",
                       no_wrap=True, overflow="ellipsis")
        return self._fallback_layout(width, web, Panel(
            message, title="[bold red]Render error[/bold red]",
            border_style="red"))

    def web_state(self) -> dict:
        """Small JSON payload for the browser's bottom bar."""
        return {"mode": self._mode_text(self._views())}

    def _layout_for(self, width: int, height: int, web: bool = False) -> Layout:
        width = max(20, width)
        height = max(10, height)
        self._frame += 1
        views = self._views()
        if self.view_mode == "detail":
            return self._render_detail(width, height, views, web=web)
        if self.view_mode == "split":
            return self._render_split(width, height, views, web=web)
        return self._render_monitor(width, height, views, web=web)

    def render_ansi(self, width: int, height: int) -> str:
        # The browser supplies its own button bar, so skip the terminal
        # key-help line and leave that row to the dashboard.
        layout = self._layout_for(width, height, web=True)
        buf = io.StringIO()
        console = Console(
            file=buf,
            width=width,
            height=height,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
            highlight=False,
        )
        console.print(layout, end="")
        text = buf.getvalue()
        return text[:-1] if text.endswith("\n") else text

    async def _run_terminal(self) -> None:
        width, height = self.console.size
        width = width or 120
        height = height or 40
        try:
            first = self._layout_for(width, height)
        except Exception as exc:       # keep the UI alive, show the error
            first = self._error_layout(width, height, exc)
        refresh = max(1, int(round(1.0 / self.render_interval)))
        with Live(
            first,
            console=self.console,
            refresh_per_second=refresh,
            screen=True,
            redirect_stdout=False,
            redirect_stderr=False,
        ) as live:
            while self._running:
                # Idle at the configured rate; keys, view changes and
                # rotation set _render_event for an immediate redraw.
                try:
                    await asyncio.wait_for(self._render_event.wait(),
                                           timeout=self.render_interval)
                except asyncio.TimeoutError:
                    pass
                self._render_event.clear()
                width, height = self.console.size
                width = width or 80
                height = height or 24
                try:
                    layout = self._layout_for(width, height)
                except Exception as exc:   # same: never exit on a render bug
                    layout = self._error_layout(width, height, exc)
                live.update(layout)

    async def run(self) -> None:
        if self._debug_path:
            self._debug_fp = open(self._debug_path, "w", buffering=1)
            print(f"[YAMD] Debug log: {self._debug_path}")

        rotation_task: Optional[asyncio.Task] = None
        key_task: Optional[asyncio.Task] = None
        reader = KeyReader()
        web_server = None
        try:
            await self._prepare_targets()
            if not self._runtimes:
                print("[YAMD] No targets configured.")
                return
            if not self._running:
                return

            for index, rt in enumerate(self._runtimes):
                await rt.collector.start()
                if index + 1 < len(self._runtimes):
                    await asyncio.sleep(0.1)

            rotation_task = asyncio.create_task(self._rotation_loop())

            if self.web_port is not None:
                web_server = YAMDWebServer(self, self.web_host, self.web_port)
                try:
                    await web_server.start()
                except OSError as exc:
                    print(f"[YAMD] Web server failed on "
                          f"{self.web_host}:{self.web_port}: {exc}")
                    try:
                        await web_server.stop()
                    except Exception:
                        pass
                    web_server = None
                    if self.web_only:
                        return
                else:
                    shown = ("127.0.0.1"
                             if self.web_host in ("0.0.0.0", "::", "")
                             else self.web_host)
                    print(f"[YAMD] Web view: http://{shown}:{self.web_port}/")

            if self.web_only:
                while self._running:
                    await asyncio.sleep(0.2)
            else:
                reader.start()
                key_task = asyncio.create_task(self._key_loop(reader))
                await self._run_terminal()
        finally:
            reader.stop()
            for task in (key_task, rotation_task):
                if task is None:
                    continue
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    # Surface a failed background task instead of leaving an
                    # unretrieved exception at loop shutdown.
                    print(f"[YAMD] background task failed: {exc}")
            if web_server is not None:
                await web_server.stop()
            await asyncio.gather(
                *(rt.collector.stop() for rt in self._runtimes),
                return_exceptions=True,
            )
            if self._debug_fp is not None:
                self._debug_fp.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="YAMD - Yet Another Multi Dashboard. "
                    "Monitors several Strata and Halogen instances at once.")
    parser.add_argument(
        "--server", "-s",
        help="Single target URL (classic one-server invocation; needs --kind "
             "or auto-detects)",
    )
    parser.add_argument(
        "--name",
        help="Target name for --server (default: host:port)",
    )
    parser.add_argument(
        "--kind",
        default="auto",
        choices=KINDS,
        help="Server kind for --server/auto targets (default: auto)",
    )
    parser.add_argument(
        "--target", "-t",
        action="append",
        default=[],
        metavar="[NAME=][KIND@]URL",
        help="Add a target; repeatable. Examples: "
             "main=halogen@127.0.0.1:8731, strata@10.0.0.5:8080",
    )
    parser.add_argument(
        "--config",
        help="TOML/JSON config: [[target]] entries plus optional [web] "
             "defaults (default: ~/.config/yamd/targets.toml)",
    )
    parser.add_argument(
        "--api-key",
        help="Bearer key sent to targets that do not define their own",
    )
    parser.add_argument(
        "--requests",
        default="all",
        help="Maximum Detail request rows per page: a count, or 'all' to fill "
             "the available rows (default: all); PgUp/PgDn scrolls the "
             "retained ring",
    )
    parser.add_argument(
        "--debug",
        help="Write raw /health, /metrics and /cache/status responses as JSONL",
    )
    parser.add_argument(
        "--view",
        default=None,
        choices=CONFIG_VIEWS,
        help="Initial view (default: monitor, or [ui] view from the config; "
             "'auto' picks split for a short target list)",
    )
    parser.add_argument(
        "--cycle",
        type=float,
        default=DEFAULT_CYCLE,
        help="Seconds between automatic target switches (default: 5)",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=DEFAULT_HOLD,
        help="Seconds a manual TAB selection is held (default: 15)",
    )
    parser.add_argument(
        "--split",
        type=int,
        default=DEFAULT_SPLIT,
        metavar="PCT",
        help="Target-list height in the split view, in percent (default: 35)",
    )
    host_group = parser.add_mutually_exclusive_group()
    host_group.add_argument(
        "--local",
        action="store_true",
        help="Force the Halogen Host panel on for every target (testing)",
    )
    host_group.add_argument(
        "--no-host",
        action="store_true",
        help="Force the Halogen Host panel off, even for loopback targets",
    )
    web_group = parser.add_mutually_exclusive_group()
    web_group.add_argument(
        "--web",
        action="store_true",
        default=None,
        help="Enable the xterm.js web view (interactive in YAMD)",
    )
    web_group.add_argument(
        "--no-web",
        dest="web",
        action="store_false",
        help="Disable the web view even if the config file enables it",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Web listen port (default 8080; implies --web)",
    )
    parser.add_argument(
        "--listen",
        default=None,
        help="Web bind address (default 0.0.0.0; 127.0.0.1 for local only)",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        default=None,
        help="Serve the web view without drawing the terminal dashboard",
    )
    args = parser.parse_args()

    if args.requests.lower() == "all":
        req_rows: Optional[int] = None
    else:
        try:
            req_rows = max(1, int(args.requests))
        except ValueError:
            parser.error("--requests must be a count or 'all'")

    force_local: Optional[bool] = None
    if args.local:
        force_local = True
    elif args.no_host:
        force_local = False

    specs = build_specs(args)
    cfg_path = Path(args.config) if args.config else None
    try:
        ui_cfg = load_ui_config(cfg_path)
        view = resolve_ui_options(args, ui_cfg, len(specs))
        blink = resolve_blink(ui_cfg)
        web = resolve_web_options(args, load_web_config(cfg_path))
    except ValueError as exc:
        parser.error(str(exc))

    web_port: Optional[int] = None
    if web.enabled:
        web_port = WEB_DEFAULT_PORT if web.port is None else web.port
        if not 1 <= web_port <= 65535:
            parser.error("--port must be between 1 and 65535")

    app = YAMDApplication(
        specs=specs,
        req_rows=req_rows,
        debug_file=args.debug,
        cycle=args.cycle,
        hold=args.hold,
        split_pct=args.split,
        view=view,
        blink=blink,
        force_local=force_local,
        web_port=web_port,
        web_host=web.listen,
        web_only=web.only,
    )
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
