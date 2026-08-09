"""Acceptance test 3 (live-network integration, opt-in).

Points the candidate-factory logic at AMMFactoryV2 seeded from block
29738000: it must discover MANCER/KIDDIES/MTH from raw logs WITHOUT being
given the MarketCreated topic, proving the address-extraction heuristic
works end to end.

Requires network access plus:
  INTEGRATION=1
  AMM_FACTORY_ADDRESS=0x...   (AMMFactoryV2 on Robinhood Chain)
  RPC_URL=...                 (optional; defaults to the Blockscout eth-rpc proxy)

Run:  INTEGRATION=1 AMM_FACTORY_ADDRESS=0x... python3 -m pytest tests/test_integration_ammfactory.py -v
"""
import os

import pytest

from stonk_watcher.blockscout import BlockscoutClient
from stonk_watcher.chain_watcher import ChainWatcher
from stonk_watcher.rpc import RpcClient
from stonk_watcher.state import State

SEED_BLOCK = 29738000
EXPECTED_SYMBOLS = {"MANCER", "KIDDIES", "MTH"}

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1" or not os.environ.get("AMM_FACTORY_ADDRESS"),
    reason="set INTEGRATION=1 and AMM_FACTORY_ADDRESS=0x... to run live test",
)


class CapturePipeline:
    def __init__(self):
        self.sent = []

    def send(self, text, level="alert", code_lines=None):
        self.sent.append(text)


class NullReporter:
    def report(self, *a, **k):
        pass

    def report_exception(self, *a, **k):
        pass

    def recovered(self, *a, **k):
        pass


def test_discovers_known_tokens_from_raw_logs(tmp_path):
    factory = os.environ["AMM_FACTORY_ADDRESS"]
    state = State(path=str(tmp_path / "state.json"))
    state.add_candidate_factory(factory, SEED_BLOCK)
    pipeline = CapturePipeline()
    watcher = ChainWatcher(state, pipeline, NullReporter(),
                           rpc=RpcClient(), blockscout=BlockscoutClient())

    head = min(watcher.rpc.block_number(), SEED_BLOCK + 200000)
    watcher.scan_candidate_factories(head)

    tracked_symbols = {info.get("symbol", "").upper()
                       for info in state.data["tracked"].values()}
    missing = EXPECTED_SYMBOLS - tracked_symbols
    assert not missing, (
        f"heuristic failed to discover {missing} from raw logs; "
        f"found only {tracked_symbols}")
    fstate = state.candidate_factories()[factory.lower()]
    assert fstate["learned_topic0"], "MarketCreated topic0 should have been learned"
