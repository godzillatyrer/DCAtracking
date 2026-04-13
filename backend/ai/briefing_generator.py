"""
AI Briefing Generator — Claude-powered alert summaries.

Generates human-readable alert briefings when a token's score
crosses the alert threshold (70+). Uses Anthropic Claude API.
"""

import json
import logging

import anthropic
from backend.config import settings

logger = logging.getLogger(__name__)


def get_client() -> anthropic.Anthropic:
    """Create Anthropic client."""
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def generate_briefing(
    token_data: dict,
    known_wallet_matches: list[dict] | None = None,
    pattern_matches: dict | None = None,
) -> str:
    """
    Generate a human-readable alert briefing.

    Args:
        token_data: Token metrics (name, symbol, price, volume, score, etc.)
        known_wallet_matches: List of known operator wallets found in holders
        pattern_matches: Similarity scores to known pump patterns

    Returns:
        AI-generated briefing text (max ~200 words)
    """
    if not settings.ANTHROPIC_API_KEY:
        return "AI briefing unavailable — ANTHROPIC_API_KEY not configured."

    wallet_section = "None detected"
    if known_wallet_matches:
        wallet_section = json.dumps(known_wallet_matches, indent=2)

    pattern_section = "No pattern analysis available"
    if pattern_matches:
        pattern_section = json.dumps(pattern_matches, indent=2)

    prompt = f"""You are a crypto forensic analyst. Analyze this token and generate a concise
alert briefing (max 200 words) for a trader who needs to decide whether to enter a position.

TOKEN DATA:
- Name: {token_data.get('name', 'Unknown')} ({token_data.get('symbol', '?')})
- Contract: {token_data.get('contract_address', 'N/A')}
- Chain: BSC
- Price: ${token_data.get('price_usd', 'N/A')}
- 24h Volume: ${token_data.get('volume_24h', 'N/A')}
- Market Cap: ${token_data.get('market_cap', 'N/A')}
- Float: {token_data.get('float_pct', 'N/A')}% of max supply circulating
- Top 10 Holders: {token_data.get('top_10_holder_pct', 'N/A')}% of supply
- Token Age: {token_data.get('token_age_days', 'N/A')} days
- Binance Alpha: {token_data.get('binance_alpha', False)}
- Score: {token_data.get('score', 0)}/100

WALLET ANALYSIS:
- Cluster detected: {token_data.get('cluster_detected', False)}
- Cluster wallets: {token_data.get('cluster_wallet_count', 0)}
- Exchange deposits from insiders: {token_data.get('exchange_deposits_detected', False)}

KNOWN OPERATOR MATCHES:
{wallet_section}

PATTERN SIMILARITY (compared to confirmed pumps RAVE, SIREN, RIVER, ARIA, STO):
{pattern_section}

Generate the briefing with:
1. One-line verdict (e.g., "HIGH CONFIDENCE — Matches SIREN pattern")
2. Key signals detected
3. Which confirmed pump pattern it most closely resembles and why
4. Risk factors
5. Suggested monitoring focus

Be direct and actionable. No disclaimers."""

    try:
        client = get_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Error generating AI briefing: {e}")
        return f"AI briefing generation failed: {e}"
