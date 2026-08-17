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

### The rolling window is ~50 minutes, and that matters

Measured against the live feed (`npm run inspect`): 200 coins spanning
`17:37:24Z` down to `16:47:32Z` — **a 50-minute window at roughly 4 new coins per
minute**, sorted strictly newest-first.

Three consequences, in order of how much they should worry you:

1. **Tier upgrades on older coins are invisible.** A coin promoted to Gold more
   than ~50 minutes after it was created has already aged out, and there is no
   pagination to go back for it. The `mint:tier` upgrade path still works, but
   only inside that window. This is a real gap with no fix available from the
   public API.
2. **New arrivals are safe.** Because the sort is newest-first, anything new
   enters at the top, so the 200-item cap cannot hide it. A paid "Get Listed"
   coin creates a fresh record, so it arrives at the top too.
3. **A missed poll costs nothing.** A coin sits in the window for ~100 ticks at
   `POLL_INTERVAL_MS=30000`, so a failed fetch — or twenty — loses nothing. Only
   an outage longer than ~50 minutes drops a coin permanently, which is what the
   staleness alarm below is calibrated against.

### Cloudflare

The endpoint is behind Cloudflare and challenges a *fraction* of requests. A
measured probe returned `1/6` challenged. With `FETCH_ATTEMPTS=4` that works out
to roughly 0.08% of cycles failing outright — and per point 3 above, a failed
cycle is harmless. The occasional `[debug] retry 1 in 1000ms` in the logs is this,
and it is nothing to chase.

What matters is the tail: if that fraction ever goes to 1, the loop keeps running
and Telegram stays silent, which is indistinguishable from a quiet feed. Hence the
staleness alarm.

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
| `WATCH_LISTINGS` | `true` | alert on paid "Get Listed" coins, any tier |
| `SCHEMA_DRIFT_ALERT` | `true` | alert on an unseen `tier`/`status` value |
| `STALE_ALERT_MS` | `600000` | alert if no fetch has succeeded in this long |
| `HEARTBEAT_MS` | `86400000` | periodic "still alive" message; `0` disables |
| `WATCH_BURNS` | `true` | alert on $ANSEM burns from the burners leaderboard |
| `BURN_MIN_USD` | `1000` | ignore burns below this; threshold crossings ignore it |
| `WATCH_STATUS` | `true` | alert when listings reopen or the site gate changes |
| `STATUS_POLL_EVERY` | `10` | poll those every Nth tick (they change rarely) |

---

## Burns

Burning $ANSEM is how a team pays the $25,000 listing fee **and** how it climbs
the tier ladder — the same act does both. So a burn is the earliest warning that
a listing is coming, ahead of anything appearing in `/api/coins`.

Read from `/api/leaderboard/burners`, which returns cumulative totals per wallet.
A burn is therefore an **increase**, not a new row — a wallet that burns twice
shows up once with a larger number.

### The site's own wording is wrong, and it matters

Both `/burn` and the token docs say burns "go to the community burn wallet". On
chain they are plain `BurnChecked` instructions on spl-token-2022 with **no
destination account** — total supply simply drops.

A monitor built on the site's description — watching for transfers into a burn
wallet — would have sat silent forever and never errored. That is the exact
failure mode worth avoiding: not a crash, but a plausible-looking watcher that
can never fire. Reading the leaderboard sidesteps the question entirely and gives
us the wallet, which a supply diff would not.

### Tier thresholds are dynamic — never hardcode them

Two contradictory sets are live simultaneously:

| Source | Gold | Diamond |
|---|---|---|
| the `/burn` page | 25,000 | 100,000 |
| `/api/config` | 92,627 | 370,508 |

The API recomputes from the live $ANSEM price, so its numbers move daily. We read
`/api/config` every `STATUS_POLL_EVERY` ticks and use that. Note the internal key
for Gold is **`bronze`**.

A burn is reported when it exceeds `BURN_MIN_USD`, **or** when it crosses a tier
threshold — a crossing is the whole point, so it overrides the dust filter.

If `/api/config` is unreachable the price is unknown, and an unknown price must
not read as `$0` — otherwise every burn is dust-filtered and the alert silently
disappears. Burns are reported without a USD figure in that case.

## Listing status

`/api/listing/config` returns `{enabled, usdAmount, burnAvailable, airdropAvailable}`.
`enabled` flipping to `true` is the moment paid listings reopen, and it fires a
🟩 **LISTINGS ARE OPEN** message.

`/api/gate` carries a site-wide countdown (`{mode, launchAt, autoOpen}`). Any
change to it is reported.

Both are polled every `STATUS_POLL_EVERY` ticks rather than every tick — they
change rarely, and request volume against a Cloudflare-fronted origin is not free.

## What is *not* watched, and why

**On-chain burns directly.** The leaderboard is the site's own accounting and is
what actually gates listings and tiers, so it is the more meaningful source. A
`getTokenSupply` diff would catch burns the leaderboard omits, but cannot say who
burned. Worth adding only if the leaderboard proves incomplete — it already looks
partial (25 wallets summing to ~175,738 against ~268,684 total burned).

**The airdrop payment route.** A team can pay by airdropping their own token to
$ANSEM holders instead of burning. Each coin gets its own merkle-distributor PDA
and vault, so there is no single address to watch, and whether the paid-listing
route reuses that machinery is unverified — no listing has ever gone through.
Such a listing would still be caught when the coin appears in `/api/coins`; only
the early warning is missing.

---

## Paid listings ("Get Listed")

Separately from the launchpad, a team can list an **existing** Solana token by
burning $25,000 of $ANSEM (or airdropping that much of their own token to $ANSEM
holders). The site then reads name, price and market cap from DexScreener.

That gives a clean structural tell: **a listed coin never has a bonding curve.**
Launchpad coins carry a `curvePct`; a listed one cannot. So the filter is
`curvePct == null && status !== "on_curve"`, and the alert is labelled
`💰 PAID LISTING` and fires regardless of tier.

Against the live feed this currently matches **nothing** — all 200 coins were
on-curve, and the Get Listed panel reads "Listings paused". That is the intended
resting state: silent until listings reopen, rather than guessing and firing on
ordinary launchpad coins.

Two things this deliberately does *not* assume:

- **`enhancedAt` is not the paid-listing marker.** 9 of 200 coins had it set, all
  of them free-tier and all on a bonding curve — so it is some cheaper profile
  upgrade, not the $25k listing. Matching on it would have produced false alerts.
- **Burning does not visibly imply a tier.** The panel says the burn "counts
  toward your tier", but every coin in the feed is `free`, so the thresholds are
  unknown. A paid listing is therefore treated as its own event, not as a
  Gold/Diamond signal.

Because the real shape of a listed coin has never been observed, the schema-drift
alert is the backstop: if a `status` or `tier` value appears that has never been
seen before, you get a message saying so. If the filter above turns out to be
wrong when listings reopen, that notice is what tells you.

---

## When it breaks

Silence from this tool is ambiguous — it means either "no Gold coins" or "I have
been unable to read the API for two days". Three messages resolve that:

| Message | When |
|---|---|
| 🔴 **is blind** | no successful fetch in `STALE_ALERT_MS` (default 10 min) |
| 🟢 **recovered** | the next successful fetch after a blind alert |
| 💚 **alive** | every `HEARTBEAT_MS` (default 24h), with counts |

The 10-minute default is deliberate: the feed holds ~50 minutes of history, so an
outage is only *destructive* past that point. Ten minutes leaves a wide margin
while still reaching you in the same hour.

`lastOkAt` lives in the state file rather than in memory, so a crash-loop cannot
reset the clock and suppress the alarm — which is precisely the scenario where
you most need it.

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
