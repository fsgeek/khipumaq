import json
from uuid import uuid4

import pytest

from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index
from khipumaq.ingest import ingest_file
from khipumaq.recall import recall


def test_recall_returns_full_episode_by_key(tmp_path):
    """recall is the second half of the reach: search says WHICH cycle, recall
    returns that cycle IN FULL. The response here is longer than search's 200-char
    snippet, so recalling the whole thing proves recall reads the episode rather
    than echoing a teaser (the filesystem-fallback this tool exists to prevent)."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900030"
    long_response = "the marker " + "provenance " * 60  # > 200 chars
    try:
        rec = {
            "cycle": 900030,
            "user_message": "what did you mean?",
            "raw_output": {"response": long_response},
            "state": {"observation": "kept whole"},
        }
        p = tmp_path / "r.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        episode = recall(db, key)

        assert episode is not None
        assert episode["_key"] == key
        assert episode["response"] == long_response  # full, not truncated
        assert episode["user_message"] == "what did you mean?"
    finally:
        if col.has(key):
            col.delete(key)


def test_recall_returns_none_for_missing_key():
    """A reach for a key that isn't there is a clean miss, not an error."""
    db = get_database()
    assert recall(db, "no-such-key-900031") is None


@pytest.mark.parametrize("later_count", [0, 1, 2, 3, 4, 6])
def test_recall_then_orders_and_limits_later_turns(
    isolated_database, stored_episode, later_count
):
    """A28: order by ts, show at most three, and count all remaining turns."""
    db, _ = isolated_database
    session = uuid4().hex
    episode = stored_episode(
        session_id=session, ts="2026-10-01T12:00:00Z",
        user_message="original question", response="original answer",
    )
    later = []
    # Insertion and key order oppose timestamp order. Text lengths cover short,
    # exactly 200, and over 200 characters, including multibyte characters.
    for minute in reversed(range(1, later_count + 1)):
        turn = stored_episode(
            _key=f"a28_{session}_{10 - minute}",
            session_id=session, ts=f"2026-10-01T12:{minute:02d}:00Z",
            user_message=("ñ" * 201, "u" * 200, "short question")[(minute - 1) % 3],
            response=("short answer", "答" * 201, "r" * 200)[(minute - 1) % 3],
            model="not part of the preview",
        )
        later.insert(0, turn)

    result = recall(db, episode["_key"])

    assert result["then"] == {
        "turns": [
            {
                "key": turn["_key"], "ts": turn["ts"],
                "user_message": turn["user_message"][:200],
                "response": turn["response"][:200],
            }
            for turn in later[:3]
        ],
        "more": max(later_count - 3, 0),
    }


def test_recall_then_excludes_other_sessions_and_nonlater_turns(
    isolated_database, stored_episode
):
    db, _ = isolated_database
    session = uuid4().hex
    episode = stored_episode(
        session_id=session, ts="2026-10-01T12:00:00Z",
        user_message="question", response="answer",
    )
    for other_session, ts in [
        (session, "2026-10-01T11:59:59Z"),
        (session, episode["ts"]),
        (f"other_{session}", "2026-10-01T11:59:59Z"),
        (f"other_{session}", episode["ts"]),
        (f"other_{session}", "2026-10-01T12:00:01Z"),
        (f"other_{session}", "2026-10-01T12:00:03Z"),
    ]:
        stored_episode(
            session_id=other_session, ts=ts,
            user_message="excluded question", response="excluded answer",
        )
    later = stored_episode(
        session_id=session, ts="2026-10-01T12:00:02Z",
        user_message="later question", response="later answer",
    )

    assert recall(db, episode["_key"])["then"] == {
        "turns": [{
            "key": later["_key"], "ts": later["ts"],
            "user_message": "later question", "response": "later answer",
        }],
        "more": 0,
    }


def test_recall_last_turn_has_empty_then(isolated_database, stored_episode):
    db, _ = isolated_database
    session = uuid4().hex
    stored_episode(
        session_id=session, ts="2026-10-01T12:00:00Z",
        user_message="first question", response="first answer",
    )
    last = stored_episode(
        session_id=session, ts="2026-10-01T12:01:00Z",
        user_message="last question", response="last answer",
    )

    assert recall(db, last["_key"])["then"] == {"turns": [], "more": 0}


@pytest.mark.parametrize("missing", [("session_id",), ("ts",), ("session_id", "ts")])
def test_recall_without_thread_metadata_has_no_then(
    isolated_database, stored_episode, missing
):
    db, _ = isolated_database
    fields = {
        "session_id": uuid4().hex, "ts": "2026-10-01T12:00:00Z",
        "user_message": "legacy question", "response": "legacy answer",
    }
    for field in missing:
        del fields[field]
    episode = stored_episode(**fields)

    result = recall(db, episode["_key"])

    assert result == episode
    assert "then" not in result


def test_recall_adds_then_without_changing_episode_or_storage(
    isolated_database, stored_episode
):
    db, _ = isolated_database
    session = uuid4().hex
    before = stored_episode(
        session_id=session, ts="2026-10-01T12:00:00Z",
        user_message="full original question " * 30,
        response="full original answer " * 30,
        model="test-model", host="test-host", experiment_label="a28",
        source_file="/fixture/session.jsonl", state_text="original state",
        metadata={"nested": ["preserved", 28]},
    )
    later = stored_episode(
        session_id=session, ts="2026-10-01T12:01:00Z",
        user_message="correction question", response="corrected answer",
    )

    result = recall(db, before["_key"])

    assert result["then"]["turns"][0]["key"] == later["_key"]
    assert {key: value for key, value in result.items() if key != "then"} == before
    stored = db.collection(EPISODES).get(before["_key"])
    assert "then" not in stored
    assert stored == before  # Includes _rev: even a write of unchanged fields fails.
    assert db.collection(EPISODES).get(later["_key"]) == later
