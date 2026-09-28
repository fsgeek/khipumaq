import sys

import pytest


@pytest.fixture(autouse=True)
def isolate_event_log(tmp_path, monkeypatch):
    """Default every test (and its subprocesses) to a private event log."""
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path / "events.jsonl"))


class DiscardingQueryHistory:
    def search(self, **kwargs):
        pass

    def recall(self, **kwargs):
        pass

    def describe(self):
        pass


@pytest.fixture(autouse=True)
def isolate_mcp_query_history(monkeypatch):
    """Server-tool tests must never write to the shared queries collection."""
    mcp_server = sys.modules.get("khipumaq.mcp_server")
    if mcp_server is None:
        return

    monkeypatch.setattr(mcp_server, "history", DiscardingQueryHistory())
