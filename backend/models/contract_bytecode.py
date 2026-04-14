"""
ContractBytecode — hash of each newly-deployed contract's runtime
bytecode, plus similarity links to known pump templates.

Used by Module 7 (Bytecode Fingerprint Match). Same pump team tends to
reuse the same ERC20 template, so a high bytecode overlap to a confirmed
pump is a moderate-confidence signal.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String

from backend.database import Base


class ContractBytecode(Base):
    __tablename__ = "contract_bytecodes"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chain = Column(String(10), default="bsc", nullable=False)
    contract_address = Column(String(64), nullable=False)

    code_hash = Column(String(66))  # sha256 hex of runtime code
    code_length = Column(Integer)

    # If this is a known-reference contract (e.g. RAVE, SIREN), label it
    reference_label = Column(String(64))

    # Best-match score against any reference contract (0.0 - 1.0)
    best_match_label = Column(String(64))
    best_match_similarity = Column(Numeric(4, 3))

    detected_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_bytecode_addr", "chain", "contract_address", unique=True),
        Index("idx_bytecode_hash", "code_hash"),
    )
