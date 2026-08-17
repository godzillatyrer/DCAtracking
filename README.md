# ansem-tier-watch

Telegram alert when a coin lands in the **Gold** or **Diamond** tier on ansem.io.

Zero dependencies. Node 18+. One file (`watch.mjs`).

---

## What it watches

The site's own front-end reads an undocumented public JSON endpoint:

```
GET https://ansem.io/api/coins?limit=200
```

```jsonc
{ "coins": [ {
    "slug": "hyper-bull",
    "name": "Hyper Bull",
    "ticker": "HBULL",
    "mint": "4aAr…xTu3",          // Solana mint address — the stable identity
    "tier": "free",                // "free" | "gold" | "diamond"
    "status": "on_curve",          // "on_curve" | "migrated"
    "marketCapUsd": 1480000,
    "volume24hUsd": 13240000,
    "change24hPct": 344.2,
    "txns24h": 83915,
    "teamPct": 5, "airdropPct": 10, "airdropTotal": 1e8, "curvePct": 100,
    "creatorWallet": "5hh7…CHvF",
    "priceUsd": 0.0014, "pairAddress": null, "imageUrl": null,
    "enhancedAt": null,
    "createdAt": "2026-08-17T16:44:59.724Z"
} ] }
```

Things worth knowing, all verified against the live endpoint:

| Fact | Consequence |
|---|---|
| `tier` is exactly `"free" \| "gold" \| "diamond"` | matched case-insensitively |
| `?tier=gold` is **silently ignored** — no server-side tier filter | we filter client-side |
| `?limit=` is honoured, **max 200** (201+ returns a validation error) | we request 200 |
| No `offset`/`page` param works, and there is no total count | the feed is a rolling window of roughly the last few hours |
| Responses come from **several replicas that disagree** — consecutive calls returned 77, 131, 141 coins within seconds | never diff consecutive responses; we keep a persistent seen-set instead |
| The coin page is `https://ansem.io/launch/coin/<mint>` | used for the alert link |

**As of writing, every coin in the feed is `tier: "free"`.** There are currently
zero Gold and zero Diamond listings, which is why those tabs render
"Nothing matches." on both Home and Z500. So expect silence until the first one
actually lands — that is the tool working, not failing. Use `DRY_RUN` plus the
test suite to convince yourself the alerting path is live.

### The rolling-window caveat

Because the feed only covers a recent window and there is no pagination, a coin
that gets upgraded to Gold *after* it has aged out of the window will never be
seen. At a 30s poll interval with the window measured in hours this is a wide
margin, but it is the one real gap. If it matters, drop `POLL_INTERVAL_MS` and
keep the process up continuously rather than running it from cron.

---

## Setup

### 1. Create the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Pick a name and a username. It hands you a token like `123456789:AAE…`
3. **Send your new bot any message** (e.g. `/start`). A bot cannot message you
   until you have opened the conversation.

### 2. Find your chat id

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
```

That number is your `TELEGRAM_CHAT_ID`. For a group, add the bot to the group,
post a message there, and use the negative id that appears.

### 3. Configure and run

```bash
cp .env.example .env      # then edit .env
npm run dry               # prints what it would send, no token required
npm start                 # live
```

---

## Configuration

All via environment variables (or a `.env` file if you run with `--env-file=.env`).

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required unless `DRY_RUN` |
| `TELEGRAM_CHAT_ID` | — | required unless `DRY_RUN` |
| `WATCH_TIERS` | `gold,diamond` | comma list of `free`/`gold`/`diamond` |
| `POLL_INTERVAL_MS` | `30000` | clamped to 5s–1h |
| `STATE_FILE` | `./state.json` | dedupe memory; use an absolute path under systemd |
| `DRY_RUN` | off | log instead of sending |
| `RUN_ONCE` | off | single pass then exit (for cron) |
| `ALERT_ON_FIRST_RUN` | off | alert on pre-existing listings too |
| `VERBOSE` | off | log fetch counts each tick |
| `ANSEM_LIMIT` | `200` | 1–200 |
| `FETCH_ATTEMPTS` | `4` | retries with exponential backoff |

---

## How deduping works

The alert key is **`mint:tier`**, persisted in `state.json`. That choice does two
things:

- a brand-new Gold/Diamond listing fires once and never again
- a coin **upgraded** from Free into Gold later still fires, and the message is
  labelled *tier upgrade* rather than *new listing*

State is written **before** the Telegram call. If Telegram fails, that one key is
rolled back so the next tick retries it — better than replaying every alert.

On a cold start the first pass records what it sees **without** alerting, so you
don't get spammed with history. Override with `ALERT_ON_FIRST_RUN=true`.

---

## Running it for real

**pm2** (simplest):

```bash
npm i -g pm2
pm2 start watch.mjs --name ansem-tier-watch --node-args="--env-file=.env"
pm2 save && pm2 startup
pm2 logs ansem-tier-watch
```

**systemd** (`/etc/systemd/system/ansem-tier-watch.service`):

```ini
[Unit]
Description=ansem.io Gold/Diamond tier watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ansem-tier-watch
EnvironmentFile=/opt/ansem-tier-watch/.env
Environment=STATE_FILE=/var/lib/ansem-tier-watch/state.json
ExecStart=/usr/bin/node /opt/ansem-tier-watch/watch.mjs
Restart=always
RestartSec=10
StateDirectory=ansem-tier-watch
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ansem-tier-watch
journalctl -u ansem-tier-watch -f
```

**Docker**:

```bash
docker run -d --name ansem-tier-watch --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=… -e TELEGRAM_CHAT_ID=… \
  -e STATE_FILE=/data/state.json -v ansem-state:/data \
  -v "$PWD/watch.mjs:/app/watch.mjs:ro" -w /app \
  node:22-alpine node watch.mjs
```

**Render** (a `render.yaml` blueprint is included):

Dashboard → New → Blueprint → pick this repo. You'll be prompted for
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. It provisions a Background
Worker (`$7/mo` starter) plus a 1 GB persistent disk (`$0.25/mo`) mounted at
`/var/data`, with `STATE_FILE=/var/data/state.json`.

The disk is **not** optional. Render's filesystem is ephemeral without one,
so every deploy would reset `state.json` — and since a cold start records
existing listings *silently*, a restart would quietly swallow any
Gold/Diamond coin that appeared while the worker was down. No error, no
alert, just a coin you never hear about.

Two Render facts drive that shape: background workers have **no free
instance type**, and free web services **cannot mount disks**.

Auto-deploy is on, so a push restarts the worker. Harmless here — the disk
keeps the seen-set — but turn it off if you want a frozen deployment.

**cron** (coarser; the process is cheap enough that a long-running one is better):

```cron
* * * * * cd /opt/ansem-tier-watch && RUN_ONCE=true STATE_FILE=/opt/ansem-tier-watch/state.json /usr/bin/node watch.mjs >> /var/log/ansem-watch.log 2>&1
```

---

## Inspecting the live feed

```bash
npm run inspect
```

Read-only: no state written, no Telegram. Prints the HTTP status and
content-type (so a Cloudflare challenge is obvious rather than showing up as a
JSON parse error), a breakdown of every `tier`/`status`/`enhancedAt`/
`pairAddress`/`curvePct` shape present, whether the feed is sorted newest-first,
and any coins that look like paid "Get Listed" entries rather than launchpad
launches.

Useful for two open questions: which field marks a paid listing, and whether the
200-item cap can hide a new one.

## Tests

```bash
npm test
```

19 assertions against a mock API covering: free-only feed stays quiet, gold
alerts once, dedupe on re-run, diamond detected, free→gold upgrade labelled
correctly, cold-start silence, Telegram-failure rollback, config validation, and
an unreachable API not crashing the loop.

---

## Notes / caution

- This is an **undocumented endpoint**. It can change shape or disappear without
  notice. If alerts stop, run `npm run dry -- ` with `VERBOSE=true` and check the
  logged coin count first.
- **The endpoint sits behind Cloudflare.** A request with node's default
  user-agent gets a `Just a moment...` bot challenge (HTTP 403, HTML body)
  instead of JSON. `watch.mjs` sends `accept: application/json` and a
  `user-agent` of `ansem-tier-watch/1.0`, which gets through — do not drop
  those headers. Occasional `[debug] retry 1 in 1000ms` lines are consistent
  with intermittent challenges; the retry absorbs them. Sustained challenges
  would exhaust all `FETCH_ATTEMPTS` and log `ERROR during check`, which is
  **stdout only, with no Telegram alert** — so it looks exactly like a quiet
  feed. Watch the verbose coin count to tell the two apart.
- Keep the poll interval sane. 30s is ~2,880 requests/day, which is unremarkable;
  1s would be abusive and may get you blocked.
- Never commit `.env` or `state.json`.
- A tier badge is a claim made by a team on a launchpad, not a safety guarantee.
  Treat an alert as "go look at this", not "this is good".
