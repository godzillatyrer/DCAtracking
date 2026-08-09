# Stonk Watcher — CLOCKIN Launch Sniper

Watches the **Stonk Launcher** (Clutch Markets' token launchpad on Robinhood
Chain) and captures the contract address of the first launched token —
**CLOCKIN** — within seconds of it existing anywhere, backend or chain, then
blasts it to Telegram (and optionally Twilio SMS / macOS popups).

**T0 = Tuesday, August 11, 8:00 PM EST = Wednesday, August 12, 00:00 UTC.**

## How it hunts (three independent tracks)

| Track | What it watches | Cadence |
|---|---|---|
| 1. API sniper | `stonkbrokers.cash/api/launcher/tokens` + `/highlights`; new CAs get the detail endpoint called and are auto-tracked | 10s → 5s (last 24h) → **2s in the war room** (1h before → 2h after T0) → 10s |
| 2. On-chain | Team wallets `0xb668…7CDa` and `0xBe49…8215`: outgoing txs alert, contract creations are auto-armed as **candidate factories** and scanned topic-agnostically with chunked `eth_getLogs`; token addresses are extracted from indexed topics and confirmed via Blockscout | 20s → **5s in the war room** |
| 3. Frontend diff | The launcher page + its `/_next/static/chunks/*.js` bundles, diffed for new contract addresses and Vercel redeploys | hourly |

Any confirmed token whose name/symbol matches **CLOCKIN** (case-insensitive)
fires the loud `*** CLOCKIN ***` alert on every channel simultaneously —
first line is the bare CA for copy-paste speed, and on Telegram the CA is a
tap-to-copy code block.

## You get an error message when it's NOT working

Silence is a failure mode, so the watcher reports its own health to Telegram:

- **`WATCHER ERROR [component]`** — any component exception (rate-limited to
  one per identical error per 10 min so it can't spam you).
- **`API POLLER THROTTLED`** — more than 5 consecutive 403/429s from the
  backend; poller backs off to 30s and tells you that path is degraded.
- **`RPC DOWN` / Blockscout down / launcher page unreachable** — after 5
  consecutive failures, with a note about what the watcher is blind to.
- **`COMPONENT STALLED`** — the watchdog alerts if any loop stops completing
  cycles (hang, network freeze), and sends a recovery note when it resumes.
- **`WATCHER STARTED` / `WATCHER STOPPED` / `WATCHER CRASHED`** — process
  lifecycle, so an unexpected stop is visible from your phone.
- **`WATCHER ALIVE`** heartbeat every 6h (configurable via `HEARTBEAT_HOURS`)
  — if the heartbeat stops arriving, the process is dead.

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env        # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
python3 watcher.py --test-alert   # must print "telegram ok"
python3 watcher.py                # run it
```

Telegram setup: create a bot with [@BotFather](https://t.me/BotFather), send
your bot one message, then read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

### Setting T0

T0 defaults to `2026-08-12T00:00:00Z`. Override in `.env`:

```
T0_UTC=2026-08-12T00:00:00Z
```

To rehearse the cadence escalation, set T0 two minutes out and watch the
intervals tighten (10s→2s API, 20s→5s chain):

```bash
T0_UTC=$(python3 -c "from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))") python3 watcher.py
```

## Running 24/7 through the launch window (macOS)

Simplest (survives logout not required, just don't close the lid):

```bash
caffeinate -is python3 watcher.py 2>&1 | tee -a watcher.log
```

Auto-restart on crash (recommended for the launch window):

```bash
while true; do caffeinate -is python3 watcher.py 2>&1 | tee -a watcher.log; sleep 5; done
```

Or as a launchd service that starts at boot and restarts on exit: save as
`~/Library/LaunchAgents/cash.stonk.watcher.plist` (fix the paths), then
`launchctl load ~/Library/LaunchAgents/cash.stonk.watcher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>cash.stonk.watcher</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/PATH/TO/repo/watcher.py</string></array>
  <key>WorkingDirectory</key><string>/PATH/TO/repo</string>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/PATH/TO/repo/watcher.log</string>
  <key>StandardErrorPath</key><string>/PATH/TO/repo/watcher.log</string>
</dict></plist>
```

State lives in `data/state.json` — restarts never duplicate alerts, and
candidate factories / API dedupe survive. Delete the file only if you want a
full re-baseline.

## Deploying on Render

A `render.yaml` blueprint is included. **Render Dashboard → New → Blueprint →
pick this repo →** you'll be prompted for `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. That's the whole deploy.

It provisions a **Background Worker** (`$7/mo` starter) plus a **1 GB
persistent disk** (`$0.25/mo`) mounted at `/var/data`, with
`STATE_FILE=/var/data/state.json`. Per-service compute is billed on top of
your workspace plan rather than included in it, so expect ~$7.25/mo for this
service regardless of which workspace tier you're on.

Two Render facts drive that shape, and both matter here:

- **Background workers have no free instance type** — a long-running process
  with no HTTP endpoint is a paid service, minimum $7/mo.
- **Render's filesystem is ephemeral without a disk.** Every deploy and every
  restart would reset `state.json`, losing alert dedupe, wallet baselines and
  candidate factories. The watcher would re-alert on everything it already
  saw. The $0.25 disk is what makes restart survival real.

### The free alternative, and why it's risky for this job

Free instances only exist for *web services*, so the free path is: change
`type: worker` to `type: web` in `render.yaml`, drop the `disk:` block, and
set `plan: free`. The watcher already serves a JSON health endpoint on
`$PORT` when Render sets it (Render fails a web-service deploy if nothing
binds a port). Then:

- **Free web services spin down after 15 minutes with no inbound traffic**,
  and cold start takes ~1 minute. Outbound polling does *not* keep them
  awake. A sleeping sniper misses the launch entirely. You'd need an external
  uptime monitor (UptimeRobot etc.) hitting the health URL every 5 minutes to
  hold it open — workable, but one more thing to fail on launch night.
- **No disk on free**, so state resets on every restart.
- Free instances share 750 hours/month per workspace.

For a one-shot event two days out, $7.25 buys away both failure modes. My
actual recommendation: **run it on Render *and* on your Mac.** They keep
independent state, so you'd get each alert twice — mildly annoying, and far
better than one host dying at 7:58 PM.

### The health endpoint

Any deployment can expose it: `python3 watcher.py --port 10000`, or just set
`PORT`. `GET /` returns **200** while every component is cycling on schedule
and **503** once the watchdog considers one stalled — so pointing UptimeRobot
at it gives you a second, fully independent "it stopped working" alarm that
doesn't depend on the watcher's own Telegram path being healthy.

```json
{ "healthy": true, "hours_to_t0": 53.3, "war_room": false,
  "components": { "api_poller": { "last_cycle_seconds_ago": 6.0, "ok": true } },
  "telegram_configured": true, "tracked_count": 0 }
```

It deliberately reports **counts only, never contract addresses** — a Render
web service URL is public, and the CA is the entire edge.

### Render caveats worth knowing before launch night

- **Auto-deploy is on** (`autoDeployTrigger: commit`). A push to the deployed
  branch restarts the worker mid-window. Turn it off in the dashboard on
  Tuesday.
- macOS alerts obviously don't work there; Telegram is the channel.
- `python3 watcher.py --test-alert` runs from the Render **Shell** tab (paid
  instances), or just test locally — it's the same bot token either way.
- The blueprint sets no `branch:`, so Render follows the repo's default
  branch. Confirm the branch shown in the Render dashboard is the one your
  merged code is actually on before launch night.

## Tuesday evening checklist (one glance)

1. **Is the heartbeat printing?** The console logs a `HEARTBEAT` line every
   60s with per-component cycle ages and the state summary. No heartbeat =
   not running.
2. **Are all alert channels tested?** Run `python3 watcher.py --test-alert`
   and confirm the Telegram message arrived on your phone.
3. `python3 watcher.py --status` → T0 shows `-x.xh`, intervals show `5s/2s`
   as the window opens, both team wallets baselined.
4. Phone: Telegram notifications ON, not muted, battery saver off.
5. Laptop: on power, `caffeinate` loop (or launchd) running, Wi-Fi solid.
6. Do **not** restart the watcher at 7:59 PM — the first minutes of history
   baseline silently. Start it hours early (it's designed to run for days).

## Commands

| Command | Purpose |
|---|---|
| `python3 watcher.py` | run the watcher |
| `python3 watcher.py --test-alert` | test every alert channel end to end |
| `python3 watcher.py --status` | state summary + current poll cadence |
| `python3 watcher.py --once` | single cycle of each component (debugging) |
| `python3 watcher.py --port 10000` | also serve the JSON health endpoint |
| `python3 -m pytest tests/ -v` | offline acceptance tests |

### Live integration test (acceptance test 3)

Proves the address-extraction heuristic on real history: point the
candidate-factory logic at AMMFactoryV2 seeded from block 29,738,000 and it
must rediscover MANCER/KIDDIES/MTH from raw logs without knowing the
MarketCreated topic:

```bash
INTEGRATION=1 AMM_FACTORY_ADDRESS=0x<AMMFactoryV2> python3 -m pytest tests/test_integration_ammfactory.py -v
```

## Notes

- The Stonk Launcher factory contract is **not deployed yet** (as of Aug 9);
  its deployment by a team wallet is itself a signal — the watcher auto-arms
  any contract a team wallet deploys and listens to *everything* it emits,
  learning the token-creation topic after the first confirmed token (cheaper
  filtered queries for launch #2, #3, …).
- `RPC_URL` defaults to Blockscout's `/api/eth-rpc` proxy; a dedicated node
  endpoint is lower-latency if you have one.
- All timestamps/cadences use UTC internally; jitter ±20% is applied to every
  poll so the cadence doesn't look mechanical.
