"""Monitor Jupiter DCA program on Solana for new order creations.

Polls Helius RPC for recent transactions on the Jupiter DCA program,
parses openDca/openDcaV2 instructions, and extracts order details.
"""

import asyncio
import hashlib
import logging
import struct
import time

import aiohttp

import config
import database

logger = logging.getLogger(__name__)

# Anchor instruction discriminators
OPEN_DCA_DISC = hashlib.sha256(b"global:open_dca").digest()[:8]
OPEN_DCA_V2_DISC = hashlib.sha256(b"global:open_dca_v2").digest()[:8]

# Account indices in the openDca instruction
# Layout: [dca, user, payer, inputMint, outputMint, ...]
IDX_DCA_ACCOUNT = 0
IDX_USER = 1
IDX_INPUT_MINT = 3
IDX_OUTPUT_MINT = 4

# Base58 alphabet (Bitcoin/Solana)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    """Decode a base58 string to bytes."""
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHABET.index(c)
    result = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big") if n else b"\x00"
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


def _extract_account_keys(tx: dict) -> list[str]:
    """Extract the full list of account keys from a transaction,
    handling both legacy and versioned (v0) formats."""
    message = tx["transaction"]["message"]
    meta = tx.get("meta", {})

    raw_keys = message.get("accountKeys", [])
    account_keys = []
    for k in raw_keys:
        if isinstance(k, str):
            account_keys.append(k)
        elif isinstance(k, dict):
            account_keys.append(k.get("pubkey", ""))
        else:
            account_keys.append(str(k))

    # Add loaded addresses from address lookup tables (v0 transactions)
    loaded = meta.get("loadedAddresses", {})
    account_keys.extend(loaded.get("writable", []))
    account_keys.extend(loaded.get("readonly", []))

    return account_keys


async def fetch_recent_signatures(session: aiohttp.ClientSession,
                                  limit: int = 100,
                                  until: str | None = None) -> list[dict]:
    """Get recent transaction signatures for the Jupiter DCA program."""
    params: list = [
        config.JUPITER_DCA_PROGRAM,
        {"limit": limit, "commitment": "confirmed"},
    ]
    if until:
        params[1]["until"] = until

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": params,
    }

    async with session.post(config.HELIUS_RPC_URL, json=payload) as resp:
        data = await resp.json()
        if "error" in data:
            logger.error("RPC error fetching signatures: %s", data["error"])
            return []
        return data.get("result", [])


async def fetch_transaction(session: aiohttp.ClientSession,
                            signature: str) -> dict | None:
    """Fetch a full transaction by signature."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "json",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed",
            },
        ],
    }

    async with session.post(config.HELIUS_RPC_URL, json=payload) as resp:
        data = await resp.json()
        if "error" in data:
            logger.debug("RPC error for tx %s: %s", signature[:16], data["error"])
            return None
        return data.get("result")


def _try_parse_instruction(ix: dict, all_keys: list[str],
                           dca_program_idx: int, tx: dict) -> dict | None:
    """Try to parse a single instruction as a DCA order creation."""
    program_id_idx = ix.get("programIdIndex")
    if program_id_idx != dca_program_idx:
        return None

    data_b58 = ix.get("data", "")
    if not data_b58:
        return None

    try:
        data_bytes = _base58_decode(data_b58)
    except (ValueError, IndexError):
        return None

    if len(data_bytes) < 40:  # 8 disc + 8 appIdx + 8 inAmt + 8 perCycle + 8 freq
        return None

    # Check discriminator
    disc = data_bytes[:8]
    if disc != OPEN_DCA_DISC and disc != OPEN_DCA_V2_DISC:
        return None

    # Parse instruction data
    try:
        app_idx = struct.unpack_from("<Q", data_bytes, 8)[0]
        in_amount = struct.unpack_from("<Q", data_bytes, 16)[0]
        in_amount_per_cycle = struct.unpack_from("<Q", data_bytes, 24)[0]
        cycle_frequency = struct.unpack_from("<q", data_bytes, 32)[0]
    except struct.error:
        logger.debug("Failed to unpack DCA instruction data")
        return None

    # Extract account keys from instruction
    accounts = ix.get("accounts", [])
    if len(accounts) <= IDX_OUTPUT_MINT:
        return None

    try:
        dca_account = all_keys[accounts[IDX_DCA_ACCOUNT]]
        user_wallet = all_keys[accounts[IDX_USER]]
        input_mint = all_keys[accounts[IDX_INPUT_MINT]]
        output_mint = all_keys[accounts[IDX_OUTPUT_MINT]]
    except IndexError:
        return None

    total_cycles = in_amount // in_amount_per_cycle if in_amount_per_cycle > 0 else 0
    block_time = tx.get("blockTime", int(time.time()))
    signatures = tx.get("transaction", {}).get("signatures", [])

    return {
        "tx_signature": signatures[0] if signatures else "",
        "dca_account": dca_account,
        "user_wallet": user_wallet,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "in_amount_raw": in_amount,
        "in_amount_per_cycle_raw": in_amount_per_cycle,
        "cycle_frequency_seconds": cycle_frequency,
        "total_cycles": total_cycles,
        "timestamp": block_time,
    }


def parse_dca_instruction(tx: dict) -> dict | None:
    """Parse a transaction to extract DCA order creation data.
    Returns order dict if this tx creates a DCA order, None otherwise."""
    if not tx or not tx.get("transaction"):
        return None

    all_keys = _extract_account_keys(tx)

    # Find DCA program index
    dca_program_idx = None
    for i, key in enumerate(all_keys):
        if key == config.JUPITER_DCA_PROGRAM:
            dca_program_idx = i
            break

    if dca_program_idx is None:
        return None

    message = tx["transaction"]["message"]
    meta = tx.get("meta", {})

    # Check outer instructions
    for ix in message.get("instructions", []):
        result = _try_parse_instruction(ix, all_keys, dca_program_idx, tx)
        if result:
            return result

    # Check inner instructions (CPI calls)
    for inner in meta.get("innerInstructions", []):
        for ix in inner.get("instructions", []):
            result = _try_parse_instruction(ix, all_keys, dca_program_idx, tx)
            if result:
                return result

    return None


async def scan_new_dca_orders(session: aiohttp.ClientSession) -> list[dict]:
    """Scan for new DCA order creations on Jupiter.
    Returns list of new DCA orders found since last scan."""

    last_sig = database.get_state("last_dca_signature")

    signatures = await fetch_recent_signatures(session, limit=100, until=last_sig)

    if not signatures:
        logger.debug("No new DCA signatures found")
        return []

    logger.info("Found %d new signatures to process", len(signatures))

    new_orders = []
    semaphore = asyncio.Semaphore(5)

    async def process_sig(sig_info: dict) -> dict | None:
        sig = sig_info["signature"]

        # Skip failed transactions
        if sig_info.get("err") is not None:
            return None

        async with semaphore:
            tx = await fetch_transaction(session, sig)
            if not tx:
                return None
            return parse_dca_instruction(tx)

    tasks = [process_sig(sig) for sig in signatures]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.error("Error processing transaction: %s", result)
            continue
        if result is not None:
            new_orders.append(result)

    # Update last processed signature (first in list = newest)
    if signatures:
        database.set_state("last_dca_signature", signatures[0]["signature"])

    logger.info(
        "Processed %d transactions, found %d new DCA orders",
        len(signatures), len(new_orders),
    )
    return new_orders
