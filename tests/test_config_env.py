"""Config env handling.

Hosting dashboards create optional variables with an empty value when the
field is left blank; a blank must fall back to the default rather than
silently overriding it with "". Values are stripped because those fields are
textareas and pasted secrets often carry a trailing newline.
"""
import importlib

from stonk_watcher import config


def reload_config():
    return importlib.reload(config)


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RPC_URL", "")
    cfg = reload_config()
    assert cfg.RPC_URL == cfg.BLOCKSCOUT_BASE + "/api/eth-rpc", (
        "a blank RPC_URL must not override the default with an empty string")


def test_whitespace_only_env_falls_back(monkeypatch):
    monkeypatch.setenv("RPC_URL", "   \n  ")
    cfg = reload_config()
    assert cfg.RPC_URL.startswith("https://")


def test_env_value_is_stripped(monkeypatch):
    # Textarea inputs commonly append a newline to a pasted secret.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF\n")
    cfg = reload_config()
    assert cfg.TELEGRAM_BOT_TOKEN == "123456:ABC-DEF"


def test_real_value_still_wins(monkeypatch):
    monkeypatch.setenv("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    cfg = reload_config()
    assert cfg.RPC_URL == "https://rpc.mainnet.chain.robinhood.com"


def test_blank_optional_address_stays_falsy(monkeypatch):
    monkeypatch.setenv("AMM_FACTORY_ADDRESS", "")
    cfg = reload_config()
    assert not cfg.AMM_FACTORY_ADDRESS, "blank AMM factory disables that scan"


def test_cleanup_restores_defaults(monkeypatch):
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = reload_config()
    assert cfg.RPC_URL.startswith("https://")
