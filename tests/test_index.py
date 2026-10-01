from uuid import uuid4

from khipumaq import index
from khipumaq.db import get_database
from khipumaq.index import EPISODES, VIEW, ensure_index


def test_ensure_index_creates_collection_and_view():
    db = get_database()
    ensure_index(db)

    assert db.has_collection(EPISODES)

    view_names = [v["name"] for v in db.views()]
    assert VIEW in view_names

    # the view must link the conversational fields, not just state
    view = db.view(VIEW)
    fields = view["links"][EPISODES]["fields"]
    assert "user_message" in fields
    assert "response" in fields
    assert "state_text" in fields
    assert "queries" not in view["links"]


def test_ensure_index_is_idempotent():
    db = get_database()
    ensure_index(db)
    ensure_index(db)  # must not raise on second call
    assert db.has_collection(EPISODES)


def test_ensure_thread_index_is_idempotent(isolated_database, monkeypatch):
    """A28: a fresh private collection gets exactly one persistent thread index."""
    db, _ = isolated_database
    name = f"{EPISODES}_thread_{uuid4().hex}"
    col = db.create_collection(name)
    monkeypatch.setattr(index, "EPISODES", name)
    try:
        assert not any(i["fields"] == ["session_id", "ts"] for i in col.indexes())

        index.ensure_thread_index(db)
        first = [i for i in col.indexes() if i["fields"] == ["session_id", "ts"]]
        assert len(first) == 1
        assert first[0]["type"] == "persistent"

        index.ensure_thread_index(db)
        second = [i for i in col.indexes() if i["fields"] == ["session_id", "ts"]]
        assert len(second) == 1
        assert second[0]["type"] == "persistent"
        assert second[0]["id"] == first[0]["id"]
    finally:
        db.delete_collection(name, ignore_missing=True)
