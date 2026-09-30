"""A27 / issue #1: intake preserves records, even those that yield no prose."""
import base64
import json

import pytest

from khipumaq import index
from khipumaq.db import get_database
from khipumaq.ingest import ingest_claude_session, ingest_codex_rollout, ingest_raw


TS = "2026-09-30T10:00:00Z"


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberate whitespace, Unicode and CRLF catch parse/re-serialize storage.
    path.write_bytes(b"".join(
        ("  " + json.dumps(record, ensure_ascii=False) + " \r\n").encode("utf-8")
        for record in records
    ))
    return path


def claude_record(kind, content, **extra):
    return {
        "type": kind, "uuid": f"a27-{kind}", "sessionId": "a27-session",
        "timestamp": TS, "message": {"role": kind, "content": content}, **extra,
    }


def codex_record(kind, payload):
    return {"timestamp": TS, "type": kind, "payload": payload}


@pytest.fixture(params=["claude", "codex"])
def transcript(request, tmp_path):
    kind = request.param
    path = tmp_path / f"{kind}.jsonl"
    if kind == "claude":
        prose = claude_record("assistant", [{"type": "text", "text": "Sí, khipu 🧶"}])
        prose["message"]["usage"] = {
            "input_tokens": 17, "output_tokens": 9,
            "cache_creation_input_tokens": 11, "cache_read_input_tokens": 23,
            "future_usage": {"unknown": [1, None, True]},
        }
        records = [
            claude_record("user", "Keep every key"),
            claude_record("assistant", [{"type": "tool_use", "id": "call-1",
                          "name": "Read", "input": {"file_path": "fixture.txt"}}],
                          uuid="a27-tool-only"),
            claude_record("user", [{"type": "tool_result", "tool_use_id": "call-1",
                          "content": "fixture output", "is_error": False}]),
            prose,
            claude_record("user", "harness context", isMeta=True),
            {"type": "system", "subtype": "turn_duration", "durationMs": 42},
        ]
        child = path.with_suffix("") / "subagents" / "agent-child.jsonl"
        write_records(child, [
            claude_record("user", "child task", agentId="child"),
            claude_record("assistant", "child answer", uuid="a27-child", agentId="child"),
        ])
        files = [path, child]
        ingest = ingest_claude_session
    else:
        records = [
            codex_record("session_meta", {"id": "a27-codex", "cwd": "/fixture/project",
                          "cli_version": "fixture", "originator": "test"}),
            codex_record("response_item", {"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "Keep every key"}]}),
            codex_record("response_item", {"type": "function_call", "call_id": "c1",
                          "name": "exec_command", "arguments": '{"cmd":"true"}'}),
            codex_record("response_item", {"type": "function_call_output", "call_id": "c1",
                          "output": "fixture output"}),
            codex_record("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 91, "cached_input_tokens": 70,
                                      "output_tokens": 12, "reasoning_output_tokens": 8}},
                "rate_limits": {"primary": {"used_percent": 1.5}}}),
            codex_record("event_msg", {"type": "task_complete", "future_key": [False, None]}),
            codex_record("response_item", {"type": "message", "role": "assistant", "id": "m1",
                          "content": [{"type": "output_text", "text": "Sí, khipu 🧶"}]}),
        ]
        files = [path]
        ingest = ingest_codex_rollout
    write_records(path, records)
    return kind, path, files, ingest


def stored(db, path):
    return sorted(db.collection(index.RAW).find({"source_file": str(path)}),
                  key=lambda doc: doc["line"])


def test_every_line_and_every_json_key_survives_and_reconstructs(transcript):
    kind, path, files, ingest = transcript
    db = get_database()
    count = ingest(db, path, "a27", host="fixture-host", machine_id="fixture-machine")
    assert count == (2 if kind == "claude" else 1)
    for file in files:
        original = file.read_bytes()
        lines = original.split(b"\n")[:-1]
        docs = stored(db, file)
        assert [doc["line"] for doc in docs] == list(range(len(lines)))
        assert ("\n".join(doc["text"] for doc in docs) + "\n").encode("utf-8") == original
        for doc, line in zip(docs, lines, strict=True):
            assert json.loads(doc["text"]) == json.loads(line)
            assert "b64" not in doc
            assert (doc["kind"], doc["host"], doc["machine_id"]) == (
                kind, "fixture-host", "fixture-machine")


def test_suite_style_ingest_does_not_leak_into_real_raw(transcript, isolated_database):
    _, path, files, ingest = transcript
    db, real_raw = isolated_database
    ingest(get_database(), path, "a27")  # same entry point as existing suite tests
    for file in files:
        assert stored(db, file)
        if real_raw is not None:
            assert list(real_raw.find({"source_file": str(file)})) == []
    # Also handle installations where real raw did not exist before the suite.
    if real_raw is None:
        assert not any(c["name"] == "raw" for c in db.collections())


def test_unfinished_line_waits_until_newline(tmp_path):
    db = get_database()
    path = tmp_path / "growing.jsonl"
    path.write_bytes(b'{"first":1}\n{"second":2}')
    assert ingest_raw(db, path, "claude") == 1
    before = stored(db, path)
    assert [doc["text"] for doc in before] == ['{"first":1}']
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert ingest_raw(db, path, "claude") == 2
    after = stored(db, path)
    assert [doc["text"] for doc in after] == ['{"first":1}', '{"second":2}']
    assert after[0]["_key"] == before[0]["_key"]


def test_non_utf8_line_is_stored_as_reversible_base64(tmp_path):
    db = get_database()
    path = tmp_path / "binary.jsonl"
    original = b'{"output":"\xff\x80\x00"}\r'
    path.write_bytes(original + b"\n")
    assert ingest_raw(db, path, "codex") == 1
    [doc] = stored(db, path)
    assert "text" not in doc
    assert base64.b64decode(doc["b64"], validate=True) == original


def test_reingest_and_growth_preserve_keys_and_only_add_new_lines(transcript):
    kind, path, files, ingest = transcript
    db = get_database()
    ingest(db, path, "a27")
    before = {str(file): stored(db, file) for file in files}
    total = db.collection(index.RAW).count()
    ingest(db, path, "a27")
    assert db.collection(index.RAW).count() == total
    extra = {"type": "future_record", "unknown": {"all": "retained"}}
    with path.open("ab") as stream:
        stream.write((json.dumps(extra) + "\n\n").encode())
    ingest(db, path, "a27")
    assert db.collection(index.RAW).count() == total + 2
    for file in files:
        old = before[str(file)]
        new = stored(db, file)
        assert [d["_key"] for d in new[:len(old)]] == [d["_key"] for d in old]
        assert [d["text"] for d in new[:len(old)]] == [d["text"] for d in old]
    assert json.loads(stored(db, path)[-2]["text"]) == extra
    assert stored(db, path)[-1]["text"] == ""


def test_dry_run_neither_creates_nor_changes_raw(transcript, monkeypatch):
    _, path, _, ingest = transcript
    db = get_database()
    # A second private name exercises the absent-collection branch independently.
    name = index.RAW + "_dry"
    monkeypatch.setattr("khipumaq.ingest.RAW", name)
    try:
        assert not db.has_collection(name)
        ingest(db, path, "a27", dry_run=True)
        assert not db.has_collection(name)
        col = db.create_collection(name)
        col.insert({"_key": "sentinel", "text": "unchanged"})
        before = list(col.all())
        ingest(db, path, "a27", dry_run=True)
        assert list(col.all()) == before
    finally:
        db.delete_collection(name, ignore_missing=True)


def test_raw_is_not_linked_after_ensure_index(transcript):
    _, path, _, ingest = transcript
    db = get_database()
    ingest(db, path, "a27")
    index.ensure_index(db)
    index.ensure_index(db)
    links = db.view(index.VIEW)["links"]
    assert index.EPISODES in links
    assert index.RAW not in links
    assert "raw" not in links


def test_tool_only_turn_is_raw_but_not_an_episode(tmp_path):
    db = get_database()
    record = claude_record("assistant", [{"type": "tool_use", "id": "tool",
                           "name": "Read", "input": {"file_path": "fixture"}}],
                           uuid="a27-only-tool-no-episode")
    path = write_records(tmp_path / "tool-only.jsonl", [record])
    before = db.collection(index.EPISODES).count()
    assert ingest_claude_session(db, path, "a27") == 0
    assert db.collection(index.EPISODES).count() == before
    assert not db.collection(index.EPISODES).has(record["uuid"])
    assert json.loads(stored(db, path)[0]["text"]) == record
