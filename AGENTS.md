YAMD (Yet Another Multi Dashboard) v2.2.0 is a real-time terminal UI for monitoring
several Halogen (https://github.com/peonist-ai/halogen-flash-server) and Strata
instances at once. `yamd.py` is the single source for both collectors, their detail
panels (the former YASD and YAHD dashboards) and the multi-target frontend.

Copyright 2026 Antani Technologies BV — MIT License

## Multi-target frontend (YAMD)

`yamd.py` starts one collector per configured target and adapts each snapshot into a
shared `TargetView` (badge, model, live slots/queue, last
request digest). Targets come from `~/.config/yamd/targets.toml` (or `.json`) and/or
repeatable `-t [NAME=][KIND@]URL` options; `kind = "auto"` probes `/cache` then
`/metrics`. The same file's optional `[web]` table (`enabled`, `port`, `listen`,
`only`) supplies the xterm.js view defaults; CLI flags override it and `--no-web`
disables it. The optional `[ui]` table picks the startup `view` (`monitor`,
`detail`, `split`, or `auto` with `auto_split_max`) and `blink` (default false:
Strata's >95% context warning becomes a solid red bar and the idle redraw rate is
1 Hz; true keeps the blink and redraws at 2 Hz). Keys and buttons still redraw
immediately. Views: `monitor` (all models + digest), `detail` (the Strata/Halogen
panels for the selected target) and `split` (list on top, detail below; the list uses
only the rows it needs and `--split` is its upper bound, so few targets leave more room
for the detail). The Engine panel title carries the Strata/Halogen version that the
single-server dashboards put in their header, shortened to fit the panel. Strata's
`engine.drafting` (`mtp`, `dflash2`, `off`) is shown in the Engine panel as `Draft:`;
older builds without the field keep the `Spec:` row. The Detail request table fills
the available rows (`--requests N` caps the page). Strata only supports its 12-row
default or `?requests=all`, so YAMD seeds the full ring once at startup, then polls the
12-row default and merges rows by `id` into a local ring (up to 500). `PgUp`/`PgDn`
page and `j`/`k` step through that ring, `Home` returns to the newest row and `End`
jumps to the oldest; the view is anchored by row key, so new completions do not shift
a page being read, and scrolling pauses automatic rotation until `Home` returns to the
newest row. The selection follows BUSY/QUEUED targets every `--cycle` seconds and
freezes on the last-used target when all are idle; `TAB`/`Shift-TAB`/`1`-`9` switch
manually (`--hold`), `a` toggles rotation, `m`/`d`/`s` change view and `q` quits. The
web view's browser page enables input (a button bar plus the same keys) for
view/target switching, request scrolling and the rotation toggle, but filters
`q`/Ctrl-C so a browser cannot quit.
Unit tests live in `tests/test_yamd.py`.

## Core function

Runs one async background collector per configured target (a single `asyncio` loop, no
thread/lock). A Halogen target polls `/metrics` and `/cache` every second, refreshing
`/health` every 10 s in its own task (its engine probe can block for about a second); a
Strata target polls `/metrics` every 500 ms with `/health`/`/v1/status` alongside. The
selected target renders as a flicker-free full-screen terminal dashboard using the Rich
(https://github.com/Textualize/rich) library at 1–2 Hz (2 Hz when Strata's context blink
is enabled). With `--web` / `--port` it also serves the same Rich frames to a browser
over xterm.js, with view/target controls but no quit shortcut.

## Dashboard panels

- **Header** — connection status (ONLINE / OFFLINE / ENGINE DOWN / DEGRADED), server
  address (stripped of scheme), Halogen API version, and timestamp
- **Metrics (left)** — state badge (IDLE / BUSY / QUEUED / DEGRADED / NO ENGINE),
  KV-pool usage bar with the same colour thresholds as before
  (green ≤ 69.9 %, orange 70–85 %, red > 85 %), max context and slot
  count, last completed request, in-flight slots and queue, busy elapsed seconds,
  processed/generated token totals, draft acceptance, prompt-cache hit rates, structured
  request counts, and model name
- **Performance (left)** — prefill and decode speed side-by-side as `X.X tok/s`, with
  `last req` / `session avg` source sub-labels
- **Session (left)** — requests seen since the dashboard started vs the server lifetime
  total, processed prompt tokens vs cached tokens (token hit %), generated tokens
- **Engine (left)** — KV dtype and pool positions, drafter + prompt-lookup settings,
  prompt-cache mode / cap / disk tier
- **Host (left, local only)** — CPU load/temperature, GPU load/temperature and system RAM
  usage. Shown only when `--server` points at loopback/localhost, because the values are
  read from the machine running the dashboard (the LLM host).
- **Cache & Pool (right)** — 10 one-line cards (Decode, Prefill, KV pool, Cache hit,
  Token hit, Entries, Cache size, Draft acc, In flight, Queue wait), each with value plus
  a btop-style braille history graph in its own colour
- **Requests (right, flex)** — one scrollable page of the newest completion windows
  derived from counter deltas (Time, Reqs, Prompt, Cached, Output, PP t/s, TG t/s,
  Draft); as the panel narrows the columns drop in the order Draft → PP, while TG stays
  to the end. The title shows the visible range, and the retained ring is scrollable up
  to 500 rows
- **Footer** — activity log of the last 5 state transitions

## Key technical details

- Halogen is a single-model server with N concurrent slots (4 by default) behind a queue.
  `/health` reports `in_flight`, `queued`, `slots`, `slot_ctx`, `context`, `busy_for_s`,
  `longest_wait_s`, `capability_probe`, the drafter, and prompt-cache settings.
- Halogen's `/health` can block for ~1 s while the server re-probes the engine, and most
  of what it carries is static (model, version, context, slots, drafter, cache settings).
  It runs in its own task (`HEALTH_REFRESH = 10.0` s), so `/metrics` and `/cache` keep an
  exact 1 s cadence; the live in-flight/queue/busy fields are read from `/metrics`
  first. A health failure is retried after 1 s and does not by itself mark the server
  offline.
- State badges are Halogen's own: `in_flight > 0` → BUSY, `queued > 0` overrides to
  QUEUED, `engine_up == 0` → NO ENGINE, and a failed `capability_probe` → DEGRADED.
  There is no "unloaded" state to show as SLEEPING, and no live prefilling/decoding
  phase in the API, so there is no READING/GENERATING badge.
- Halogen writes its token counters when a request **finishes**. This dashboard derives
  per-request speeds from the deltas of `llamacpp:prompt_tokens_total` /
  `prompt_seconds_total` and `tokens_predicted_total` / `tokens_predicted_seconds_total`,
  so `last req` figures match the response `timings` exactly. While a request is still
  running there is no live token rate to show: the badge stays BUSY and `busy_for_s`
  carries the elapsed time.
- Because `/metrics` is cumulative and has no request ring, recent-request rows are built
  by the client: each poll window in which `halogen:requests_total` moved becomes one row,
  with the window's token deltas, its PP (prefill) and TG (decode) rates from
  `prompt_tokens_total`/`prompt_seconds_total` and
  `tokens_predicted_total`/`tokens_predicted_seconds_total`, and its Draft acceptance
  from `halogen:draft_tokens_accepted_total / draft_tokens_total`. The whole
  client-side ring (up to 500 windows) is retained and can be paged with
  `PgUp`/`PgDn`, `j`/`k`, `Home` and `End`; `--requests` only caps one page.
- History graphs use the dashboard's own two-minute ring (120 samples at 1 s); each card
  draws its newest 2 × width samples, one per braille column, so the curve advances one
  column per poll. Decode/Prefill plot the completed window's rate (a spike per request,
  zero between requests), not the retained last-request display value. Halogen publishes
  no GPU/CPU/host sensors, so there are no hardware cards.
- KV-pool usage comes from `/cache.pool.usage_ratio` (falling back to
  `llamacpp:kv_cache_usage_ratio`), displayed capped at 99.5 % so it never reads 100 %.
- Cache hit rates come from `/cache`: `hit_rate` (requests) and `token_hit_rate` (prompt
  tokens). `/cache` may report stale counters during a long cold prefill; `stale_s` is
  surfaced as `age Ns` on the Cache size card when it is 5 s or older.
- Draft acceptance is `halogen:draft_tokens_accepted_total / draft_tokens_total`; the
  Engine panel also shows the drafter (`mtp`/`serial`) and prompt-lookup n-gram/chain.
- `/health` is the only route that skips authorization and answers even when
  `HALOGEN_API_KEY` is set, so it is the key-free probe; `--api-key` sends `Bearer` to
  every other endpoint. Connectivity itself is judged by the 1 s `/metrics` poll
  because it is the endpoint that must keep working.
- Offline policy: 4 consecutive failed poll cycles (~4 s at 1 s) before reporting
  OFFLINE; the first successful cycle connects immediately, and 2 consecutive successes
  bring it back after an outage; a 401 on `/metrics` while `/health` answers stays
  ONLINE and reports "API key rejected"; malformed Prometheus or JSON keeps the last
  good snapshot and never flips connectivity.
- Number formatting mirrors the previous dashboard (`_num` / `_kfmt` / `_gb`); its
  btop-style braille cells (2 samples per character, 4 dot rows per cell, filled
  bottom-up, unlit cell = space) draw the new cache/throughput series, each card's line
  in its own colour from `GRAPH_COLORS`; the newest 2 × width samples are drawn one per
  virtual column. Strata keeps a client-side 120-sample ring seeded from the server's
  60-sample history and extended once per second, so Strata and Halogen draw the same
  120-second window. A ring that is still filling is left-padded, and no resampling is
  done, so the curve scrolls one column per second without dots growing or shrinking.
  The card geometry clamps are identical between Hardware and Cache & Pool.
- The Host panel is enabled only when the normalized server URL is loopback
  (`localhost`, `127.*`, `::1`, `0.0.0.0`) or this machine's hostname. It samples
  `/proc/stat` for CPU load, `/proc/meminfo` for RAM, k10temp/coretemp/zenpower (or the
  thermal zone) for CPU temperature, and amdgpu sysfs (`gpu_busy_percent`,
  `hwmon/*/temp1_input`) for GPU load/temperature. Missing sources render as "—"; a
  remote server never shows the panel unless `--local` forces it. The collector samples
  it once per poll.
- Layout is rebuilt every frame from terminal size: left column ratio 3 (Metrics flex +
  Performance/Session/Engine fixed 5 rows, plus Host when local), right column ratio 4
  (Cache & Pool fixed 12 rows + Requests flex). Layout modes are `full`, `no_session`
  (local medium height: Session drops, Host stays) and `compact` (Metrics + Performance
  only); blank Metrics spacers go first on short terminals.
- Request rows fit the terminal after fixed-panel overhead, capped by
  `--requests N | all` per page; the ring behind the page holds up to 500 rows
- Optional web view: off by default. `--web` or `--port N` starts an aiohttp server
  (default port 8080, bind `--listen 0.0.0.0`). `/` serves an xterm.js page and `/ws`
  streams the current Rich frame as truecolor ANSI (scrollback disabled, no input).
  The browser sends its cols/rows in the WebSocket query and reconnects on resize; the
  server renders per unique size at the app's rate (YAHD 5 FPS, YAMD 1 Hz, or 2 Hz
  with `[ui] blink`) and only sends changed frames. `--listen
  127.0.0.1` restricts it to the local machine; `--web-only` skips the terminal TUI.
  There is no web authentication, so the default `0.0.0.0` is a trusted-network choice.
- Accepts `--server` / `-s` for the Halogen URL (default http://127.0.0.1:8731);
  auto-prepends `http://` if scheme is omitted
- Accepts `--api-key` for key-protected servers and `--requests N|all` for request-table
  depth
- `--local` forces the Host panel and local sensor sampling even when `--server` is
  remote (for testing); `--no-host` forces it off on a loopback monitor
- Optional `--debug` flag writes raw `/health`, `/metrics`, and `/cache` responses (plus
  the `host` sample when local) as JSONL, once per poll snapshot (1/s), not once per
  rendered frame
- Runs with Ctrl+C or SIGTERM graceful shutdown
- Runs with `screen=True` for the terminal UI unless `--web-only` is set
