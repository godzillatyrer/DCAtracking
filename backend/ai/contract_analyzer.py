"""
AI Contract Analyzer — Smart contract red flag detection.

Scans smart contract source code for manipulation red flags using Claude AI.
Checks for hidden mints, pause mechanisms, blacklists, proxy patterns, etc.
"""

import json
import logging

import anthropic
from backend.config import settings

logger = logging.getLogger(__name__)


def get_client() -> anthropic.Anthropic:
    """Create Anthropic client."""
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def analyze_contract(source_code: str, token_name: str) -> dict:
    """
    Analyze smart contract source for manipulation red flags.

    Args:
        source_code: Solidity source code from BscScan
        token_name: Name of the token for context

    Returns:
        Dict with red flags analysis:
        {
            "has_mint": bool,
            "has_pause": bool,
            "has_blacklist": bool,
            "has_proxy": bool,
            "hidden_fees": bool,
            "risk_level": "low/medium/high/critical",
            "red_flags": [str],
            "summary": str
        }
    """
    default_result = {
        "has_mint": False,
        "has_pause": False,
        "has_blacklist": False,
        "has_proxy": False,
        "hidden_fees": False,
        "risk_level": "unknown",
        "red_flags": [],
        "summary": "Analysis unavailable",
    }

    if not settings.ANTHROPIC_API_KEY:
        default_result["summary"] = "AI contract analysis unavailable — ANTHROPIC_API_KEY not configured."
        return default_result

    if not source_code:
        default_result["summary"] = "No source code available (unverified contract)."
        default_result["risk_level"] = "high"
        default_result["red_flags"] = ["Source code not verified on BscScan"]
        return default_result

    # Truncate source to fit context window
    truncated_source = source_code[:15000]

    prompt = f"""You are a Solidity smart contract auditor. Analyze this BSC token contract
and identify manipulation red flags.

TOKEN: {token_name}

CONTRACT SOURCE CODE:
{truncated_source}

Check for:
1. Hidden mint functions (can deployer create new tokens?)
2. Pause/unpause mechanisms (can deployer freeze trading?)
3. Blacklist functions (can deployer block specific addresses?)
4. Proxy/upgradable patterns (can contract logic be changed?)
5. Unusual fee structures (hidden taxes on buy/sell?)
6. Owner-only functions that could be used for rug pull
7. Hardcoded addresses that receive special treatment
8. Anything else that gives deployer disproportionate control

Respond ONLY in JSON format:
{{
    "has_mint": true/false,
    "has_pause": true/false,
    "has_blacklist": true/false,
    "has_proxy": true/false,
    "hidden_fees": true/false,
    "risk_level": "low/medium/high/critical",
    "red_flags": ["list of specific concerns"],
    "summary": "one-paragraph assessment"
}}"""

    try:
        client = get_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI contract analysis JSON: {e}")
        default_result["summary"] = "AI returned unparseable response."
        return default_result
    except Exception as e:
        logger.error(f"Error in AI contract analysis: {e}")
        default_result["summary"] = f"AI contract analysis failed: {e}"
        return default_result
