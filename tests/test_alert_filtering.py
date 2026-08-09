"""ALERTS_MODE delivery filter.

"launches" (the default) delivers only actual token launches plus
watcher-health messages. Recon signals — team wallet txs, factory deploys,
frontend contract diffs — are logged and dropped. Detection is unaffected;
this filters delivery only.
"""
import importlib

import pytest

from stonk_watcher import alerts, config


@pytest.fixture
def delivered(monkeypatch):
    """Capture what actually reaches a channel, past the filter."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "chat")
    sent = []
    pipeline = alerts.AlertPipeline()
    monkeypatch.setattr(pipeline, "_send_telegram",
                        lambda text, level, code_lines: sent.append(text) or None)
    return pipeline, sent


def test_launches_mode_delivers_launches(monkeypatch, delivered):
    monkeypatch.setattr(config, "ALERTS_MODE", "launches")
    pipeline, sent = delivered
    pipeline.send("NEW LAUNCHER TOKEN (API)\n0xabc", category="launch")
    assert len(sent) == 1


def test_launches_mode_drops_recon(monkeypatch, delivered):
    monkeypatch.setattr(config, "ALERTS_MODE", "launches")
    pipeline, sent = delivered
    pipeline.send("TEAM WALLET OUTGOING TX\nmethod: deliverBatch", category="recon")
    pipeline.send("CANDIDATE FACTORY ACTIVITY\ntopic0: 0x1", category="recon")
    pipeline.send("NEW CONTRACT IN FRONTEND\n0xdef", category="recon")
    assert sent == [], "recon noise must not reach the phone"


def test_launches_mode_keeps_health(monkeypatch, delivered):
    """A broken watcher must still be able to say so."""
    monkeypatch.setattr(config, "ALERTS_MODE", "launches")
    pipeline, sent = delivered
    pipeline.send("WATCHER ERROR [chain_watcher]\nRPC DOWN", category="health")
    assert len(sent) == 1


def test_default_category_is_recon(monkeypatch, delivered):
    """An untagged alert stays quiet rather than leaking noise."""
    monkeypatch.setattr(config, "ALERTS_MODE", "launches")
    pipeline, sent = delivered
    pipeline.send("something new I forgot to tag")
    assert sent == []


def test_all_mode_delivers_everything(monkeypatch, delivered):
    monkeypatch.setattr(config, "ALERTS_MODE", "all")
    pipeline, sent = delivered
    pipeline.send("TEAM WALLET OUTGOING TX", category="recon")
    pipeline.send("NEW LAUNCHER TOKEN (API)", category="launch")
    pipeline.send("WATCHER ERROR", category="health")
    assert len(sent) == 3


def test_mode_defaults_to_launches(monkeypatch):
    monkeypatch.delenv("ALERTS_MODE", raising=False)
    cfg = importlib.reload(config)
    assert cfg.ALERTS_MODE == "launches"


def test_launch_paths_are_tagged():
    """Guard against a launch alert silently regressing to recon."""
    import inspect

    from stonk_watcher import api_poller, chain_watcher

    for module, marker in ((api_poller, "NEW LAUNCHER TOKEN"),
                           (chain_watcher, "NEW TOKEN VIA NEW FACTORY")):
        source = inspect.getsource(module)
        assert marker in source
        assert 'category="launch"' in source, (
            f"{module.__name__} must tag its launch alert")
