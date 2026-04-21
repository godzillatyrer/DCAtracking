# CLAUDE.md — Solana Cabal Tracker

## What this repo is

A single-service Solana cabal tracker. Workflow:

1. A meme token pumps on Solana. You paste its mint into the dashboard.
2. `cabal_extractor.py` pulls every early buyer/seller/holder from
   GMGN (top_traders + top_holders) + Helius (getTokenLargestAccounts
   + 1500-tx history), merges them, and auto-adds profitable wallets
   to `solana_known_wallets` as `role='cabal_trader'`.
3. `solana_graph_walk.py` runs every 15 minutes, follows outgoing SOL
   from tracked cabal wallets to fresh recipients, and auto-adds those
   as `role='cabal_linked'` (depth 1) / `cabal_linked_2` (depth 2).
4. When multiple tracked wallets buy the same new mint, a Telegram
   alert fires (via `backend/alerts/telegram_bot.py::fire_cabal_alert`).

BSC support and the Phase-2 launch detection pipeline were removed.
Everything left is Solana.

## Architecture pointers

- **Entrypoint**: `backend/main.py` — FastAPI app, mounts two routers
  (`wallets`, `diagnostics`), runs the scheduler in-process, boots a
  one-shot idempotent migration that widens
  `wallet_funding_edges.tx_hash` to `VARCHAR(128)` (Solana sigs are
  87–88 chars).
- **Scheduler**: `backend/scheduler.py` — single job,
  `solana_graph_walk` at 15-min interval, under a
  `MAX_CONCURRENT_JOBS=2` semaphore + 8-min hard timeout.
- **Extractor**: `backend/detection/cabal_extractor.py`
  - Price resolution: GeckoTerminal → DexScreener → Birdeye
  - Per-owner SOL delta uses each wallet's own index in `accountKeys`,
    not only the fee-payer (fixes bot-routed swaps)
  - Cost basis is per-token; realized vs unrealized split proportionally
  - Filter is permissive: keeps anyone who `tokens_bought > 0`, is in
    ≥2 sources, or clears the profit/balance threshold; ranks by
    cross-confirmation then `max(realized, balance, total_profit)`
- **Graph walk**: `backend/detection/solana_graph_walk.py` — inserts
  into `wallet_funding_edges`; depth-limited to 2.
- **Models**: `Alert`, `ScanLog`, `SolanaKnownWallet`,
  `SolanaWalletActivity`, `WalletFundingEdge`.
- **Clients**: `helius`, `gmgn`, `birdeye`, `defillama`,
  `geckoterminal` — each exposes `configured`; extraction degrades
  rather than crashing when a key is missing.
- **Frontend**: React 18 SPA, single page (`WalletTracker.jsx`) —
  extract form + tracked-wallets table. `App.jsx` has no other routes.

## API surface

- `POST /api/wallets/solana/extract-from-runner` — body:
  `{mint, symbol, min_profit_usd, min_profit_mult, max_wallets, auto_add}`.
  Returns wallets + debug.
- `GET  /api/wallets/solana/known` — paginated tracked-wallet list.
- `GET  /api/diagnostics/apis` — per-provider reachability + latency
  (Helius, GMGN, Birdeye, DeFi Llama, GeckoTerminal, Telegram).
- `GET  /api/diagnostics/health-audit` — structured issue list for
  routines (scheduler freshness + schema drift).
- `POST /api/diagnostics/fix/widen-columns` — idempotent migration.
- `POST /api/diagnostics/fix/cleanup-orphans` — no-op, kept for compat.
- `GET  /api/health` — Render healthcheck.

## Routine: daily health check

```bash
# human summary to stderr + JSON to stdout
python scripts/routine_health_check.py

# auto-POST safe fixes then re-audit
python scripts/routine_health_check.py --auto-heal
```

Set `DCATRACKING_URL` to the deployed URL. Exit codes: 0 healthy,
1 critical, 2 warning, 3 unreachable.

## Safe to auto-apply

- `schema_drift:*` → `POST /api/diagnostics/fix/widen-columns`
  (currently widens `wallet_funding_edges.tx_hash` +
  `recipient_deploy_tx` to VARCHAR(128)). Idempotent.

## Never auto-apply

- Any code change — always PR on `claude/routine-fix-<date>`.
- `.env` / Render env var changes — escalate to human.
- `git push --force`. Ever.
- Dropping data. Ever.

## Known good silence

- `HELIUS_API_KEY` unset → `solana_graph_walk` logs and skips. Expected
  if the user hasn't seeded a Helius key; don't "fix" by wiring
  another RPC.
- GMGN returning `gmgn_top_traders_count: 0` / `gmgn_top_holders_count: 0`
  on brand-new pump.fun tokens — GMGN often hasn't indexed them yet.
  Extractor falls back to Helius tx history; that's the design.

## Common fix patterns

| Issue | Likely root cause | Fix template |
|---|---|---|
| `StringDataRightTruncation` on `wallet_funding_edges` | New column width needed | Widen in `backend/models/wallet_funding_graph.py` + add `ALTER TABLE` in `backend/main.py::_widen_solana_sig_columns` |
| `post_filter_count: 0` on a real runner | Price source failed + SOL attribution missed | Check `token_price_source` in the debug payload; verify DexScreener + Birdeye fallbacks resolved |
| `job_stale:solana_graph_walk` > 45 min | Render OOM or Helius rate-limit | Check Render logs; lower `MAX_WALLETS_PER_RUN` in `solana_graph_walk.py` |
| `job_error:solana_graph_walk` with `StringDataRightTruncation` | Column drift | POST `/api/diagnostics/fix/widen-columns` |

## What's intentionally not audited

- Code style / tests (no tests exist).
- Frontend correctness (only API-reachable things are audited).

Resist expanding scope. The routine is only useful as far as it's tight.
