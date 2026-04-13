"""
BSC blockchain client — uses MegaNode/NodeReal JSON-RPC API.

Provides a translation layer so all existing scanners continue to call
bscscan_request(params) with Etherscan-style parameters, and this module
translates them into MegaNode JSON-RPC calls behind the scenes.

Supported Etherscan actions translated:
  - contract/getcontractcreation  → first mint event (Transfer from 0x0)
  - token/tokenholderlist         → nr_getTokenHolders20
  - account/tokentx              → eth_getLogs (Transfer events)
  - account/txlist               → eth_getLogs (all events) + nonce check
  - stats/tokensupply            → eth_call totalSupply()
  - contract/getsourcecode       → eth_getCode (verified status only)
  - token/tokeninfo              → eth_call name()/symbol()/decimals()
"""

import asyncio
import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

RATE_LIMIT_DELAY = 0.20  # 200ms between calls (5/sec for free tier)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 64

_call_id = 0


def _rpc_url() -> str:
    return f"{settings.MEGANODE_BASE_URL}/{settings.MEGANODE_API_KEY}"


async def _rpc_call(method: str, params: list) -> dict | None:
    """Make a single JSON-RPC call to MegaNode."""
    global _call_id
    _call_id += 1
    url = _rpc_url()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json={
                "jsonrpc": "2.0",
                "id": _call_id,
                "method": method,
                "params": params,
            })
            await asyncio.sleep(RATE_LIMIT_DELAY)
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    logger.warning(f"RPC error ({method}): {data['error']}")
                    return None
                return data.get("result")
            else:
                logger.error(f"RPC HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"RPC call error ({method}): {e}")
    return None


def _pad_address(addr: str) -> str:
    """Pad an address to 32 bytes for use as a log topic."""
    return "0x" + addr.lower().replace("0x", "").zfill(64)


def _unpad_address(topic: str) -> str:
    """Extract an address from a 32-byte topic."""
    return "0x" + topic[-40:]


def _hex_to_int(val: str) -> int:
    """Convert hex string to int."""
    if not val or val == "0x":
        return 0
    return int(val, 16)


def _etherscan_response(result) -> dict:
    """Wrap a result in Etherscan-style response format."""
    return {"status": "1", "message": "OK", "result": result}


# === Translation functions ===


async def _get_contract_creation(contract_address: str) -> dict | None:
    """Find contract deployer via first mint event (Transfer from 0x0)."""
    logs = await _rpc_call("eth_getLogs", [{
        "address": contract_address.lower(),
        "topics": [TRANSFER_TOPIC, ZERO_ADDRESS],
        "fromBlock": "0x0",
        "toBlock": "latest",
    }])
    if logs and len(logs) > 0:
        # The first mint recipient is often the deployer
        first_mint = logs[0]
        deployer = _unpad_address(first_mint["topics"][2])
        return _etherscan_response([{
            "contractAddress": contract_address.lower(),
            "contractCreator": deployer,
            "txHash": first_mint.get("transactionHash", ""),
        }])
    return None


async def _get_token_holders(contract_address: str, page: int, offset: int) -> dict | None:
    """
    Get token holders. Tries nr_getTokenHolders20 first, falls back to
    reconstructing holders from Transfer event logs if enhanced API unavailable.
    """
    # Try enhanced API first
    page_size = str(offset) if offset else "10"
    page_key = ""
    holders_all = []

    for _ in range(page):
        result = await _rpc_call("nr_getTokenHolders20", [
            contract_address.lower(), page_key, page_size
        ])
        if not result or not result.get("holders"):
            break
        holders_all = result["holders"]
        page_key = result.get("pageKey", "")
        if not page_key:
            break

    # Transform to Etherscan format
    transformed = []
    for h in holders_all:
        balance = _hex_to_int(h.get("tokenBalance", "0x0"))
        transformed.append({
            "TokenHolderAddress": h.get("holderAddress", "").lower(),
            "TokenHolderQuantity": str(balance),
        })

    if transformed:
        return _etherscan_response(transformed)

    # FALLBACK: Reconstruct holders from Transfer event logs
    logger.info(f"nr_getTokenHolders20 unavailable, reconstructing from transfers for {contract_address[:10]}...")
    return await _reconstruct_holders_from_logs(contract_address, page, offset)


async def _reconstruct_holders_from_logs(
    contract_address: str, page: int, offset: int
) -> dict | None:
    """
    Reconstruct top holders by replaying Transfer events from eth_getLogs.
    Computes net balances per wallet from all transfers.
    """
    from collections import defaultdict
    from decimal import Decimal

    # Known addresses to exclude (routers, burn addresses, exchanges)
    EXCLUDED = {
        "0x" + "0" * 40,
        "0x" + "0" * 39 + "dead",
        "0x000000000000000000000000000000000000dead",
        "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap v2
        "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # PancakeSwap v3
    }

    balances: dict[str, int] = defaultdict(int)

    # Fetch Transfer logs in chunks (eth_getLogs may limit response size)
    # Get latest block to create ranges
    latest_block = await _rpc_call("eth_blockNumber", [])
    if not latest_block:
        return _etherscan_response([])

    latest = _hex_to_int(latest_block)

    # Scan in chunks of 50000 blocks from the end (most recent activity matters most)
    # For tokens < 50k blocks old, this gets everything
    chunk_size = 50000
    start_block = max(0, latest - chunk_size * 5)  # Last ~250k blocks

    for from_block in range(start_block, latest, chunk_size):
        to_block = min(from_block + chunk_size - 1, latest)
        logs = await _rpc_call("eth_getLogs", [{
            "address": contract_address.lower(),
            "topics": [TRANSFER_TOPIC],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }])

        if not logs:
            continue

        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr = _unpad_address(topics[1])
            to_addr = _unpad_address(topics[2])
            amount = _hex_to_int(log.get("data", "0x0"))

            balances[from_addr] -= amount
            balances[to_addr] += amount

    # Sort by balance, exclude zero/negative and infrastructure
    sorted_holders = sorted(
        [(addr, bal) for addr, bal in balances.items()
         if bal > 0 and addr not in EXCLUDED],
        key=lambda x: x[1],
        reverse=True,
    )

    # Paginate
    start = (page - 1) * offset
    end = start + offset
    page_holders = sorted_holders[start:end]

    transformed = [
        {"TokenHolderAddress": addr, "TokenHolderQuantity": str(bal)}
        for addr, bal in page_holders
    ]

    logger.info(f"Reconstructed {len(sorted_holders)} holders from logs, returning {len(transformed)}")
    return _etherscan_response(transformed)


async def _get_token_transfers_by_contract(
    contract_address: str, page: int, offset: int, sort: str
) -> dict | None:
    """Get token transfers for a contract via eth_getLogs."""
    logs = await _rpc_call("eth_getLogs", [{
        "address": contract_address.lower(),
        "topics": [TRANSFER_TOPIC],
        "fromBlock": "0x0",
        "toBlock": "latest",
    }])
    if logs is None:
        return None

    # Transform logs to Etherscan tokentx format
    transfers = []
    for log in logs:
        if len(log.get("topics", [])) < 3:
            continue
        transfers.append({
            "from": _unpad_address(log["topics"][1]),
            "to": _unpad_address(log["topics"][2]),
            "value": str(_hex_to_int(log.get("data", "0x0"))),
            "contractAddress": log.get("address", "").lower(),
            "hash": log.get("transactionHash", ""),
            "blockNumber": str(_hex_to_int(log.get("blockNumber", "0x0"))),
            "tokenDecimal": "18",
            "tokenSymbol": "",
            "tokenName": "",
        })

    if sort == "desc":
        transfers.reverse()

    # Apply pagination
    start = (page - 1) * offset
    end = start + offset
    return _etherscan_response(transfers[start:end])


async def _get_token_transfers_by_wallet(
    wallet_address: str, contract_address: str | None,
    page: int, offset: int, sort: str
) -> dict | None:
    """Get token transfers for a wallet address."""
    padded_wallet = _pad_address(wallet_address)

    # Get transfers FROM wallet
    from_filter = {
        "topics": [TRANSFER_TOPIC, padded_wallet],
        "fromBlock": "0x0",
        "toBlock": "latest",
    }
    if contract_address:
        from_filter["address"] = contract_address.lower()

    # Get transfers TO wallet
    to_filter = {
        "topics": [TRANSFER_TOPIC, None, padded_wallet],
        "fromBlock": "0x0",
        "toBlock": "latest",
    }
    if contract_address:
        to_filter["address"] = contract_address.lower()

    from_logs = await _rpc_call("eth_getLogs", [from_filter]) or []
    to_logs = await _rpc_call("eth_getLogs", [to_filter]) or []

    all_logs = from_logs + to_logs

    # Deduplicate by tx hash + log index
    seen = set()
    unique_logs = []
    for log in all_logs:
        key = (log.get("transactionHash", ""), log.get("logIndex", ""))
        if key not in seen:
            seen.add(key)
            unique_logs.append(log)

    # Transform
    transfers = []
    for log in unique_logs:
        if len(log.get("topics", [])) < 3:
            continue
        transfers.append({
            "from": _unpad_address(log["topics"][1]),
            "to": _unpad_address(log["topics"][2]),
            "value": str(_hex_to_int(log.get("data", "0x0"))),
            "contractAddress": log.get("address", "").lower(),
            "hash": log.get("transactionHash", ""),
            "blockNumber": str(_hex_to_int(log.get("blockNumber", "0x0"))),
            "tokenDecimal": "18",
            "tokenSymbol": "",
            "tokenName": "",
        })

    # Sort by block number
    transfers.sort(
        key=lambda x: int(x["blockNumber"]),
        reverse=(sort == "desc"),
    )

    # Pagination
    start = (page - 1) * offset
    end = start + offset
    return _etherscan_response(transfers[start:end])


async def _get_txlist(
    wallet_address: str, page: int, offset: int, sort: str
) -> dict | None:
    """
    Get normal transactions for a wallet.
    MegaNode doesn't have a direct equivalent of Etherscan's txlist.
    We approximate by looking at token transfer events involving this wallet
    to identify counterparties and timing.
    """
    # Get the wallet's transaction count (nonce) for age estimation
    nonce = await _rpc_call("eth_getTransactionCount", [wallet_address.lower(), "latest"])
    nonce_int = _hex_to_int(nonce) if nonce else 0

    # Get token transfers involving this wallet as a proxy for activity
    padded = _pad_address(wallet_address)
    to_logs = await _rpc_call("eth_getLogs", [{
        "topics": [TRANSFER_TOPIC, None, padded],
        "fromBlock": "0x0",
        "toBlock": "latest",
    }]) or []

    # Transform: create pseudo-txlist entries from token transfers
    # The first incoming transfer's sender is often the funding source
    txs = []
    for log in to_logs:
        if len(log.get("topics", [])) < 3:
            continue
        block_num = _hex_to_int(log.get("blockNumber", "0x0"))
        txs.append({
            "from": _unpad_address(log["topics"][1]),
            "to": wallet_address.lower(),
            "value": str(_hex_to_int(log.get("data", "0x0"))),
            "hash": log.get("transactionHash", ""),
            "blockNumber": str(block_num),
            "timeStamp": "0",  # Not available from logs
        })

    # Sort
    txs.sort(
        key=lambda x: int(x["blockNumber"]),
        reverse=(sort == "desc"),
    )

    # Pagination
    start = (page - 1) * offset
    end = start + offset
    return _etherscan_response(txs[start:end])


async def _get_token_supply(contract_address: str) -> dict | None:
    """Get total supply via eth_call to totalSupply()."""
    result = await _rpc_call("eth_call", [
        {"to": contract_address.lower(), "data": "0x18160ddd"},
        "latest",
    ])
    if result:
        supply = _hex_to_int(result)
        return _etherscan_response(str(supply))
    return None


async def _get_source_code(contract_address: str) -> dict | None:
    """Check if contract has code (source not available via RPC)."""
    code = await _rpc_call("eth_getCode", [contract_address.lower(), "latest"])
    has_code = code is not None and code != "0x" and len(code) > 2
    return _etherscan_response([{
        "SourceCode": "",
        "ABI": "",
        "ContractName": "",
        "Proxy": "0",
        "Implementation": "",
    }])


async def _get_token_info(contract_address: str) -> dict | None:
    """Get token name, symbol, decimals via eth_call."""
    addr = contract_address.lower()

    # name() = 0x06fdde03
    name_result = await _rpc_call("eth_call", [{"to": addr, "data": "0x06fdde03"}, "latest"])
    # symbol() = 0x95d89b41
    symbol_result = await _rpc_call("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])
    # decimals() = 0x313ce567
    decimals_result = await _rpc_call("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])

    def decode_string(hex_data: str | None) -> str:
        if not hex_data or hex_data == "0x" or len(hex_data) < 130:
            return ""
        try:
            # ABI-encoded string: offset (32 bytes) + length (32 bytes) + data
            data = bytes.fromhex(hex_data[2:])
            offset = int.from_bytes(data[0:32], "big")
            length = int.from_bytes(data[offset:offset + 32], "big")
            return data[offset + 32:offset + 32 + length].decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    name = decode_string(name_result)
    symbol = decode_string(symbol_result)
    decimals = _hex_to_int(decimals_result) if decimals_result else 18

    return _etherscan_response([{
        "name": name,
        "symbol": symbol,
        "decimals": str(decimals),
        "description": "",
    }])


# === Main router — translates Etherscan params to RPC calls ===


async def bscscan_request(params: dict) -> dict | None:
    """
    Translation layer: accepts Etherscan-style API parameters,
    routes to the appropriate MegaNode JSON-RPC call, and returns
    data in Etherscan response format.

    This means ZERO changes needed in scanner code.
    """
    module = params.get("module", "")
    action = params.get("action", "")

    try:
        # contract/getcontractcreation
        if module == "contract" and action == "getcontractcreation":
            return await _get_contract_creation(params["contractaddresses"])

        # token/tokenholderlist
        elif module == "token" and action == "tokenholderlist":
            return await _get_token_holders(
                params["contractaddress"],
                int(params.get("page", 1)),
                int(params.get("offset", 10)),
            )

        # account/tokentx (token transfers)
        elif module == "account" and action == "tokentx":
            address = params.get("address", "")
            contract = params.get("contractaddress", "")
            page = int(params.get("page", 1))
            offset = int(params.get("offset", 50))
            sort = params.get("sort", "desc")

            if address and not contract:
                # Wallet-level token transfers (all tokens)
                return await _get_token_transfers_by_wallet(
                    address, None, page, offset, sort
                )
            elif address and contract:
                # Wallet transfers for a specific token
                return await _get_token_transfers_by_wallet(
                    address, contract, page, offset, sort
                )
            elif contract:
                # Contract-level transfers (all holders)
                return await _get_token_transfers_by_contract(
                    contract, page, offset, sort
                )

        # token/tokentx (used in exchange_flow for contract-level transfers)
        elif module == "token" and action == "tokentx":
            return await _get_token_transfers_by_contract(
                params["contractaddress"],
                int(params.get("page", 1)),
                int(params.get("offset", 100)),
                params.get("sort", "desc"),
            )

        # account/txlist (normal transactions)
        elif module == "account" and action == "txlist":
            return await _get_txlist(
                params["address"],
                int(params.get("page", 1)),
                int(params.get("offset", 5)),
                params.get("sort", "asc"),
            )

        # stats/tokensupply
        elif module == "stats" and action == "tokensupply":
            return await _get_token_supply(params["contractaddress"])

        # contract/getsourcecode
        elif module == "contract" and action == "getsourcecode":
            return await _get_source_code(params["address"])

        # token/tokeninfo
        elif module == "token" and action == "tokeninfo":
            return await _get_token_info(params["contractaddress"])

        else:
            logger.warning(f"Unknown BscScan action: module={module} action={action}")
            return None

    except Exception as e:
        logger.error(f"bscscan_request translation error ({module}/{action}): {e}")
        return None
