"""
Module 7: Bytecode Fingerprint Match.

Compares the runtime bytecode of new contracts against a small library
of known-pump reference contracts (RAVE, SIREN, RIVER, ARIA, STO).
Pump operators tend to reuse the same ERC20 template, so high overlap
is a moderate-confidence signal that the new token is from the same
team.

We use a cheap SHA-256 of the normalized bytecode for exact matches,
and a naive chunk-overlap ratio for similarity. For Phase 1 this is
sufficient; more sophisticated similarity (n-gram, opcode-level) can
come later.
"""

import hashlib
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.bscscan_client import _rpc_call
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.contract_bytecode import ContractBytecode
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import TIER_MEDIUM

logger = logging.getLogger(__name__)


# Seed references — the 5 confirmed pumps from the seed data.
REFERENCE_CONTRACTS = {
    "RAVE":  "0x97693439ea2f0ecdeb9135881e49f354656a911c",
    "SIREN": "0x997a58129890bbda032231a52ed1ddc845fc18e1",
    "RIVER": "0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    "ARIA":  "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
    "STO":   "0xdaf1695c41327b61b9b9965ac6a5843a3198cf07",
}


def _hash_code(code_hex: str) -> str:
    # Strip metadata trailer (CBOR) — last 43 bytes of typical solc output
    code = code_hex[2:] if code_hex.startswith("0x") else code_hex
    if len(code) > 86:
        code = code[:-86]  # drop ~43 bytes of metadata hash
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _similarity(a: str, b: str) -> float:
    """
    Cheap similarity: 1.0 if lengths match and first/last 512 chars match
    (strong heuristic that it's the same solc template), 0.5 if just the
    lengths match, 0.0 otherwise.

    Replaced the previous set-of-chunks Jaccard because that was building
    ~100MB of Python sets per scoring pass and contributing to OOMs.
    """
    if not a or not b:
        return 0.0
    if len(a) == len(b):
        head_match = a[:512] == b[:512]
        tail_match = a[-512:] == b[-512:]
        if head_match and tail_match:
            return 1.0
        if head_match or tail_match:
            return 0.75
        return 0.5
    return 0.0


async def _get_code(addr: str) -> str | None:
    result = await _rpc_call("eth_getCode", [addr.lower(), "latest"])
    if not result or result in ("0x", "0x0"):
        return None
    return result


async def _ensure_references_cached(db: Session) -> dict[str, str]:
    """Fetch reference bytecode once + cache."""
    cache: dict[str, str] = {}
    for label, addr in REFERENCE_CONTRACTS.items():
        existing = db.query(ContractBytecode).filter_by(
            chain="bsc", contract_address=addr.lower()
        ).first()
        if existing and existing.reference_label:
            cache[label] = existing.code_hash or ""
            continue
        code = await _get_code(addr)
        if not code:
            continue
        h = _hash_code(code)
        row = ContractBytecode(
            chain="bsc",
            contract_address=addr.lower(),
            code_hash=h,
            code_length=len(code),
            reference_label=label,
            detected_at=datetime.utcnow(),
        )
        db.add(row)
        cache[label] = h
    db.commit()
    return cache


async def run_bytecode_match():
    """Scheduler entry. Compares freshly-detected candidates against refs."""
    logger.info("Starting bytecode_match run...")
    db = SessionLocal()
    matched = 0
    try:
        await _ensure_references_cached(db)

        # Only look at recent, yet-unhashed candidates — 10 per run to keep
        # peak memory bounded (each eth_getCode can return 100KB+ of hex).
        recent = db.query(LaunchCandidate).filter(
            LaunchCandidate.chain == "bsc",
            LaunchCandidate.status != "expired",
        ).order_by(LaunchCandidate.detected_at.desc()).limit(10).all()

        # Cache reference bytecode once per run so we don't re-fetch 5 × 10
        # contracts worth of large hex strings.
        ref_codes: dict[str, str] = {}
        for label, ref_addr in REFERENCE_CONTRACTS.items():
            code = await _get_code(ref_addr)
            if code:
                ref_codes[label] = code

        for cand in recent:
            addr = cand.contract_address
            if addr.startswith("pending:"):
                continue
            existing = db.query(ContractBytecode).filter_by(
                chain="bsc", contract_address=addr
            ).first()
            if existing and existing.best_match_similarity is not None:
                continue
            code = await _get_code(addr)
            if not code:
                continue
            code_hash = _hash_code(code)
            code_length = len(code)

            best_label = None
            best_score = 0.0
            for label, ref_code in ref_codes.items():
                score = (
                    1.0 if _hash_code(ref_code) == code_hash
                    else _similarity(code, ref_code)
                )
                if score > best_score:
                    best_score = score
                    best_label = label
            # Drop the large hex string for this candidate before moving on
            del code

            row = existing or ContractBytecode(
                chain="bsc",
                contract_address=addr,
                code_hash=code_hash,
                code_length=code_length,
                detected_at=datetime.utcnow(),
            )
            row.code_hash = code_hash
            row.code_length = code_length
            row.best_match_label = best_label
            row.best_match_similarity = Decimal(str(round(best_score, 3)))
            if existing is None:
                db.add(row)
            db.commit()

            if best_score >= 0.90:
                record_signal(
                    db,
                    chain="bsc",
                    contract_address=addr,
                    signal_type="bytecode_fingerprint",
                    signal_tier=TIER_MEDIUM,
                    confidence=70,
                    evidence={
                        "best_match": best_label,
                        "similarity": float(best_score),
                    },
                    description=(
                        f"Bytecode {best_score*100:.0f}% similar to {best_label}"
                    ),
                )
                matched += 1
    finally:
        db.close()
    logger.info(f"bytecode_match complete. {matched} matches emitted.")
    return {"matches": matched}
