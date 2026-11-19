# CLAUDE.md — Project Context for YAMD

## Project Overview

**YAMD** (Yet Another Multi Dashboard) is a real-time terminal UI for monitoring
several `Halogen` (peonist-ai/halogen-flash-server) and Strata instances at once. A
Halogen target is polled through `/metrics` and `/cache` every second, with `/health`
refreshed every 10 s; a Strata target uses `/metrics`, `/health` and `/v1/status`. The
selected target renders as a flicker-free Rich dashboard at 1–2 Hz.

- **Version:** 2.2.0 (check `pyproject.toml` for current)
- **Language:** Python 3.10+
- **Single file:** `yamd.py` (~6000 lines; both collectors and detail panels)
- **License:** MIT (Copyright 2026 Antani Technologies BV)

The file is the single source for the former YAHD (Halogen) and YASD (Strata)
dashboards. Both collectors and their panels live in `yamd.py`; the YAMD application
adds the monitor/split views, target rotation and the interactive web view.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    YAMDApplication                      │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────┐  │
│  │  Collector   │   │    Layout    │   │   Live     │  │
│  │ (async loop) │──▶│  (Rich)      │──▶│  (10 FPS)  │  │
│  └──────────────┘   └──────────────┘   └────────────┘  │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│                   Halogen server                        │
│  /health  /metrics (Prometheus)  /cache  (all polled)   │
└─────────────────────────────────────────────────────────┘
```

### Core Components

| Component | Purpose |
|-----------|---------|
| `HalogenCollector` | Async background poller: `/metrics` + `/cache` every second, `/health` in its own task every 10 s. Parses JSON/Prometheus, derives counter deltas, builds snapshots, tracks connectivity, state log, request windows, history series, and raw bodies for `--debug`. |
| `HalogenSnapshot` | Immutable flat dataclass holding all metrics at one point in time. |
| Panel builders (`strata_make_*`, `halogen_make_*`) | Pure functions that build Rich `Panel` objects from a snapshot (`make_metrics_panel`, `make_performance_panel`, `make_session_panel`, `make_engine_panel`, `make_requests_panel`, `make_offline_panel`, plus Strata `make_hardware_panel` and Halogen `make_host_panel`/`make_cache_panel`). |
| `HostSampler` | Local `/proc` + amdgpu sysfs reader used only for loopback servers; fills CPU/GPU load and temperature plus RAM usage. |
| `WebServer` | Optional aiohttp server (`--web`/`--port`): serves the xterm.js page at `/` and streams Rich ANSI frames over `/ws`, one render per unique browser size at 5 FPS. |
| `YAMDApplication` | Orchestrates the collectors, target rotation, monitor/detail/split layouts, the Live render loop or web-only wait, signals and debug logging. |

## Dashboard Panels

1. **Header** — Connection status (ONLINE / OFFLINE / ENGINE DOWN / DEGRADED), server address (scheme stripped), Halogen API version, timestamp
2. **Metrics (left)** — State badge (IDLE / BUSY / QUEUED / DEGRADED / NO ENGINE), KV-pool bar (green ≤69.9%, orange 70–85%, red >85%, no blink), context/slots, last request, in-flight/queue, busy elapsed, token totals, draft acceptance, cache hit rates, structured counts, model name
3. **Performance (left)** — Prefill & decode speeds side-by-side, `last req` / `session avg` source sub-labels
4. **Session (left)** — Requests seen vs server total, processed vs cached prompt tokens, generated tokens
5. **Engine (left)** — KV dtype + pool positions, drafter + prompt-lookup, prompt-cache mode/cap/disk
6. **Host (left, local only)** — CPU/GPU load, CPU/GPU temperature and RAM usage, read from `/proc` and amdgpu sysfs; shown only for loopback URLs
7. **Cache & Pool (right)** — 10 one-line cards (Decode, Prefill, KV pool, Cache hit, Token hit, Entries, Cache size, Draft acc, In flight, Queue wait), each with a colour-coded braille graph
8. **Requests (right, flex)** — One scrollable page of client-derived completion windows (Time, Reqs, Prompt, Cached, Output, PP t/s, TG t/s, Draft); as width shrinks, Draft drops first, then PP, while TG stays to the end. `PgUp`/`PgDn` page, `j`/`k` step, `Home`/`End` jump to newest/oldest; the page is anchored by row key and the title shows its range
9. **Footer** — Activity log of last 5 state transitions

## Key Technical Details

### State Detection (`/health` + `/metrics`)
- `in_flight > 0` → BUSY
- `queued > 0` → **QUEUED** (overrides)
- `halogen:engine_up == 0` → **NO ENGINE** (`/health` still answers: not offline)
- `/health.capability_probe != "ok"` → **DEGRADED**
- Halogen has no "unloaded" state (no SLEEPING) and no live prefilling/decoding phase,
  so there is no READING/GENERATING badge; the last request's prefill/decode speed is
  still shown on the Performance panel

### Connectivity Policy
- Connectivity is judged by the 1 s `/metrics` poll; `/health` runs in its own task
  (`HEALTH_REFRESH = 10.0` s) and a failure there is retried after 1 s without marking
  the server offline
- OFFLINE only after 4 consecutive failed poll cycles (~4 s); the first successful cycle
  connects immediately, and 2 consecutive successes recover after an outage
- 401 on `/metrics` while `/health` answers → stays ONLINE, reports "API key rejected" (`ApiKeyRejected`, `_AUTH` sentinel)
- `/metrics` unreachable but `/health` fine → ONLINE with "Metrics endpoint unavailable" (`MetricsUnavailable`)
- Malformed JSON/Prometheus keeps the last good snapshot, never flips connectivity
- Repeated connection resets rebuild the `aiohttp` session (hardened connector: `limit_per_host=8`, `enable_cleanup_closed`, `ttl_dns_cache=300`)

### Speeds and Deltas (important)
- Halogen updates its token counters **at request completion**, not during generation.
- Speeds are derived from the deltas of `llamacpp:prompt_tokens_total` / `prompt_seconds_total` (prefill) and `tokens_predicted_total` / `tokens_predicted_seconds_total` (decode). These match the response `timings` (`prompt_per_second`, `predicted_per_second`) exactly.
- No delta ⇒ no live rate; the snapshot keeps the last completed request's speed (`last req`) or the server-lifetime average (`session avg`).
- `busy_for_s` (health/metrics) is the elapsed time of the oldest in-flight request.
- See `_derive()` in `yamd.py` and `halogen_make_performance_panel()`.

### Request Rows
- Halogen has no request-ring endpoint. The collector keeps `_req_events` and appends one row per poll window where `halogen:requests_total` increased. Token deltas, the window's PP (`prompt_tokens`/`prompt_seconds`) and TG (`tokens_predicted`/`predicted_seconds`) rates, and Draft acceptance (`draft_tokens_accepted`/`draft_tokens`) are attached.
- Newest first in `snapshot.requests`; cap is `REQ_EVENT_MAX = 500`; `--requests N | all` caps one page, and the whole retained ring is scrollable (`PgUp`/`PgDn`, `j`/`k`, `Home`/`End`). Scrolling pauses automatic rotation while the selected target is scrolled.
- Responsive columns: Draft disappears below 64 inner columns, PP below 48; TG (decode) is always kept. Prompt appears at ≥42 and Cached at ≥74.

### KV Pool and Cache
- Gauge: `/cache.pool.usage_ratio`, fallback `llamacpp:kv_cache_usage_ratio`; used/positions from `pool.used` / `pool.positions`
- Display capped at 99.5 % so it never reads exactly 100 %
- `/cache`: `hit_rate` (requests) and `token_hit_rate` (prompt tokens), entries/max, bytes, hits/misses/stores/evicted/dropped, pool regions, disk tier, `stale_s`
- `/metrics` carries the same counters as fallback when `/cache` is unavailable
- Graph history is the dashboard's own two-minute ring (`HISTORY_MAX = 120` at 1 s); Decode/Prefill plot each completed window's rate (zero between requests)

### Number Formatting and Graphs
- `_num` (thousands separators, em-dash for missing), `_kfmt` (k/M suffixes), `_gb` (GiB)
- Braille graphs: 2 samples per character, 4 dot rows per cell, bottom-up, unlit cell = space; `_braille_level` keeps btop's rounding and a one-row floor for non-zero samples
- Each card's graph colour comes from `GRAPH_COLORS` (card order); Rich downsamples when truecolor is unavailable
- Draws the newest 2 × width samples, one per virtual column, so the curve advances one column per poll; short rings are left-padded so the newest samples stay flush right

### Host Sensors (local only)
- Enabled when the normalized server URL is loopback (`localhost`, `127.*`, `::1`, `0.0.0.0`) or this machine's hostname; a remote server never shows the panel
- CPU load: `/proc/stat` deltas; CPU temperature: k10temp/coretemp/zenpower `temp*_input`, else a CPU thermal zone
- GPU load/temperature: amdgpu sysfs (`card*/device/gpu_busy_percent`, `hwmon/*/temp*_input`, preferring the `edge` label)
- RAM: `/proc/meminfo` (`MemTotal - MemAvailable`)
- `HostSampler.sample()` runs once per poll; missing sources render as "—"
- `--local` forces the Host panel on (even with a remote `--server`) for testing; `--no-host` forces it off

### Web View (optional)
- Off by default; `--web`/`--port` starts `WebServer` on `--listen` (default `0.0.0.0`) and `--port` (default `8080`)
- `/` serves a minimal xterm.js page from jsDelivr; `/ws?cols=N&rows=M` streams `YAMDApplication.render_ansi()` frames
- `render_ansi()` uses the same `_layout_for(width,height)` as the terminal, with a truecolor Rich Console writing to a string
- Frames are full-screen ANSI prefixed with home and erase-below; scrollback is disabled and YAMD accepts view/target/request-scroll commands (never quit)
- The server renders each unique (cols, rows) once per tick at the app rate (1–2 Hz) and sends only changed frames
- Browser resize reconnects with new cols/rows; `--web-only` skips the terminal Live loop (the broadcast task still drives debug logging)

### Layout
- Left column ratio 3 (Metrics flex + Performance/Session/Engine fixed 5 rows, plus Host when local), right ratio 4 (Cache & Pool fixed 12 rows + Requests flex)
- Modes: `full` → Metrics + Performance + Session + Engine + Host (local); `no_session` → Metrics + Performance + Engine + Host (local medium height); `compact` → Metrics + Performance
- Metrics blank spacers go first on short terminals (content + borders must fit)
- Labels abbreviate below 40 cols inner width, tiny mode below 30; tagline below 118, short clock below 110, minimal header below 76
- Request rows fit the terminal after fixed overhead (`REQUEST_OVERHEAD`)

### Prometheus Parsing
- `_parse_prometheus()` keeps metric names with colons, discards labels, skips malformed lines, last value wins
- `/metrics` is fetched as text, never JSON

## CLI Interface

```bash
yamd [--server URL] [--kind KIND] [-t TARGET ...] [--config FILE]
     [--api-key KEY] [--requests N|all] [--debug FILE]
     [--view monitor|detail|split|auto] [--cycle S] [--hold S] [--split PCT]
     [--local | --no-host] [--web] [--port PORT] [--listen ADDR] [--web-only]
```

- `-s, --server` — single target URL (default: `http://127.0.0.1:8731`); auto-prepends `http://` if scheme omitted
- `--api-key` — Bearer key sent to every endpoint except `/health` (`HALOGEN_API_KEY`)
- `--requests` — Detail request rows per page: a count or `all` (default: `all`); the retained ring behind the page is scrollable
- `--debug` — Path to JSONL debug log; writes raw `/health`, `/metrics`, `/cache` and the local `host` sample once per poll snapshot
- `--local` / `--no-host` — Override Host-panel auto-detection (force on for a remote test server, or force off on loopback)
- `--web` / `--port N` — Enable the xterm.js web view on port `N` (default `8080`); `--port` implies `--web`
- `--listen ADDR` — Web bind address (default `0.0.0.0`; use `127.0.0.1` for localhost only)
- `--web-only` — Enable the web view and skip the terminal dashboard

## Dependencies

- `rich` — Terminal rendering
- `aiohttp` — Async HTTP client and the optional web view server
- Python ≥ 3.10

## Development Notes

### Running
```bash
pip install -e .
yamd  # or: python yamd.py
```

### Key Files
- `yamd.py` — Single-file application
- `pyproject.toml` — Package metadata, entry point (`yamd = yamd:main`)
- `README.md` — User documentation
- `LICENSE` — MIT license

### Debugging
- Use `--debug dump.jsonl` to capture raw server responses (one record per poll snapshot)
- Collector keeps a 120-sample history ring and a 500-window request ring
- `_state_log` tracks last 10 state transitions (footer shows last 5)

### Signal Handling
Graceful shutdown on `Ctrl+C` (SIGINT) and SIGTERM.

## Testing Halogen locally

```bash
curl -s http://127.0.0.1:8731/health | python3 -m json.tool
curl -s http://127.0.0.1:8731/metrics | grep '^halogen:'
curl -s http://127.0.0.1:8731/cache | python3 -m json.tool
```

The reference test server used during development was
`http://192.168.1.100:8731` (no API key).

## Extending / Modifying

| To change... | Look in... |
|--------------|------------|
| Poll interval | `POLL_INTERVAL = 1.0` / `HalogenCollector.__init__` (`poll_interval`) |
| History length | `HISTORY_MAX = 120` |
| Request ring size | `REQ_EVENT_MAX = 500` |
| State log size | `_log_max = 10` |
| Offline thresholds | `OFFLINE_THRESHOLD = 4`, reconnect-after-2 in `_poll_loop` |
| Color thresholds | `make_metrics_panel` (KV bar), draft-acceptance bands |
| Panel layout | `create_layout()`, panel builder functions, `LEFT_RATIO` / `RIGHT_RATIO` |
| Endpoints | `HalogenCollector` class constants (`HEALTH_ENDPOINT`, `METRICS_ENDPOINT`, `CACHE_ENDPOINT`) |
| Metric names | module-level `M_*` constants |
| Request depth | `--requests` flag → `req_rows` (page cap); scroll state in `TargetRuntime.req_anchor` / `req_page_rows` |
