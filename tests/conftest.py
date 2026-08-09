import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stonk_watcher.alerts import ErrorReporter  # noqa: E402
from stonk_watcher.state import State  # noqa: E402


class FakePipeline:
    """Captures alerts instead of sending them."""

    def __init__(self):
        self.sent = []

    def send(self, text, level="alert", code_lines=None):
        self.sent.append({"text": text, "level": level, "code_lines": code_lines})

    def texts(self):
        return [s["text"] for s in self.sent]

    def find(self, needle):
        return [s for s in self.sent if needle in s["text"]]


@pytest.fixture
def pipeline():
    return FakePipeline()


@pytest.fixture
def errors(pipeline):
    return ErrorReporter(pipeline, cooldown=0)


@pytest.fixture
def state(tmp_path):
    return State(path=str(tmp_path / "state.json"))
