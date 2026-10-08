import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

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


# Set these before pytest imports test modules (which import the constants and
# functions with collection/view names captured as default arguments).
from khipumaq import index

_TEST_NAMES = {
    name: f"test_suite_{uuid4().hex}_{name}"
    for name in ("episodes", "episodes_search", "raw", "queries", "episodes_chat", "episodes_chat_search")
}
index.EPISODES = _TEST_NAMES["episodes"]
index.VIEW = _TEST_NAMES["episodes_search"]
index.RAW = _TEST_NAMES["raw"]
index.CHAT = _TEST_NAMES["episodes_chat"]
index.CHAT_VIEW = _TEST_NAMES["episodes_chat_search"]


@contextmanager
def _isolated_database():
    """All real-driver writes use disposable collections, without test opt-in.

    Constants cover ingest and index, including imported aliases. Driver mapping
    also covers literal collection names; AQL mapping covers legacy tests and
    collection bind parameters. Fake databases keep their normal names.
    """
    import re

    from arango.aql import AQL
    from arango.database import StandardDatabase
    from khipumaq.db import _CHECKOUT_CONFIG, get_database

    db = get_database(_CHECKOUT_CONFIG)
    # Only read this handle: the regression test checks the actual raw collection.
    real_raw = db.collection("raw") if db.has_collection("raw") else None
    execute = AQL.execute

    def execute_isolated(self, query, *args, **kwargs):
        query = re.sub(
            r"\b(IN|INTO)\s+(episodes_chat_search|episodes_chat|episodes_search|episodes|queries|raw)\b",
            lambda m: f"{m[1]} {_TEST_NAMES[m[2]]}", query,
        )
        if "bind_vars" in kwargs and kwargs["bind_vars"] is not None:
            kwargs["bind_vars"] = {
                key: _TEST_NAMES.get(value, value) if key.startswith("@") else value
                for key, value in kwargs["bind_vars"].items()
            }
        return execute(self, query, *args, **kwargs)

    def redirect(method):
        def wrapped(self, name, *args, **kwargs):
            return method(self, _TEST_NAMES.get(name, name), *args, **kwargs)
        return wrapped

    with pytest.MonkeyPatch.context() as patch:
        # Avoid the personal config even when a test calls get_database().
        patch.setenv("KHIPUMAQ_CONFIG", str(_CHECKOUT_CONFIG))
        for method in ("collection", "has_collection", "create_collection", "delete_collection"):
            patch.setattr(StandardDatabase, method, redirect(getattr(StandardDatabase, method)))
        patch.setattr(AQL, "execute", execute_isolated)
        try:
            index.ensure_index(db)
            yield db, real_raw
        finally:
            try:
                db.delete_view(index.VIEW, ignore_missing=True)
                db.delete_view(index.CHAT_VIEW, ignore_missing=True)
            finally:
                for name in ("episodes", "raw", "queries", "episodes_chat"):
                    db.delete_collection(_TEST_NAMES[name], ignore_missing=True)


# MCP builds its startup description during test collection, before fixtures.
# Create the private store before collection and clean up even on collection errors.
def pytest_sessionstart(session):
    session.config._isolated_database_context = _isolated_database()
    session.config._isolated_database = session.config._isolated_database_context.__enter__()


def pytest_sessionfinish(session, exitstatus):
    session.config._isolated_database_context.__exit__(None, None, None)


@pytest.fixture(scope="session")
def isolated_database(request):
    return request.config._isolated_database


@pytest.fixture
def stored_episode(isolated_database):
    """Insert A28 documents into the suite's private collection and remove them."""
    db, _ = isolated_database
    col = db.collection(index.EPISODES)
    keys = []

    def insert(**fields):
        document = {"_key": f"a28_{uuid4().hex}", **fields}
        col.insert(document)
        keys.append(document["_key"])
        return col.get(document["_key"])

    try:
        yield insert
    finally:
        for key in keys:
            col.delete(key, ignore_missing=True)


@pytest.fixture(autouse=True)
def isolate_user_directories(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
