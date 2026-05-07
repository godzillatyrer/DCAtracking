# CLAUDE.md — Solana Cabal Tracker

## What this repo is

A single-service Solana cabal tracker. Detects insider/smart-money
accumulation **before** the pump by inverting the usual approach:
instead of watching tokens for suspicious buyers, it watches CEX hot
wallets (Binance/OKX/Bybit/Bitget) for outflows, registers each
recipient as a tracked wallet, and alerts when ≥N of those CEX-funded
wallets cumulatively hold ≥X% of supply on the same coin.

Workflow:

1. **CEX outflow harvester** (every 10 min) — pulls recent SOL outflows
   from Binance/OKX/Bybit/Bitget hot wallets, registers each recipient
   as `solana_known_wallets.role = 'cex_funded'` with
   `funding_source = '<exchange>:<hot_label>'`.
2. **Wallet activity tracker** (every 5 min) — picks up these new
   wallets and logs their SPL buys/sells into
   `solana_wallet_activity` with USD values.
3. **Accumulation alerter** (every 20 min) — joins activity to the
   cex_funded population, aggregates per-mint net positions across
   the group, queries token supply via Helius, and fires a Telegram
   alert when:
     - ≥ `CEX_ACCUMULATION_MIN_WALLETS` (default 3) CEX-funded wallets
       are net-long on the same mint
     - combined holdings ≥ `CEX_ACCUMULATION_MIN_SUPPLY_PCT` (default
       1.5%) of supply
     - earliest buy ≥ `CEX_ACCUMULATION_MIN_HOLD_HOURS` (default 24h)
       ago — stealth, not same-day apes
     - group sell USD / buy USD ≤ `CEX_ACCUMULATION_MAX_SELL_RATIO`
       (default 0.30) — they're holding, not flipping
     - MC between `CEX_ACCUMULATION_MIN_MC_USD` ($500k) and
       `CEX_ACCUMULATION_MAX_MC_USD` ($20M)
4. **Manual cabal extractor** (`cabal_extractor.py`) — paste a runner
   mint, get its early buyers/holders auto-added as `cabal_trader`.
5. **Solana graph walk** (every 15 min) — follows outgoing SOL hops
   from tracked cabal wallets to find sibling wallets (depth 2).
6. **Jupiter DCA watcher** (every 3 min) — flags large openDca/V2
   orders ($150k+) on tokens under $50M mcap; checks CEX funding +
   wallet age inline.
7. **Convergence + sniper-solo dispatcher** (after every activity
   tracker cycle) — fires when N+ cabal wallets buy the same new mint.

EVM, BSC, Hyperliquid, and pump.fun-migration code paths have all been
removed. This is Solana-only.

## Architecture pointers

- **Entrypoint**: `backend/main.py` — FastAPI app, mounts three routers
  (`wallets`, `diagnostics`, `settings`), runs the scheduler in-process,
  boots a one-shot idempotent migration that widens
  `wallet_funding_edges.tx_hash` to `VARCHAR(128)`.
- **Scheduler**: `backend/scheduler.py` — 8 jobs under
  `MAX_CONCURRENT_JOBS=2` semaphore + 8-min hard timeout:
  `cex_outflow_harvester` (10 min), `accumulation_alerter` (20 min),
  `solana_graph_walk` (15 min), `wallet_activity_tracker` (5 min, also
  kicks the cabal-convergence dispatcher), `wallet_stats_aggregator`
  (10 min), `alert_outcome_tracker` (10 min), `behavioral_clusterer`
  (nightly 03:15 UTC), `dca_order_watcher` (3 min). Plus
  `telegram_command_poller` (10 sec, lightweight).
- **CEX harvester**: `backend/anomaly/cex_outflow_harvester.py` — for
  each active row in `cex_addresses` whose `exchange` is in
  `CEX_HARVEST_TARGET_EXCHANGES`, parses recent tx balance deltas to
  find SOL recipients, upserts them as `role='cex_funded'`. Skips
  amounts outside [`CEX_HARVEST_MIN_SOL`, `CEX_HARVEST_MAX_SOL`] to
  filter dust + OTC desks. Promotes existing `cabal_linked` wallets if
  they appear in a CEX outflow.
- **Accumulation alerter**: `backend/anomaly/accumulation_alerter.py` —
  aggregates `solana_wallet_activity` joined to
  `solana_known_wallets WHERE role='cex_funded'`, computes per-(mint,
  wallet) `bought - sold` tokens + USD across a 30-day lookback, calls
  Helius `getMintAuthorityAndSupply` for the supply denominator,
  applies all thresholds above, dedups per mint over
  `CEX_ACCUMULATION_DEDUP_HOURS` (default 7 days).
- **Cabal extractor**: `backend/detection/cabal_extractor.py` — manual
  flow when you paste a runner mint.
- **DCA Order Watcher**: `backend/anomaly/dca_order_watcher.py` —
  Jupiter DCA program (`DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M`).
  Decodes Anchor instruction data for `openDca` / `openDcaV2`. Filters
  for $150K+ orders on tokens under $50M mcap. Inline CEX-funding +
  fresh/dormant wallet check.
- **Wallet classifier**: `backend/anomaly/wallet_classifier.py` —
  Solana-only fresh/dormant classifier with 24h cache. Empty-timestamp
  wallets are now classified `is_fresh=False` (was True; that silently
  inflated freshie counts on every Helius blip).
- **Models**: `Alert`, `AlertOutcome`, `SolanaKnownWallet`,
  `SolanaWalletStats`, `SolanaWalletActivity`, `WalletFundingEdge`,
  `ChainAnomaly`, `WalletClassification`, `ScanLog`, `CexAddress`,
  `MajorCoin`, `AppSetting`. No new tables — the CEX-funded pipeline
  reuses `solana_known_wallets.funding_source` + `role='cex_funded'`.
- **Clients**: `helius`, `gmgn`, `birdeye`, `defillama`,
  `geckoterminal`, `dexscreener` — each exposes `configured`;
  extraction degrades rather than crashing when a key is missing.
- **Frontend**: React 18 SPA. Pages: `WalletTracker` (extract +
  tracked wallets), `LiveActivity`, `Leaderboard`, `Anomalies`,
  `AlertHistory`, `Settings`, `Health`.

## Alert types (current)

- `cex_accumulation` — the new core signal (this file's premise).
- `cabal_convergence` — N+ already-tracked cabal wallets buying the
  same fresh mint within `CONVERGENCE_WINDOW_MIN`.
- `cabal_exit` — N+ wallets from the same cluster dumping a previously
  alerted mint.
- `sniper_solo` — single high-confidence sniper bought a new mint.
- `dca_order` — large Jupiter DCA on a low-cap.

Removed (do not reintroduce without rationale): `freshie_swarm`,
`dormant_swarm`, `pump_migration`, `hl_whale_trade`,
`eth_freshie_swarm`, `bsc_freshie_swarm`, `eth_dormant_swarm`,
`bsc_dormant_swarm`. They were structurally noisy — pump.fun launches
are dominated by fresh sniper wallets, and "fresh wallets ape new
coin" is the noise floor on Solana, not signal.

## API surface

- `POST /api/wallets/solana/extract-from-runner` — manual cabal
  extraction. Body: `{mint, symbol, min_profit_usd, min_profit_mult,
  max_wallets, auto_add}`.
- `GET  /api/wallets/solana/known` — paginated tracked-wallet list.
- `GET  /api/wallets/solana/leaderboard` — top wallets by confidence.
- `GET  /api/wallets/solana/activity` — recent buys.
- `GET  /api/wallets/solana/convergence` — current convergence
  candidates.
- `GET  /api/diagnostics/apis` — per-provider reachability + latency.
- `GET  /api/diagnostics/health-audit` — structured issue list.
- `POST /api/diagnostics/fix/widen-columns` — idempotent migration.
- `GET  /api/anomalies` — ChainAnomaly feed.
- `GET  /api/health` — Render healthcheck.
- `GET  /api/health/full` — dashboard bundle (jobs + DB stats + APIs).

## Routine: daily health check

```bash
python scripts/routine_health_check.py
python scripts/routine_health_check.py --auto-heal
```

Set `DCATRACKING_URL` to the deployed URL. Exit codes: 0 healthy,
1 critical, 2 warning, 3 unreachable.

## Safe to auto-apply

- `schema_drift:*` → `POST /api/diagnostics/fix/widen-columns`
  (currently widens `wallet_funding_edges.tx_hash` to VARCHAR(128)).
  Idempotent.

## Never auto-apply

- Any code change — always PR on `claude/routine-fix-<date>`.
- `.env` / Render env var changes — escalate to human.
- `git push --force`. Ever.
- Dropping data. Ever.

## Known good silence

- `HELIUS_API_KEY` unset → all Solana watchers (cex_outflow_harvester,
  accumulation_alerter, solana_graph_walk, wallet_activity_tracker,
  dca_order_watcher) log and skip. Expected without a Helius key.
- New mint with no GeckoTerminal/DexScreener data → cabal_extractor
  falls back to Helius tx history. By design.
- Empty CEX outflow harvest cycle → normal. Hot wallets don't always
  send outflows in every 10-min window.

## Tuning the new detector

The accumulation alerter is configured entirely through the Settings
UI / `app_settings` table. Tighten or loosen via:

- **Too many alerts** → raise `CEX_ACCUMULATION_MIN_SUPPLY_PCT`
  (1.5 → 2.5), raise `CEX_ACCUMULATION_MIN_WALLETS` (3 → 4), or shrink
  `CEX_ACCUMULATION_MAX_MC_USD` to filter out larger caps.
- **Too few alerts** → lower `CEX_ACCUMULATION_MIN_HOLD_HOURS`
  (24 → 12), lower `CEX_ACCUMULATION_MIN_NET_USD_PER_WALLET`, or
  expand `CEX_HARVEST_TARGET_EXCHANGES`.
- **Catching exits instead of entries** → drop
  `CEX_ACCUMULATION_MAX_SELL_RATIO` (0.30 → 0.15).

## Common fix patterns

| Issue | Likely root cause | Fix template |
|---|---|---|
| `StringDataRightTruncation` on `wallet_funding_edges` | New column width needed | Widen in `backend/models/wallet_funding_graph.py` + add `ALTER TABLE` in `backend/main.py::_widen_solana_sig_columns` |
| `accumulation_alerter` returns 0 candidates for hours | Either CEX harvester hasn't found wallets, OR no mint has 3+ cex_funded wallets net-long | Check `cex_outflow_harvester` scan_log; verify `solana_known_wallets WHERE role='cex_funded'` has rows; query `solana_wallet_activity` for those wallets |
| `job_stale:cex_outflow_harvester` > 30 min | Render OOM or Helius rate-limit | Check Render logs; lower `CEX_HARVEST_SIGS_PER_HOT` |
| `job_error:*` with `StringDataRightTruncation` | Column drift | POST `/api/diagnostics/fix/widen-columns` |

## What's intentionally not audited

- Code style / tests (no tests exist).
- Frontend correctness (only API-reachable things are audited).
- EVM / BSC / Hyperliquid — explicitly removed; do not reintroduce
  without a strong rationale.

Resist expanding scope. The detector is only useful as far as it's
tight on the CEX-funded-accumulation premise.
