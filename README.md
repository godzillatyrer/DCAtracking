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
