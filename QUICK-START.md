# YAMD — Quick Start

## Setup (venv)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # installs rich + aiohttp and the yamd command
```

## TL;DR

```bash
# 1. Start Halogen (API on port 8731; see the server's docs for the container run)
# 2. Run YAMD, pointing it at the API port (detail = classic single-server view):
python yamd.py -s http://127.0.0.1:8731 --view detail
```

## Multiple servers (YAMD)

`yamd` monitors several Halogen and Strata servers in one dashboard, with a monitor
list, per-target detail view and a split view:

```bash
yamd -t halogen@127.0.0.1:8731 -t strata@10.0.0.5:8080
yamd --config ~/.config/yamd/targets.toml --view split
```

See the "Multi-target monitoring: YAMD" section of `README.md` for the config file
format, views and key bindings.

---

## What YAMD needs

For a Halogen target, YAMD reads **three routes** from its API port. No log file, no
extra flags, no wrapper script.

| Endpoint | What YAMD gets |
|---|---|
| `/health` | Connectivity probe, version, context/slots, in-flight + queue, drafter, prompt-cache settings (always open, even with `HALOGEN_API_KEY`) |
| `/metrics` | Prometheus counters: prompt/generated tokens, rates, KV-cache occupancy, requests, drafts, cache and queue counters |
| `/cache` | Prompt-cache report: hit rates, entries, bytes, stores, pool regions, disk tier |

Just point YAMD at a reachable Halogen URL and it renders.

> **Host panel:** when `--server` is `localhost`, `127.0.0.1` (or another loopback
> address), YAMD also reads this machine's CPU/GPU load and temperature and RAM usage
> and shows them below Engine. A remote URL never samples local hardware.

---

## Halogen API key

`GET /health` stays open; every other route requires the key when `HALOGEN_API_KEY` is
set. Pass the same key to YAMD:

```bash
python yamd.py --api-key "$HALOGEN_API_KEY"
```

---

## YAMD CLI options

Run `yamd --help` for the full list. The commonly used options are:

```
usage: yamd.py [-h] [--server SERVER] [--name NAME] [--kind {strata,halogen,auto}]
               [-t TARGET] [--config CONFIG] [--api-key API_KEY]
               [--requests REQUESTS] [--debug DEBUG] [--view {monitor,detail,split,auto}]
               [--cycle CYCLE] [--hold HOLD] [--split PCT]
               [--local | --no-host] [--web | --no-web] [--port PORT]
               [--listen LISTEN] [--web-only]

options:
  --server, -s    Single target URL (default: http://127.0.0.1:8731)
  --kind          Kind for --server: strata, halogen or auto (default: auto)
  -t, --target    Add a target: [NAME=][KIND@]URL; repeatable
  --config        TOML/JSON target file (default: ~/.config/yamd/targets.toml)
  --api-key       Bearer key for targets that do not define their own
  --requests      Detail request rows per page: a count, or 'all' (default: all)
  --debug         Path to a JSONL debug log file
  --view          Startup view: monitor, detail, split or auto (default: monitor)
  --cycle/--hold  Rotation interval and manual-selection hold (seconds)
  --split         Target-list height in the split view, in percent (default: 35)
  --local         Force the Host panel on for every target (testing)
  --no-host       Force the Host panel off on loopback targets
  --web           Enable the interactive xterm.js web view
  --port          Web listen port (default: 8080; implies --web)
  --listen        Web bind address (default: 0.0.0.0; 127.0.0.1 for local only)
  --web-only      Enable the web view and skip the terminal dashboard
```

Example with all options:

```bash
python yamd.py \
  --server http://127.0.0.1:8731 \
  --api-key "$HALOGEN_API_KEY" \
  --requests all \
  --debug /tmp/yamd-debug.jsonl
```

To preview the Host panel on a machine that is not the LLM host, point at the test
server and force local sampling:

```bash
python yamd.py -s http://192.168.1.100:8731 --local
```

Web view (interactive):

```bash
python yamd.py --web --port 8080               # terminal + browser
python yamd.py --web-only --port 8080          # browser only
python yamd.py --web --listen 127.0.0.1        # reachable from this machine only
```

Open `http://<host>:8080/`. The page pulls xterm.js from jsDelivr and has no
authentication; bind `127.0.0.1` unless other machines should be able to watch the
dashboard.

- `--requests 10` caps the Detail table at 10 rows per page; `--requests all` fills the
  panel. `PgUp`/`PgDn` (or `j`/`k`) scroll the retained ring, and `Home`/`End` jump to
  the newest/oldest row.
- `--debug` writes the raw `/health`, `/metrics`, `/cache`/`/v1/status` and `host`
  bodies as JSONL, one record per target and poll snapshot.

> **Note:** The `scheme` is optional — `python yamd.py -s 192.168.1.10:8731` works; `http://` is prepended automatically. A trailing `/v1` is also accepted and stripped, because Halogen serves the dashboard routes at the API root.

---

## Verifying it works

After starting both, you should see:

1. **Header** — `YAMD ● ONLINE` with the server URL and target status
2. **Metrics** — State, KV-pool bar, context/slots, last request, in-flight/queue, token
   totals, draft acceptance, cache hit rates, structured counts, model
3. **Performance** — Prefill & decode speed with `last req` / `session avg` sources
4. **Session** — Requests seen vs total, processed vs cached tokens, generated tokens
5. **Engine** — KV dtype/pool, drafter and prompt lookup, prompt-cache mode
6. **Host** — Only when connecting to `localhost`/`127.0.0.1`: CPU/GPU load and
   temperature, system RAM
7. **Cache & Pool** — Throughput, KV pool, cache hit rates, entries, cache size, draft
   acceptance, in-flight slots, queue wait, each with a colour-coded braille graph
8. **Requests** — Newest completion windows with request counts, token deltas, PP/TG
   tok/s, and Draft acceptance
9. **Activity Log** — State transitions at the bottom

If you see instead:

- `● OFFLINE` — Halogen is unreachable at the header address (check host/port, firewall).
  YAMD waits a few consecutive failures before reporting this.
- `● ENGINE DOWN` — `/health` answers but `halogen:engine_up` is 0. The dashboard keeps
  the last snapshot; check the server's logs.
- `● DEGRADED` — `/health` answered with `capability_probe: failed`; the server's engine
  probe did not answer and some fields are defaults.
- `(API key rejected (HTTP 401))` next to `● ONLINE` — the server is reachable but your
  `--api-key` is wrong or missing.

## A note on request rows

Halogen has no request-ring endpoint. YAMD watches `halogen:requests_total` and records
one row per poll window in which it moves; several completions inside one 1 s window
share a row (the `Reqs` column says how many). Token figures and the PP (prefill) / TG
(decode) rates are the deltas of that same window, so they match the request's own
`timings` when only one request completed. `Draft` is the window's speculative acceptance
(`draft_tokens_accepted / draft_tokens`). As the panel narrows, Draft drops first, then
PP; TG stays to the end.
