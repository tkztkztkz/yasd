# Changelog

All notable changes to YAMD (Yet Another Multi Dashboard, formerly YAHD) are documented here.
The 1.x entries describe the original YASD (Strata) dashboard this project was
retargeted from.

## [2.2.0] - 2026-11-11

### Added
- The Requests table is now scrollable through the retained ring (up to 500
  rows). `PgUp`/`PgDn` page, `j`/`k` step one row, `Home` returns to the newest
  row and `End` jumps to the oldest. The page is anchored by row key, so a new
  completion arriving at the head of the ring does not shift the rows being
  read, and the title shows the visible range (`13–30 of 500 kept`). Scrolling
  pauses automatic rotation until `Home` returns to the newest row. The keys
  are active in the detail and split views only.
- The web view's button bar gains `Req ▲` / `Req ▼` buttons for the same
  request scrolling; `PgUp`/`PgDn` from a browser keyboard work too.

### Changed
- `--requests N` still caps one page; it no longer limits how far back the
  Requests table can scroll.

## [2.1.1] - 2026-11-09

### Changed
- **`yamd.py` is now the single source.** The Strata collector/panels from
  `yasd.py` and the Halogen collector/panels from `yahd.py` are merged into
  `yamd.py`; both modules and the `yahd` entry point are gone. A classic
  single-server layout is `yamd --server URL --view detail`. Tests and docs now
  reference `yamd` only.
- Deduplicated the merged code: both collectors share `_CollectorBase`
  (lifecycle, hardened HTTP session, health fetch, poll-error policy, state log
  and accessors), and both detail views share the panel geometry, usage-bar,
  performance, card-list and request-table helpers.

## [2.1.0] - 2026-11-06

### Added
- **YAMD** (`yamd.py`), a multi-target frontend that monitors several Strata and
  Halogen instances at once. `yasd` and `yahd` are unchanged and still work as
  single-server tools.
- Target definitions from `~/.config/yamd/targets.toml` (or `.json`) and/or
  repeatable `-t [NAME=][KIND@]URL` options. API keys are per target
  (`api_key` / `api_key_env`); `kind = "auto"` probes `/cache` and `/metrics`.
- The config file can also carry the web view settings under `[web]`
  (`enabled`, `port`, `listen`, `only`). CLI flags override them, and
  `--no-web` disables a config-enabled view.
- The Engine panel title now carries the server version (Strata engine version,
  or Halogen API/engine version) that the single-server dashboards kept in
  their header; the label shortens automatically on narrow terminals and uses
  the same emphasis as the `Engine` label. The monitor header's full title is
  bright white instead of dim, so it no longer blends into the blue bar.
- Strata: read `engine.drafting` / `engine.drafting_note` and show the active
  drafter in the Engine panel (`Draft: DFlash2 · lookup 3 · spec 8 · pool 15`,
  `Draft: MTP ≤5 ...`, or `Draft: off`). This keeps newer Strata builds that
  support DFlash2 distinguishable; older builds keep the `Spec:` row.
- YAMD's Detail request table now fills the available rows by default.
  `--requests N` caps it, while `--requests all` (the default) fills the panel.
  Strata only supports its 12-row default or `?requests=all`, so the collector
  seeds the full request ring once at startup, then polls the 12-row default
  and merges new rows by `id` into a local ring (up to 500). This fills the
  panel without requesting the full ring on every poll.
- Strata now keeps a client-side 120-sample graph ring, seeded from the
  server's 60-sample history and extended once per second. Hardware and
  Cache & Pool therefore share the same 120-second window and draw the newest
  2 × width samples one per virtual column. Short rings are left-padded while
  filling instead of resampled, so the braille curve scrolls smoothly without
  dots growing or shrinking. Both card lists also use the same
  label/value/graph width clamps.
- Three views: **monitor** (all models with IDLE/BUSY/QUEUED state and the last
  request digest), **detail** (the original YASD/YAHD panels for the selected
  target) and **split** (target list on top, detail below). The split view
  shrinks the list to its content, with `--split` acting as the upper bound, so
  a short target list gives the detail panels the extra rows.
- Automatic rotation across busy/queued targets (`--cycle`), stopping on the
  last-used target when everything goes idle. `TAB` / `Shift-TAB` / `1`-`9`
  switch manually (`--hold`), and `a` toggles rotation.
- A small POSIX/Windows key reader for the above, plus `m` / `d` / `s` view
  switching and `q` to quit.
- The optional xterm.js web view now works with YAMD as well, and is
  interactive: a bottom button bar and keyboard input switch the view/target
  and toggle rotation. `q` / Ctrl-C are filtered out, so a browser cannot stop
  the dashboard. YAHD's own web view remains display-only.
- `--debug` writes one JSONL record per target and poll, tagged with the target
  name and kind.

## [2.0.0] - 2026-11-03

### Changed
- **Retargeted from Strata to Halogen** (`halogen-flash-server`). The module,
  package and CLI are now `yahd` (the file is `yahd.py`, the entry point is
  `yahd = yahd:main`); the default server is `http://127.0.0.1:8731`.
- Polling now reads `/metrics` (Prometheus text) and `/cache` every second;
  `/health` runs in its own task every 10 s instead of the old
  `/health` + `/metrics` + `/v1/status` fan-out.
- Added `_parse_prometheus()`: metric names keep their colons
  (`llamacpp:*`, `halogen:*`), labels are discarded, malformed lines skipped.
- State badge vocabulary is Halogen's: IDLE / BUSY / QUEUED, plus DEGRADED
  (capability probe failed) and NO ENGINE (`halogen:engine_up = 0`). The
  Strata-only SLEEPING/unloaded state is gone, and so are READING/GENERATING:
  Halogen exposes no live per-request phase (its counters land at completion),
  so claiming one was misleading.
- Metrics panel: Strata's context gauge and per-request progress are replaced by
  a KV-pool usage gauge (`/cache.pool.usage_ratio`), context × slots, in-flight
  slots and queue depth, busy elapsed seconds, prompt-cache hit rates, and
  structured-output/strict-tool counters.
- Performance panel: prefill/decode speeds derived from the deltas of
  `llamacpp:prompt_tokens_total` / `prompt_seconds_total` and
  `tokens_predicted_total` / `tokens_predicted_seconds_total`; sources are
  `last req` and `session avg`. The figures match Halogen's own response
  `timings`.
- Session panel: request count seen since monitor start vs server lifetime
  total, processed vs cached prompt tokens, generated tokens. Strata's "since"
  date stamp was removed (Halogen has no server start field).
- Engine panel: KV dtype and pool positions, drafter (`mtp`/`serial`) and
  prompt-lookup n-gram/chain, prompt-cache mode/cap/disk tier.
- Added a **Host** panel for loopback servers (`localhost`, `127.*`, `::1`,
  `0.0.0.0`): CPU and GPU load/temperature plus system RAM usage, read from
  `/proc/stat`, `/proc/meminfo`, k10temp/coretemp and amdgpu sysfs. It is not
  shown for a remote server. Layout modes: `full`, `no_session` (medium local
  height keeps Host and drops Session) and `compact`.
- `--local` forces the Host panel and local sampling on even when `--server` is
  remote (useful to preview the sensors before deploying on the LLM host);
  `--no-host` forces it off on a loopback monitor.
- Added an optional **web view** (off by default): `--web` or `--port N` starts an
  aiohttp server (default 8080, `--listen 0.0.0.0`), `--listen 127.0.0.1` restricts it
  to localhost, and `--web-only` skips the terminal TUI. `/` serves an xterm.js page and
  `/ws` streams the same Rich frames as truecolor ANSI; the browser's cols/rows size the
  render and resizing reconnects. Frames are sent only when they change.
- Hardware panel replaced by **Cache & Pool**: 10 cards (Decode, Prefill,
  KV pool, Cache hit, Token hit, Entries, Cache size, Draft acc, In flight,
  Queue wait), each with the existing btop-style braille graph.
- Requests panel: Halogen has no request ring, so rows are client-derived
  completion windows from `halogen:requests_total` deltas, showing Time, Reqs,
  Prompt, Cached, Output, separate PP/TG tok/s and the window's Draft
  acceptance. `--requests all` uses a 500-window local ring. As the panel
  narrows, Draft drops first, then PP, while TG stays to the end.
- Graph history now comes from the dashboard's own two-minute ring
  (`HISTORY_MAX = 120` at 1 s) instead of the server's history series. Each
  card draws its newest 2 × width samples, one per braille column, so the
  curve advances one column per poll instead of stalling on narrow cards.
  Decode/Prefill plot each completed window's rate (a spike per request, zero
  between requests) rather than repeating the last-request display value.
- Header: `YAHD Yet Another Halogen Dashboard · Halogen <version>`; a minimal
  header below 76 columns keeps the address and status readable.
- Debug log writes raw `/health`, `/metrics` (text) and `/cache` once per poll
  snapshot instead of once per rendered frame.
- API-key handling: `Bearer` to every route except `/health`; 401 while
  `/health` answers stays ONLINE.
- `/health` moved to its own background task (`HEALTH_REFRESH = 10 s`) because
  its engine probe can block for ~1 s. `/metrics` and `/cache` now keep an
  exact 1 s schedule (monotonic-clock pacing), so the graph scroll no longer
  pauses every health refresh; a health failure is retried after 1 s and the
  live in-flight/queue/busy fields are read from `/metrics` first.
- KV-pool bar: still green → orange → red, but the >95 % blinking was removed.
  Halogen fills the pool close to capacity by design, so a full red bar is the
  normal state and flashing was noise.
- `--server` accepts a bare host, a trailing slash, or the OpenAI-style `/v1`
  base; `/v1` is stripped because the dashboard routes live at the API root.
- The first successful poll connects immediately; two consecutive successes
  are only required when recovering from an outage.

### Removed
- All Strata-only panels and fields: experts slots/cache, MTP draft budget,
  lookup/pool workers, KV quant mapping, conversation cache slots, images/cvec,
  the server-reported GPU/CPU/VRAM/temp/power/PCIe/disk/RAM cards, server
  history series, per-request ring, `loaded`/SLEEPING handling, and the
  `/v1/status` endpoint. (A local-only Host panel now reads this machine's
  sensors directly.)

### Verified
- Rendered at 60×24, 70×24, 80×25, 100×30, 120×40 and 140×45 without clipping
  the model row; decoder/prefill figures checked against a live Halogen
  0.18.21 server's `timings` (42.3 tok/s vs 42.305, 324.9 tok/s vs 324.93).
- HostSampler read live k10temp/amdgpu sysfs on an AMD Strix Halo-class host
  (CPU load/temp, GPU load/temp, RAM); a fake loopback Halogen server confirmed
  the panel and its JSONL debug record, while a remote URL sampled nothing.

## [1.2.0] - 2026-10-11

### Changed
- Session panel: the Reqs "since" stamp now carries the date, not just the
  time. Previously `123 since 14:30`; now `123 since Oct 11 14:30` on wide
  terminals (`make_session_panel`, `yasd.py`).
- The stamp is chosen responsively so the fixed 5-row Session panel never
  wraps: it picks the widest date+time form that fits the value column
  (left inner width minus the 8-col label). Tiers, widest first: human
  `Oct 11 14:30` (`MONTHS[m]` + space-padded day + `%H:%M`), compact
  `10-11 14:30`, then time-only `14:30` as the floor.
- Added a module-level `MONTHS` tuple of locale-independent month
  abbreviations, so the stamp reads the same on every platform (Windows
  `strftime`'s `%b` follows the active locale).

### Docs
- Version bumped to `1.2.0` in `pyproject.toml` and the `yasd.py` docstring.

### Verified
- Tier ladder exercised across terminal widths 60-100: human form from 76
  cols up, compact at 74, time-only at 72 and below; `py_compile` clean.

## [1.1.1] - 2026-10-04

### Changed
- Header: the "Strata {version}" label and value now render `bold cyan`
  instead of `dim cyan`, matching the "YASD" label colour (`make_header`,
  `yasd.py`).
- Default request rows shown in the Requests panel bumped from `5` to `12`
  (`--requests` default in `main()`, `yasd.py`).

### Docs
- Version bumped to `1.1.1` in `pyproject.toml` and the `yasd.py` docstring.

## [1.1.0] - 2026-10-04

### Changed
- Hardware panel: replaced the single-colour dim-grey ASCII block sparkline
  (`_sparkline`, `▁▂▃▄▅▆▇█`, one sample per character) with a btop-style
  braille history graph (`_braille_graph`, `yasd.py`).
- Graph cells carry two samples side by side with four dot rows per cell,
  filled bottom-up (`_braille_cell`, `_BRAILLE_UP` table indexed `left * 5 +
  right`, unlit cell renders as space so an all-zero series stays blank).
- Sample-to-row mapping (`_braille_level`) keeps btop's upward bias and
  C++-style rounding, with a floor of one lit row for any non-zero sample.
- Resolution doubled: a `width`-wide card now plots `2 * width` samples
  instead of one block per character.
- Bucketing unchanged in spirit: the server's 60 one-second history samples
  are bucketed to the card's cell width with each bucket drawn at its hottest
  sample; a short/partial ring is left-padded so the newest samples stay flush
  right.
- Each of the 10 hardware cards (Speed, GPU load, VRAM, GPU temp, Power,
  PCIe, CPU, Disk read, Experts VRAM, System RAM) now draws its graph in its
  own hue from the new `GRAPH_COLORS` tuple; the graph column lost its `dim`
  style so the colour survives (Rich folds it down when truecolor is
  unavailable).
- Edge handling: `width < 1` returns `""`, empty history returns spaces to
  keep the column full.
- Docs: `AGENTS.md`, `CLAUDE.md`, `QUICK-START.md`, `README.md` updated from
  "sparkline" wording to "btop-style braille history graph in its own colour"
  / "colour-coded braille graphs" / "history series".
- `README.md`: added a `Version 1.1.0` banner and simplified Prerequisites to
  "A running Strata instance" (dropped the `python serve/server.py --port
  8080` example).
- Version bumped to `1.1.0` in `pyproject.toml` and the `yasd.py` docstring.

### Fixed
- Adjacent hardware history lines no longer blend into one indistinguishable
  grey series; small-but-live series no longer read as missing data.
- All-zero history no longer renders as tofu boxes on fonts without a Braille
  Patterns face (renders blank instead).

### Verified
- Rendering checked at 120x40, 100x30, and 80x25; layout (ten one-line
  cards) untouched.

## [1.0.0] - 2026-10-02

- Initial release: real-time terminal UI polling Strata `/health`, `/metrics`,
  and `/v1/status` every 500 ms and rendering at 10 FPS with Rich.
