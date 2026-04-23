"""Behavioral entity merger (nightly).

The stats aggregator already assigns entity_id by funding-graph
connectivity. But cabals sometimes coordinate without sharing a
funding source (e.g. funded via exchange hot wallets). This job adds
a SECOND clustering pass based on behavior: pairs of wallets that
bought ≥ BEHAVIOR_MIN_SHARED_MINTS identical mints within
BEHAVIOR_TIME_WINDOW_SEC of each other get their entity_ids merged.

Runs nightly because the full pair-scan is O(N²) in the worst case.
Cheap at our scale (hundreds of tracked wallets) but not tiny.
"""

import logging
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from backend import settings_cache
from backend.database import SessionLocal
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

logger = logging.getLogger(__name__)


class _UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


async def run_behavioral_clusterer():
    min_shared = settings_cache.get("BEHAVIOR_MIN_SHARED_MINTS", 5)
    window_sec = settings_cache.get("BEHAVIOR_TIME_WINDOW_SEC", 120)

    db = SessionLocal()
    merged = 0
    try:
        # Load all buys (wallet, mint, ts). For scale: filter to last 30d.
        rows = (
            db.query(
                SolanaWalletActivity.wallet_address,
                SolanaWalletActivity.token_mint,
                SolanaWalletActivity.detected_at,
            )
            .filter(SolanaWalletActivity.activity_type == "spl_buy")
            .filter(SolanaWalletActivity.token_mint.isnot(None))
            .all()
        )
        if not rows:
            return {"merged_pairs": 0}

        # mint -> list of (wallet, ts)
        by_mint: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
        for addr, mint, ts in rows:
            if ts:
                by_mint[mint].append((addr, ts))

        # For each mint, find pairs of wallets buying within window_sec.
        # Record as "together" event for that (wallet_a, wallet_b).
        shared_counts: dict[tuple[str, str], int] = defaultdict(int)
        for mint, events in by_mint.items():
            events.sort(key=lambda e: e[1])
            # Two-pointer window
            n = len(events)
            for i in range(n):
                wi, ti = events[i]
                for j in range(i + 1, n):
                    wj, tj = events[j]
                    if (tj - ti).total_seconds() > window_sec:
                        break
                    if wi == wj:
                        continue
                    pair = tuple(sorted((wi, wj)))
                    shared_counts[pair] += 1

        if not shared_counts:
            return {"merged_pairs": 0}

        # Pairs with >= min_shared mints are candidates to merge
        uf = _UF()
        pairs_merged = 0
        for (a, b), count in shared_counts.items():
            if count >= min_shared:
                uf.union(a, b)
                pairs_merged += 1

        if pairs_merged == 0:
            return {"merged_pairs": 0}

        # Rewrite entity_id for each root component.
        # Keep stable ids by using min-wallet-address-hash as root id.
        roots: dict[str, set[str]] = defaultdict(set)
        for addr in {a for pair in shared_counts for a in pair}:
            roots[uf.find(addr)].add(addr)

        updated_wallets = 0
        for root, members in roots.items():
            if len(members) < 2:
                continue
            # Stable eid from the root address
            eid = hash(root) & 0x7FFFFFFFFFFFFFFF
            size = len(members)
            for addr in members:
                s = db.query(SolanaWalletStats).filter_by(wallet_address=addr).first()
                if s is None:
                    continue
                # Only overwrite if not already in a BIGGER cluster
                if s.entity_size is None or size > (s.entity_size or 0):
                    s.entity_id = eid
                    s.entity_size = size
                    updated_wallets += 1
        db.commit()
        merged = updated_wallets
        logger.info(
            f"behavioral_clusterer: {pairs_merged} pairs merged, "
            f"{updated_wallets} wallet rows updated"
        )
        return {"merged_pairs": pairs_merged, "updated_wallets": updated_wallets}
    finally:
        db.close()
