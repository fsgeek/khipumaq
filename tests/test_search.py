import json
from uuid import uuid4

import pytest

from khipumaq.db import get_database
from khipumaq import index
from khipumaq.index import ANALYZER, EPISODES, INDEXED_FIELDS, OPT_IN_LABELS, ensure_chat_index, ensure_index
from khipumaq.ingest import ingest_file
from khipumaq.search import search


@pytest.fixture
def search_view():
    """A real view over private test episodes; never link the live collection."""
    db = get_database()
    name = f"test_search_{uuid4().hex}"
    view = f"{name}_view"
    col = db.create_collection(name)
    try:
        db.create_arangosearch_view(view, properties={
            "links": {
                name: {
                    "fields": {
                        field: {"analyzers": [ANALYZER]}
                        for field in INDEXED_FIELDS
                    }
                }
            }
        })
        yield db, col, view
    finally:
        try:
            db.delete_view(view, ignore_missing=True)
        finally:
            db.delete_collection(name, ignore_missing=True)


@pytest.mark.parametrize("other_field", ["response", "state_text"])
def test_total_all_requires_every_token_across_fields(search_view, other_field):
    db, col, view = search_view
    col.insert({"_key": "both", "cycle": 1,
                "user_message": "heliotrope", other_field: "cantilever"})
    col.insert({"_key": "partial", "cycle": 2, "response": "heliotrope"})
    col.insert({"_key": "unrelated", "cycle": 3, "response": "sandbar"})

    result = search(db, "heliotrope cantilever", view=view)

    assert set(result) == {"total", "total_all", "hits"}
    assert result["total"] == 2
    assert result["total_all"] == 1
    assert {hit["key"] for hit in result["hits"]} == {"both", "partial"}
    limited = search(db, "heliotrope cantilever", view=view, limit=1)
    assert limited["total"] == 2
    assert limited["total_all"] == 1
    assert len(limited["hits"]) == 1


def test_total_all_uses_analyzer_to_split_hyphenated_query(search_view):
    db, col, view = search_view
    col.insert({"_key": "partial", "cycle": 1, "response": "test"})

    partial = search(db, "ubuntu26-test", view=view)
    assert partial["total"] == 1
    assert partial["total_all"] == 0
    assert [hit["key"] for hit in partial["hits"]] == ["partial"]

    col.insert({"_key": "both", "cycle": 2,
                "user_message": "ubuntu26", "response": "test"})
    both = search(db, "ubuntu26-test", view=view)
    assert both["total"] == 2
    assert both["total_all"] == 1
    assert {hit["key"] for hit in both["hits"]} == {"partial", "both"}


@pytest.mark.parametrize("filters, expected_all", [
    ({}, 4),
    ({"scope": "target"}, 3),
    ({"since": "2026-06-10T00:00:00Z"}, 3),
    ({"until": "2026-06-20T00:00:00Z"}, 3),
    ({"scope": "target", "since": "2026-06-10T00:00:00Z",
      "until": "2026-06-20T00:00:00Z"}, 1),
    ({"scope": "missing"}, 0),
])
def test_total_all_obeys_scope_and_inclusive_exclusive_window(
    search_view, filters, expected_all,
):
    db, col, view = search_view
    for cycle, (scope, ts) in enumerate([
        ("target", "2026-06-09T23:59:59Z"),
        ("target", "2026-06-10T00:00:00Z"),
        ("target", "2026-06-20T00:00:00Z"),
        ("other", "2026-06-15T00:00:00Z"),
    ]):
        col.insert({"cycle": cycle, "experiment_label": scope, "ts": ts,
                    "user_message": "heliotrope", "response": "cantilever"})
    col.insert({"cycle": 4, "experiment_label": "target",
                "ts": "2026-06-15T00:00:00Z", "response": "heliotrope"})

    result = search(db, "heliotrope cantilever", view=view, **filters)

    assert result["total_all"] == expected_all
    expected_total = expected_all + (filters.get("scope") != "missing")
    assert result["total"] == expected_total
    assert len(result["hits"]) == expected_total


def test_opt_in_labels_live_apart_so_no_default_search_can_reach_them(isolated_database):
    """The chats are the ayllu's to opt into. They are in their own collection and
    view, so even a client with older code, which knows only the default view and
    has no filter for them, cannot return them under scope "all"."""
    db, _ = isolated_database
    ensure_index(db)
    ensure_chat_index(db)
    assert {"claude-ai-chat", "chatgpt-chat"} <= set(OPT_IN_LABELS)  # chat exports, one label per source
    chat_label = "claude-ai-chat"
    marker = f"optinmarker{uuid4().hex}"
    code = db.collection(index.EPISODES)
    chat = db.collection(index.CHAT)
    code.insert({"_key": f"code-{marker}", "cycle": 1, "experiment_label": "khipumaq", "ts": "2026-10-01T00:00:00Z",
                 "user_message": marker, "response": "cantilever"})
    chat.insert({"_key": f"chat-{marker}", "cycle": 2, "experiment_label": chat_label, "ts": "2026-10-01T00:00:00Z",
                 "user_message": marker, "response": "cantilever"})
    try:
        everything = search(db, f"{marker} cantilever")
        assert {h["key"] for h in everything["hits"]} == {f"code-{marker}"}
        assert (everything["total"], everything["total_all"]) == (1, 1)

        # What an older client does: the default view, no opt-in filter, scope "all".
        old_client = search(db, f"{marker} cantilever", scope="all", view=index.VIEW)
        assert f"chat-{marker}" not in {h["key"] for h in old_client["hits"]}

        named = search(db, f"{marker} cantilever", scope=chat_label)
        assert {h["key"] for h in named["hits"]} == {f"chat-{marker}"}
        assert (named["total"], named["total_all"]) == (1, 1)

        other = search(db, f"{marker} cantilever", scope="khipumaq")
        assert {h["key"] for h in other["hits"]} == {f"code-{marker}"}
    finally:
        code.delete(f"code-{marker}", ignore_missing=True)
        chat.delete(f"chat-{marker}", ignore_missing=True)


def test_an_opt_in_label_misfiled_in_the_default_store_is_still_left_out_of_all(isolated_database):
    """Second layer: if some importer writes an opt-in label into the default
    collection by mistake, scope "all" still does not return it."""
    db, _ = isolated_database
    ensure_index(db)
    marker = f"misfiledmarker{uuid4().hex}"
    col = db.collection(index.EPISODES)
    col.insert({"_key": f"ok-{marker}", "cycle": 1, "experiment_label": "khipumaq", "ts": "2026-10-01T00:00:00Z",
                "user_message": marker, "response": "cantilever"})
    col.insert({"_key": f"misfiled-{marker}", "cycle": 2, "experiment_label": "chatgpt-chat", "ts": "2026-10-01T00:00:00Z",
                "user_message": marker, "response": "cantilever"})
    try:
        result = search(db, f"{marker} cantilever")
        assert {h["key"] for h in result["hits"]} == {f"ok-{marker}"}
        assert (result["total"], result["total_all"]) == (1, 1)
    finally:
        col.delete(f"ok-{marker}", ignore_missing=True)
        col.delete(f"misfiled-{marker}", ignore_missing=True)


@pytest.mark.parametrize("query", ["", "--- !!!"])
def test_total_all_is_zero_when_analyzer_drops_every_token(search_view, query):
    db, col, view = search_view
    col.insert({"cycle": 1, "response": "heliotrope cantilever"})
    assert next(db.aql.execute(
        "RETURN TOKENS(@q, @analyzer)",
        bind_vars={"q": query, "analyzer": ANALYZER},
    )) == []

    assert search(db, query, view=view) == {"total": 0, "total_all": 0, "hits": []}


def test_repeated_query_words_do_not_change_total_all(search_view):
    db, col, view = search_view
    col.insert({"cycle": 1, "user_message": "heliotrope", "response": "cantilever"})
    col.insert({"cycle": 2, "response": "heliotrope"})

    original = search(db, "heliotrope cantilever", view=view)
    repeated = search(db, "heliotrope cantilever heliotrope cantilever", view=view)

    assert original["total_all"] == repeated["total_all"] == 1
    assert original["total"] == repeated["total"] == 2
    assert {hit["key"] for hit in original["hits"]} == {
        hit["key"] for hit in repeated["hits"]
    }


def test_search_finds_a_phrase_the_instance_said_in_response(tmp_path):
    """The canonical manufactured-silence case: a phrase that lives only in the
    conversational `response` field must be findable."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900010"
    try:
        # Unique marker phrase so this is deterministic regardless of what else
        # lives in the shared episodes collection (e.g. an ingested corpus). The
        # phrase exists ONLY in the conversational `response` field.
        rec = {
            "cycle": 900010,
            "user_message": "the human asks about trust",
            "raw_output": {"response": "I meant the heliotrope cantilever marker specifically"},
            "state": {"x": "unrelated noise"},
        }
        p = tmp_path / "s.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        results = search(db, "heliotrope cantilever", limit=5)["hits"]

        assert 900010 in [r["cycle"] for r in results]
    finally:
        if col.has(key):
            col.delete(key)


def test_search_result_carries_key_for_recall(tmp_path):
    """A search hit must hand back the episode's `_key`. search ranks and gives a
    snippet; recall(db, key) reads it in full. The `key` is the seam between them
    — without it the instance can't recall what it found and falls back to the
    filesystem, which is the manufactured silence this whole tool exists to kill."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900040"
    try:
        rec = {
            "cycle": 900040,
            "user_message": "seam question",
            "raw_output": {"response": "the kingfisher escarpment marker"},
            "state": {},
        }
        p = tmp_path / "k.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        results = search(db, "kingfisher escarpment", limit=5)["hits"]
        hit = next(r for r in results if r["cycle"] == 900040)

        assert hit["key"] == key
    finally:
        if col.has(key):
            col.delete(key)


def test_search_result_carries_episode_provenance(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    marker = uuid4().hex
    cycle = 910_000_000 + (uuid4().int % 80_000_000)
    key = str(cycle)
    timestamp = "2026-09-03T17:45:12Z"
    label = f"provenance-label-{marker}"
    path = tmp_path / "provenance.jsonl"
    try:
        record = {
            "cycle": cycle,
            "timestamp": timestamp,
            "experiment_label": label,
            "user_message": "provenance question",
            "raw_output": {
                "response": f"the provenance quasar marker is {marker}"
            },
            "state": {},
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        ingest_file(db, path)

        results = search(db, f"provenance quasar {marker}", limit=5)["hits"]
        hit = next(result for result in results if result["key"] == key)

        assert hit["ts"] == timestamp
        assert hit["experiment_label"] == label
        assert hit["source_file"] == str(path)
    finally:
        if col.has(key):
            col.delete(key)


def test_scope_partitions_corpora_by_experiment_label(tmp_path):
    """Two episodes share one unique marker phrase but belong to different
    corpora. `scope="claude_code"` must surface only the claude_code episode, so
    a live-session query cannot reach taste_open episodes (and vice versa)."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    keys = ["900020", "900021"]
    try:
        marker = "xylophone perimeter sandbar"  # unique, shared by both episodes
        recs = [
            {
                "cycle": 900020,
                "experiment_label": "claude_code",
                "user_message": "live session turn",
                "raw_output": {"response": f"discussing the {marker} at length"},
                "state": {},
            },
            {
                "cycle": 900021,
                "experiment_label": "taste_open",
                "user_message": "eval corpus turn",
                "raw_output": {"response": f"the {marker} appears here too"},
                "state": {},
            },
        ]
        p = tmp_path / "two.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in recs))
        ingest_file(db, p)

        all_cycles = [
            r["cycle"]
            for r in search(db, marker, scope="all", limit=10)["hits"]
        ]
        assert 900020 in all_cycles and 900021 in all_cycles

        cc_cycles = [
            r["cycle"]
            for r in search(db, marker, scope="claude_code", limit=10)["hits"]
        ]
        assert 900020 in cc_cycles
        assert 900021 not in cc_cycles
    finally:
        for k in keys:
            if col.has(k):
                col.delete(k)


def test_search_total_counts_all_matches_before_limit(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    marker = f"totalmarker{uuid4().hex}"
    first_cycle = 920_000_000 + (uuid4().int % 60_000_000)
    cycles = [first_cycle + offset for offset in range(3)]
    keys = [str(cycle) for cycle in cycles]
    try:
        records = [
            {
                "cycle": cycle,
                "user_message": "count every matching episode",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            }
            for cycle in cycles
        ]
        path = tmp_path / "total.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records))
        ingest_file(db, path)

        result = search(db, marker, limit=1)

        assert result["total"] == 3
        assert len(result["hits"]) == 1
    finally:
        for key in keys:
            if col.has(key):
                col.delete(key)


def test_search_since_and_until_apply_inclusive_exclusive_window(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    marker = f"windowmarker{uuid4().hex}"
    first_cycle = 920_000_000 + (uuid4().int % 60_000_000)
    cycles = [first_cycle + offset for offset in range(4)]
    keys = [str(cycle) for cycle in cycles]
    since = "2026-06-10T00:00:00Z"
    until = "2026-06-20T00:00:00Z"
    timestamps = (
        "2026-06-09T23:59:59Z",
        since,
        "2026-06-15T12:00:00Z",
        until,
    )
    try:
        records = [
            {
                "cycle": cycle,
                "timestamp": timestamp,
                "user_message": "check timestamp boundaries",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            }
            for cycle, timestamp in zip(cycles, timestamps)
        ]
        path = tmp_path / "window.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records))
        ingest_file(db, path)

        since_result = search(db, marker, since=since, limit=10)
        until_result = search(db, marker, until=until, limit=10)
        window_result = search(db, marker, since=since, until=until, limit=10)

        assert {hit["cycle"] for hit in since_result["hits"]} == set(cycles[1:])
        assert since_result["total"] == 3
        assert {hit["cycle"] for hit in until_result["hits"]} == set(cycles[:3])
        assert until_result["total"] == 3
        assert {hit["cycle"] for hit in window_result["hits"]} == set(cycles[1:3])
        assert window_result["total"] == 2
    finally:
        for key in keys:
            if col.has(key):
                col.delete(key)


def test_search_scope_and_since_filters_combine(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    marker = f"scopetimemarker{uuid4().hex}"
    label = f"scope-time-{uuid4().hex}"
    other_label = f"other-scope-time-{uuid4().hex}"
    first_cycle = 920_000_000 + (uuid4().int % 60_000_000)
    cycles = [first_cycle + offset for offset in range(3)]
    keys = [str(cycle) for cycle in cycles]
    since = "2026-07-10T00:00:00Z"
    try:
        records = [
            {
                "cycle": cycles[0],
                "timestamp": "2026-07-09T23:59:59Z",
                "experiment_label": label,
                "user_message": "matching scope before window",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            },
            {
                "cycle": cycles[1],
                "timestamp": since,
                "experiment_label": label,
                "user_message": "matching scope inside window",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            },
            {
                "cycle": cycles[2],
                "timestamp": "2026-07-11T00:00:00Z",
                "experiment_label": other_label,
                "user_message": "other scope inside window",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            },
        ]
        path = tmp_path / "scope-window.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records))
        ingest_file(db, path)

        result = search(db, marker, scope=label, since=since, limit=10)

        assert result["total"] == 1
        assert [hit["cycle"] for hit in result["hits"]] == [cycles[1]]
    finally:
        for key in keys:
            if col.has(key):
                col.delete(key)


def test_search_without_filters_returns_empty_envelope_for_no_match():
    db = get_database()
    ensure_index(db)
    marker = f"missingmarker{uuid4().hex}"

    result = search(db, marker)

    assert result["total"] == 0
    assert result["hits"] == []
