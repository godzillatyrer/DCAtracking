"""
Wallet Stats Aggregator — rolls up SolanaWalletActivity into
SolanaWalletStats, assigns entity_id via union-find on wallet_funding_edges.

Runs on a timer (10 min). Non-blocking: pure DB work + in-memory
computation. Idempotent — upserts one row per wallet.

Confidence score formula:
  base = log(1 + max(net_profit_usd, 0))
  win_bonus = win_rate * sqrt(min(mints_closed, 25))   # caps upside
  recency = 1.0 if active in last 7d, 0.5 if 7-30d, 0.2 otherwise
  score = (base * (1 + win_bonus)) * recency
"""

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats
from backend.models.wallet_funding_graph import WalletFundingEdge

logger = logging.getLogger(__name__)


# ─── Union-Find ────────────────────────────────────────────────────────

class _UF:
    """Classic path-compressed union-find over arbitrary string keys."""

    def __init__(self):
        self.parent: dict[str, str] = {}
        self.size: dict[str, int] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        self.size.setdefault(x, 1)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # compress
            x = self.parent[x]
        return x

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Union by size
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def _build_entities(db) -> tuple[dict[str, int], dict[int, int]]:
    """Return (wallet -> entity_id, entity_id -> size).

    An entity is a connected component in the funding graph intersected
    with our tracked-wallet universe. entity_id is just an integer
    derived from the root address hash so it's stable across runs.
    """
    uf = _UF()

    tracked_addrs = {
        w for (w,) in db.query(SolanaKnownWallet.wallet_address).all()
    }
    if not tracked_addrs:
        return {}, {}

    # Each tracked wallet starts as its own component
    for a in tracked_addrs:
        uf.find(a)

    # Union along funding edges — both endpoints must be in the
    # tracked set for us to care about the edge.
    edges = db.query(
        WalletFundingEdge.funder_address, WalletFundingEdge.recipient_address,
    ).filter(WalletFundingEdge.chain == "solana").all()

    for funder, recipient in edges:
        if funder in tracked_addrs and recipient in tracked_addrs:
            uf.union(funder, recipient)

    # Also: wallets that share a funding_source explicitly (set at
    # extract time or graph walk time) are probably the same actor
    # even if they never sent SOL to each other.
    from collections import defaultdict
    by_funder = defaultdict(list)
    for (addr, src) in db.query(
        SolanaKnownWallet.wallet_address, SolanaKnownWallet.funding_source,
    ).filter(SolanaKnownWallet.funding_source.isnot(None)).all():
        if src:
            by_funder[src].append(addr)
    for group in by_funder.values():
        if len(group) < 2:
            continue
        root = group[0]
        for other in group[1:]:
            if other in tracked_addrs:
                uf.union(root, other)

    wallet_to_entity: dict[str, int] = {}
    entity_size: dict[int, int] = {}
    for addr in tracked_addrs:
        root = uf.find(addr)
        # Stable integer id — 63-bit hash of the root addr keeps it
        # positive and BigInteger-safe.
        eid = hash(root) & 0x7FFFFFFFFFFFFFFF
        wallet_to_entity[addr] = eid
        entity_size[eid] = entity_size.get(eid, 0) + 1
    return wallet_to_entity, entity_size


# ─── Confidence score ─────────────────────────────────────────────────

def _compute_confidence(
    net_profit_usd: float, win_count: int, loss_count: int,
    mints_closed: int, last_activity_at: datetime | None,
) -> float:
    base = math.log1p(max(net_profit_usd, 0.0))
    closed = max(win_count + loss_count, 1)
    win_rate = win_count / closed
    win_bonus = win_rate * math.sqrt(min(mints_closed, 25))

    recency = 1.0
    if last_activity_at:
        age = datetime.utcnow() - last_activity_at
        if age > timedelta(days=30):
            recency = 0.2
        elif age > timedelta(days=7):
            recency = 0.5

    return round(base * (1.0 + win_bonus) * recency, 2)


# ─── Sniper classification ─────────────────────────────────────────────
#
# "Very low entries + insane profit each time" — the user's words.
# A wallet that routinely buys tiny amounts ($50-500) and exits 5-50x
# more than it put in. This is the profile that makes for the best
# SOLO alerts: even a single buy from one of these is a strong signal
# because their track record demonstrates they only enter before a
# real pump.

SNIPER_MAX_AVG_ENTRY_USD = 500.0   # "very low entries"
SNIPER_MIN_EXIT_MULTIPLIER = 3.0   # "insane profit" floor
SNIPER_MIN_WIN_RATE = 0.60         # consistency
SNIPER_MIN_CLOSED_POSITIONS = 4    # anti-lucky-twice sample size
SNIPER_MIN_NET_PROFIT_USD = 1000   # overall ROI sanity


def _compute_sniper(
    avg_buy_size_usd: float | None, avg_exit_multiplier: float | None,
    win_count: int, loss_count: int, mints_closed: int,
    net_profit_usd: float,
) -> bool:
    if avg_buy_size_usd is None or avg_buy_size_usd <= 0:
        return False
    if avg_exit_multiplier is None:
        return False
    closed = win_count + loss_count
    if closed < SNIPER_MIN_CLOSED_POSITIONS:
        return False
    win_rate = (win_count / closed) if closed else 0.0
    return (
        avg_buy_size_usd <= SNIPER_MAX_AVG_ENTRY_USD
        and avg_exit_multiplier >= SNIPER_MIN_EXIT_MULTIPLIER
        and win_rate >= SNIPER_MIN_WIN_RATE
        and mints_closed >= SNIPER_MIN_CLOSED_POSITIONS
        and net_profit_usd >= SNIPER_MIN_NET_PROFIT_USD
    )


# ─── Main aggregation ──────────────────────────────────────────────────

async def run_wallet_stats_aggregator():
    db = SessionLocal()
    try:
        entities, entity_sizes = _build_entities(db)

        # Per (wallet, mint) roll-up: buy side, sell side.
        per_pair = (
            db.query(
                SolanaWalletActivity.wallet_address.label("w"),
                SolanaWalletActivity.token_mint.label("m"),
                func.sum(
                    func.coalesce(
                        func.nullif(SolanaWalletActivity.value_usd, 0), 0,
                    ) * (
                        # Sign trick: buy = -value, sell = +value.
                        # Summed gives us mint-level P/L directly.
                        # Using CASE since SA doesn't inline well.
                        1
                    )
                ),
            )
            .group_by(SolanaWalletActivity.wallet_address,
                      SolanaWalletActivity.token_mint)
        )

        # Simpler: pull raw rows, aggregate in Python. Dataset is
        # bounded (~ tracked wallets * avg mints traded), which stays
        # small in practice.
        rows = db.query(
            SolanaWalletActivity.wallet_address,
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.activity_type,
            SolanaWalletActivity.value_usd,
            SolanaWalletActivity.detected_at,
        ).filter(
            SolanaWalletActivity.token_mint.isnot(None),
        ).all()

        from collections import defaultdict
        # wallet -> {mint -> {buy, sell}}
        by_wallet: dict[str, dict[str, dict]] = defaultdict(
            lambda: defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
        )
        last_seen: dict[str, datetime] = {}
        tx_counts: dict[str, int] = defaultdict(int)
        buy_counts: dict[str, int] = defaultdict(int)
        sell_counts: dict[str, int] = defaultdict(int)

        for addr, mint, kind, val, detected in rows:
            tx_counts[addr] += 1
            if kind == "spl_buy":
                buy_counts[addr] += 1
                by_wallet[addr][mint]["buy"] += float(val or 0)
            elif kind == "spl_sell":
                sell_counts[addr] += 1
                by_wallet[addr][mint]["sell"] += float(val or 0)
            if detected and (last_seen.get(addr) is None or detected > last_seen[addr]):
                last_seen[addr] = detected

        # Upsert stats row per wallet
        new_rows = 0
        sniper_count = 0
        for addr, mint_map in by_wallet.items():
            total_buy = sum(m["buy"] for m in mint_map.values())
            total_sell = sum(m["sell"] for m in mint_map.values())
            net = total_sell - total_buy

            best_mint = None
            best_profit = None
            worst_profit = None
            win_count = 0
            loss_count = 0
            mints_closed = 0
            exit_multipliers: list[float] = []
            for mint, v in mint_map.items():
                profit = v["sell"] - v["buy"]
                if v["sell"] > 0:
                    mints_closed += 1
                    if profit > 0:
                        win_count += 1
                    else:
                        loss_count += 1
                    if v["buy"] > 0:
                        # Exit multiplier = sell proceeds / cost basis.
                        # Cap outliers at 100x so a single degen trade
                        # doesn't dominate the average.
                        mult = min(v["sell"] / v["buy"], 100.0)
                        exit_multipliers.append(mult)
                if best_profit is None or profit > best_profit:
                    best_profit = profit
                    best_mint = mint
                if worst_profit is None or profit < worst_profit:
                    worst_profit = profit

            avg_buy_size = (
                total_buy / buy_counts[addr] if buy_counts[addr] else None
            )
            avg_exit_mult = (
                sum(exit_multipliers) / len(exit_multipliers)
                if exit_multipliers else None
            )
            sniper = _compute_sniper(
                avg_buy_size_usd=avg_buy_size,
                avg_exit_multiplier=avg_exit_mult,
                win_count=win_count,
                loss_count=loss_count,
                mints_closed=mints_closed,
                net_profit_usd=net,
            )
            if sniper:
                sniper_count += 1

            eid = entities.get(addr)
            esize = entity_sizes.get(eid, 1) if eid is not None else 1

            confidence = _compute_confidence(
                net_profit_usd=net,
                win_count=win_count,
                loss_count=loss_count,
                mints_closed=mints_closed,
                last_activity_at=last_seen.get(addr),
            )

            stats = db.query(SolanaWalletStats).filter_by(wallet_address=addr).first()
            if stats is None:
                stats = SolanaWalletStats(wallet_address=addr)
                db.add(stats)
                new_rows += 1
            stats.tx_count = tx_counts[addr]
            stats.buy_count = buy_counts[addr]
            stats.sell_count = sell_counts[addr]
            stats.mints_traded = len(mint_map)
            stats.mints_closed = mints_closed
            stats.total_buy_usd = round(total_buy, 2)
            stats.total_sell_usd = round(total_sell, 2)
            stats.net_profit_usd = round(net, 2)
            stats.best_mint = best_mint
            stats.best_mint_profit_usd = round(best_profit, 2) if best_profit is not None else None
            stats.worst_mint_profit_usd = round(worst_profit, 2) if worst_profit is not None else None
            stats.win_count = win_count
            stats.loss_count = loss_count
            stats.avg_buy_size_usd = round(avg_buy_size, 2) if avg_buy_size is not None else None
            stats.avg_exit_multiplier = round(avg_exit_mult, 2) if avg_exit_mult is not None else None
            stats.is_sniper = sniper
            stats.last_activity_at = last_seen.get(addr)
            stats.confidence_score = confidence
            stats.entity_id = eid
            stats.entity_size = esize

        db.commit()

        total_stats = db.query(func.count(SolanaWalletStats.wallet_address)).scalar() or 0
        logger.info(
            f"wallet_stats_aggregator: upserted {len(by_wallet)} wallets "
            f"({new_rows} new, {sniper_count} snipers). Total rows: {total_stats}."
        )
        return {
            "wallets_with_activity": len(by_wallet),
            "new_rows": new_rows,
            "total_stats_rows": total_stats,
            "entities": len(entity_sizes),
            "snipers": sniper_count,
        }
    finally:
        db.close()
