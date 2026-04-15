# CLAUDE.md — DCAtracking routine guide

This file is context for any Claude Code session, especially scheduled
routines (the twice-daily health check). Keep it short; update it when
architecture changes materially.

## What this repo is

BSC + Solana launch-detection + exploit-watcher pipeline. See
`README.md` if added later; otherwise:

- **Launch detection**: 14 modules in `backend/detection/` that detect
  new-token launches worth alerting on. Unified tiered scorer (S/A/B/C)
  gates every alert (`backend/detection/launch_scorer.py`). The
  "no-noise" rule: single filter-tier signals never fire; S-tier
  requires ≥2 HIGH signals or 1 SELF_S signal.
- **Exploit watcher**: `backend/detection/exploit_watcher.py` fires
  short-opportunity alerts on TVL drops, abnormal SPL mints, and
  bridge drains. Uses a separate daily cap from launch alerts.
- **Chains**: BSC is fully wired; Solana requires `HELIUS_API_KEY`
  (modules log-and-skip without it).
- **Deploy target**: Render, single FastAPI service, in-process
  APScheduler. Database is Postgres. `Base.metadata.create_all()` +
  `_widen_legacy_columns()` in `backend/main.py` handle schema.

## Routine task: daily health check

Two relevant commands:

```bash
# Human-friendly: concise summary to stderr, JSON to stdout
python scripts/routine_health_check.py

# Auto-apply safe fixes, then re-audit
python scripts/routine_health_check.py --auto-heal

# Machine-readable only (for piping to jq)
python scripts/routine_health_check.py --json > /tmp/audit.json
```

Set `DCATRACKING_URL` to the live Render URL before running. Exit codes:

- `0` — healthy (no action)
- `1` — at least one **critical** issue (act)
- `2` — warnings only (investigate, don't block)
- `3` — service unreachable (likely Render outage — escalate to human)

## When running the routine

1. **Run the health check with `--auto-heal`.** This POSTs to any
   `auto_fix_url` the audit returned (currently: schema widening +
   orphan cleanup). These are idempotent — safe to call repeatedly.
2. **Re-run without `--auto-heal`** to see what's left.
3. For each remaining issue:
   - **CRITICAL** — investigate the `files_to_check` the audit listed.
     If the fix is small and obvious (single file, <20 lines), make it
     on a branch named `claude/routine-fix-<date>` and open a PR.
   - **WARNING / INFO** — log observation in the PR description but
     don't create a fix unless you have high confidence it's correct.
4. **Never push to main.** Always a PR, always with a description
   linking the audit issue id.

## Safe to auto-apply

These issues have `auto_fix_url` set — POST them without concern:

- `schema_drift:*` → `/api/diagnostics/fix/widen-columns`
- `orphan_candidates` → `/api/diagnostics/fix/cleanup-orphans`

## Never auto-apply

- Any code change. Always PR.
- Any `.env` / Render env var change. Surface to human.
- Any `git push --force`. Never.
- Dropping data. Ever.

## Architecture pointers (for fixes)

- `backend/scheduler.py` — all jobs registered + throttled here.
  `MAX_CONCURRENT_JOBS = 2` gates memory via `asyncio.Semaphore`.
- `backend/detection/launch_scorer.py` — the ONLY place alerts fire
  from the launch pipeline. Every detection module writes
  `launch_signals` and lets this module decide.
- `backend/main.py::_widen_legacy_columns()` — hand-rolled schema
  migration. When models change VARCHAR widths, add an entry here.
- `backend/main.py::_cleanup_legacy_data()` — one-shot data scrub at
  boot. Idempotent.
- `backend/api/diagnostics.py` — the audit logic itself. If a new job
  is added to `backend/scheduler.py`, add its expected interval to
  `_JOB_INTERVAL_MIN` so the audit can flag it when stale.

## Known good silence

- "No launch candidates surfaced in 24h" (INFO) can be legitimate on a
  quiet market. Only escalate if it persists >3 days.
- Solana modules flagged `never_ran` when `HELIUS_API_KEY` is unset —
  this is expected. Do not "fix" by registering more jobs.

## Common fix patterns

| Issue pattern | Likely root cause | Fix template |
|---|---|---|
| `schema_drift` | Model was widened, migration wasn't | Add an `ALTER TABLE … TYPE VARCHAR(n)` in `_widen_legacy_columns()` |
| `job_stale:wallet_analyzer` for 8+ hours | Render OOM'd the worker | Check Render logs for OOM. If real, tighten the heaviest batch (usually bytecode_match or exploit_watcher) |
| `job_error:exploit_watcher` with "StringDataRightTruncation" | New narrow column | Repeat: widen model + add ALTER + wrap insert with `_trunc` |
| `alert_spam` | Gating regression | Inspect `launch_scorer.recompute_candidate` recent changes |
| `volume_scanner_silent` for 24h+ | DEX Screener outage or rate limit | Check `/api/diagnostics/apis` |

## What's intentionally not in the audit

- Code quality / style
- Test coverage (no tests exist yet)
- Frontend correctness (only API-reachable things are audited)

The routine is only as useful as its scope — resist expanding it.
