"""
AI Social Classifier — Shill vs organic social activity classification.

Uses Claude AI to classify social mentions as genuine community discussion
vs coordinated shill campaigns.
"""

import json
import logging

import anthropic
from backend.config import settings

logger = logging.getLogger(__name__)


def get_client() -> anthropic.Anthropic:
    """Create Anthropic client."""
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def classify_social(tweets: list[dict], token_symbol: str) -> dict:
    """
    Classify social mentions as organic or coordinated shill.

    Args:
        tweets: List of tweet objects (text, author, timestamp, metrics)
        token_symbol: The token ticker symbol

    Returns:
        Classification results dict
    """
    default_result = {
        "shill_pct": 0,
        "narrative": "unknown",
        "bot_count": 0,
        "influencer_mentions": [],
        "resembles_past_pump": False,
        "confidence": "low",
        "summary": "Analysis unavailable",
    }

    if not settings.ANTHROPIC_API_KEY:
        default_result["summary"] = "AI social classification unavailable — ANTHROPIC_API_KEY not configured."
        return default_result

    if not tweets:
        default_result["summary"] = "No tweets available for analysis."
        return default_result

    # Truncate to 50 tweets for context
    tweet_subset = tweets[:50]
    tweets_json = json.dumps(tweet_subset, indent=2, default=str)

    prompt = f"""Analyze these tweets about ${token_symbol} and classify the social activity.

TWEETS:
{tweets_json}

Classify:
1. organic_vs_shill: What % appears to be genuine community discussion vs coordinated promotion?
2. narrative_pushed: What narrative is being pushed? (AI, gaming, DeFi stablecoin, etc.)
3. bot_indicators: How many accounts show bot-like behavior? (new accounts, copy-paste text, coordinated timing)
4. influencer_involvement: Are any notable crypto influencers promoting this?
5. comparison_to_known_pumps: Does this social pattern resemble pre-pump activity for RAVE, SIREN, RIVER, ARIA, or STO?

Respond ONLY in JSON format:
{{
    "shill_pct": 0-100,
    "narrative": "string",
    "bot_count": integer,
    "influencer_mentions": ["list"],
    "resembles_past_pump": true/false,
    "confidence": "low/medium/high",
    "summary": "one-paragraph assessment"
}}"""

    try:
        client = get_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text

        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI social classification JSON: {e}")
        default_result["summary"] = "AI returned unparseable response."
        return default_result
    except Exception as e:
        logger.error(f"Error in AI social classification: {e}")
        default_result["summary"] = f"AI social classification failed: {e}"
        return default_result
