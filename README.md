# YAMD - Yet Another Multi Dashboard

A real-time terminal UI for monitoring several Halogen (halogen-flash-server) and
Strata instances at once. One file. One command. One screen. Zero fuss.

`yamd.py` is the single source: it contains both collectors, their detail panels (the
former YAHD and YASD dashboards) and the multi-target frontend. A single-server
deployment is just YAMD with one target.

> **Provenance:** YAMD is a fork of YASD (Yet Another Strata Dashboard),
> extended with Halogen support, multi-target monitoring via `targets.toml`
> and the xterm.js web view. "YAMD" is this fork's name; YASD is copyright
> (C) 2026 Antani Technologies BV and its notice is kept in `LICENSE`.

## Prerequisites

- Python 3.10+
- A running Halogen API server (default API port `8731`)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
yamd
```

Or without installing the entry point:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install rich aiohttp
python yamd.py
```

On Windows, activate with `.venv\Scripts\activate`.

## Usage

```bash
python yamd.py                                # connect to localhost:8731
python yamd.py --server 192.168.1.10:8731     # connect to a remote server (scheme optional)
python yamd.py --server 192.168.1.10:8731/v1  # API base also works; /v1 is stripped
python yamd.py --api-key "$HALOGEN_API_KEY"   # servers with HALOGEN_API_KEY set
python yamd.py --requests all                 # fill the terminal with the client's request ring
python yamd.py --debug dump.jsonl             # log raw server responses as JSONL
python yamd.py -s 192.168.1.100:8731 --local  # force this machine's Host sensors while testing
python yamd.py --no-host                      # hide the Host panel on a loopback monitor
python yamd.py --web                          # terminal + browser at http://127.0.0.1:8080/
python yamd.py --web --port 9000 --listen 127.0.0.1  # web on localhost only
python yamd.py --web-only --port 8080         # web view without the terminal dashboard
python yamd.py --server 127.0.0.1:8731 --view detail  # the classic single-server layout
```

## Multi-target monitoring: YAMD

`yamd.py` monitors several Strata and Halogen instances at once. A single `--server`
(or the default localhost target) is the classic one-server invocation; use
`--view detail` to start directly in the full per-target panel layout.

```bash
yamd                                              # config file or localhost:8731
yamd -t main=halogen@127.0.0.1:8731 \
     -t strata@10.0.0.5:8080                      # repeatable; kind is optional
yamd --config ~/.config/yamd/targets.toml
yamd --view split --split 35 --cycle 5 --hold 15
yamd --web --port 8080                            # interactive browser view
```

Targets can live in `~/.config/yamd/targets.toml` (or `.json`):

```toml
[[target]]
name = "halogen-main"
kind = "halogen"              # strata | halogen | auto
url  = "http://127.0.0.1:8731"
api_key_env = "HALOGEN_API_KEY"

[[target]]
name = "strata-70b"
kind = "strata"
url  = "http://10.0.0.5:8080"
# api_key = "local-test-key"  # direct value; local/test servers

[ui]
view = "auto"                 # monitor | detail | split | auto
auto_split_max = 3            # with auto: split at <= 3 targets
blink = false                 # true: Strata >95% blink, 2 Hz redraw

[web]
enabled = true                # or use --web on the command line
port = 8080
listen = "127.0.0.1"          # 0.0.0.0 to expose it on the network
only = false                  # true = no terminal TUI (like --web-only)
```

`-t` accepts `NAME=KIND@URL`, `KIND@URL`, `NAME=URL` or a bare URL. With
`kind = "auto"` YAMD probes `/cache` and `/metrics` once at startup to tell
Strata (JSON) from Halogen (Prometheus text).

Each target accepts either `api_key_env = "VARIABLE_NAME"` or a literal
`api_key = "value"`. A literal `api_key` wins when both are present, which is
handy for local models and test servers that use a dummy key. The global
`--api-key` option is used only for targets that define neither.

The `[web]` table is optional. Command-line flags override it: `--web` and
`--no-web` turn the view on/off, `--port` / `--listen` override the address,
and `--web-only` overrides `only`. Setting `port` in the file does not enable
the web view by itself (`enabled` / `only` do), so a disabled config can keep
its port for later.

The `[ui]` table picks the startup view. `view = "split"` is handy when only a
few targets are configured: the target list takes just the rows it needs, so
the detail panels get almost the whole terminal. `view = "auto"` starts in
`split` for at most `auto_split_max` targets (default 3) and in `monitor` above
that. `--view` overrides the config. A top-level `view = "..."` is accepted as
a shorthand for `[ui].view`.

`blink = false` (the default) turns Strata's >95% context warning into a solid
red bar like Halogen's and redraws the dashboard at 1 Hz. `blink = true` keeps
the 1 s on / 1 s off warning and redraws at 2 Hz so it stays visible. Key
presses, button clicks and automatic target switches still redraw immediately.

In YAMD, the Detail request table fills the rows available in the panel by
default. `--requests N` caps it at N rows; `--requests all` (the default) means
"fill the panel". Strata's API only accepts its 12-row default or
`?requests=all`, so YAMD fetches the full Strata ring once at startup, then
polls the cheap 12-row default and merges new rows by `id` into a local ring
(up to 500). The table therefore fills immediately without requesting the full
ring on every poll.

That retained ring is scrollable: `PgUp`/`PgDn` page, `j`/`k` step one row,
`Home` returns to the newest row and `End` jumps to the oldest. The page is
anchored to its top row, so new completions arriving above it do not shift the
view while you read. Scrolling pauses automatic rotation; `Home` releases it.
The title shows the visible range (for example `13–30 of 500 kept`). Halogen's
500 windows are built by the dashboard while it runs, so they are not kept
across a restart; Strata seeds its server-side ring once at startup.

The Hardware (Strata) and Cache & Pool (Halogen) history cards use the same
graph geometry. Strata now keeps a client-side 120-sample ring seeded from the
server's 60-sample history and extended once per second, so both panels draw
the same 120-second window. Each card uses the newest 2 × width samples, one
per virtual column, and pads the left side only while the ring is still
filling. No resampling is done, so the braille curve scrolls smoothly one
column per second without dots growing or shrinking.

Views and keys:

| Key | Action |
|---|---|
| `m` / `d` / `s` | monitor / detail / split view |
| `TAB` / `Shift-TAB` | next / previous target (held for `--hold` seconds) |
| `1`-`9` | jump to a target |
| `a` | toggle automatic rotation |
| `PgDn` / `j` | Requests: one page / one row older (detail and split) |
| `PgUp` / `k` | Requests: one page / one row newer |
| `Home` / `End` | Requests: newest / oldest row |
| `q` / Ctrl-C | quit |

- **Monitor** lists every model with its state (IDLE / BUSY / QUEUED / OFFLINE /
  DEGRADED / NO ENGINE / SLEEPING), in-flight slots, queue and the last request's
  digest.
- **Detail** shows the Strata or Halogen panels for the
  selected target. The Engine panel's title carries the server version
  (`Engine · Strata 1.2.0`, `Engine · Halogen 2.0.0 · engine 2.1.0`) that the
  single-server dashboards used to put in their header. For Strata it also
  reports the active drafter from `engine.drafting`: `Draft: DFlash2 ·
  lookup 3 · spec 8 · pool 15`, `Draft: MTP ≤5 · ...`, or `Draft: off`. Older
  Strata builds without that field keep the previous `Spec:` row.
- **Split** keeps the target list on top and the selected target's detail
  below. The list takes only as many rows as it needs; `--split` (default 35%)
  is the maximum it can grow to when there are many targets, so a short list
  leaves more room for the detail panels.

The selection follows targets that are busy or queued, switching every
`--cycle` seconds. When everything goes idle it stops on the target that ran
last; activity automatically resumes rotation. `TAB` overrides the rotation for
`--hold` seconds, and `a` turns it off until pressed again. Remote targets never
show the Halogen Host panel; the usual `--local` / `--no-host` overrides apply.

In the YAMD web view, the browser is interactive: click the terminal and use the
same keys (`TAB` / `1`-`9` / `m` / `d` / `s` / `a` / `PgUp` / `PgDn`), or use the
button bar at the bottom. Its `Req ▲` / `Req ▼` buttons page through the Requests
table. The bar shows the rotation mode; its `A−` / `A+` buttons change the
terminal font size (remembered per browser), and `Full` toggles the browser
fullscreen view. The browser frame omits the terminal key-help line (the button
bar replaces it), so that row goes to the dashboard. `q` / Ctrl-C are
deliberately not forwarded, so a browser cannot stop the dashboard.

Tests (unit + mock-server integration):

```bash
python -m unittest discover -s tests -v
```

## Web view (xterm.js)

The web view is **off by default**. `--web` or `--port N` starts an aiohttp server
(default port `8080`, bind `0.0.0.0`). Open `http://<host>:<port>/` in a browser: the same
Rich dashboard is streamed as ANSI frames into xterm.js. Scrollback is disabled and the
layout follows the browser's terminal size.

The page has a button bar and keyboard control for view/target switching, the rotation
toggle, the terminal font size and fullscreen; `q` and Ctrl-C are still not accepted
from the browser, so it cannot stop the process.

```bash
python yamd.py --web-only -s 127.0.0.1:8731
yamd --config ~/.config/yamd/targets.toml --web
```

- `--listen 127.0.0.1` exposes it to this machine only; the default `0.0.0.0` allows other
  hosts on the network to attach. The page has no authentication, so bind loopback (or a
  trusted interface) unless the network is trusted. In YAMD, a remote client can switch
  views/targets and toggle rotation, but cannot quit.
- `--port` implies `--web`; without `--port` the default is `8080`
- `--web-only` does not draw the terminal dashboard
- YAMD's `[web]` config table supplies the same defaults; `--no-web` overrides it
- The page loads xterm.js and the fit addon from jsDelivr, so the browser needs access to
  that CDN

## What it shows

The dashboard is split into a few panels:

- **Header** -- connection status (ONLINE / OFFLINE / ENGINE DOWN / DEGRADED), server
  address, Halogen API version, current time
- **Metrics** -- state (IDLE / BUSY / QUEUED / DEGRADED / NO ENGINE), KV-pool usage
  bar, context and slots, last completed request, in-flight slots and queue, busy
  elapsed time, processed/generated tokens, draft acceptance, prompt-cache hit rates,
  structured-output counters, model name
- **Performance** -- prefill and decode speed side by side (`last req` or `session avg`)
- **Session** -- requests seen since the dashboard started vs the server lifetime total,
  processed vs cached prompt tokens, generated tokens
- **Engine** -- KV dtype and pool positions, drafter and prompt lookup, prompt-cache mode
- **Host** -- CPU/GPU load and temperature, system RAM usage. Appears only when
  `--server` is `localhost`, `127.0.0.1` (or another loopback address), because the
  figures are read from the machine running the dashboard
- **Cache & Pool** -- decode/prefill throughput, KV-pool occupancy, request and token
  cache hit rates, entries, cache size, draft acceptance, in-flight slots, queue wait,
  each with a colour-coded braille history graph
- **Requests** -- completion windows derived from Halogen's counters, with request count,
  prompt/cached/output tokens, separate PP (prefill) and TG (decode) tok/s, and the
  window's Draft acceptance
- **Footer** -- last few state transitions in an activity log

Halogen writes its token counters when a request finishes, so speeds are the last
completed request's own average (matching the response `timings`) or the server-lifetime
average before one has finished; while a request runs the badge stays BUSY and the busy
timer carries the elapsed time. Halogen exposes no live prefilling/decoding phase, so
there is no READING/GENERATING badge (unlike Strata's per-request state). Halogen itself
publishes no GPU/CPU/host sensors; when YAMD runs on the LLM host (a loopback `--server`),
it reads CPU/RAM from `/proc`, CPU temperature from k10temp/coretemp, and GPU
load/temperature from amdgpu sysfs, and shows them in the Host panel below Engine. A
remote server deliberately has no Host panel, because those values would describe the
wrong machine. For an early check, `--local` forces the Host panel on while connected to
a remote test server, and `--no-host` hides it on a loopback monitor.

If the server goes away the body is replaced by a CONNECTION LOST panel until it returns.
`/health` answering while the engine is down shows ENGINE DOWN, not OFFLINE.

## Dependencies

- [rich](https://github.com/Textualize/rich) -- terminal rendering
- [aiohttp](https://docs.aiohttp.org/) -- async HTTP client

## License

MIT License. See [LICENSE](LICENSE) for the full text.

YASD is copyright (C) 2026 Antani Technologies BV.
