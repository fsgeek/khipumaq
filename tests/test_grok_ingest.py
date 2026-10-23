import json
import os
from urllib.parse import quote
from uuid import uuid4

from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index
from khipumaq.grok import grok_session_to_episodes, grok_sweep, ingest_grok_session

T0 = 1792647919  # 2026-10-22T05:45:19Z


def _update(update, ts, sid):
    return {"timestamp": str(ts), "method": "_x.ai/session/update",
            "params": {"sessionId": sid, "update": update}}


def user(text, index, model="grok-4.7"):
    return {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text},
            "_meta": {"modelId": model, "promptIndex": index}}


def agent(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def thought(text):
    return {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}


def tool_call():
    return {"sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "read_file"}


def done():
    return {"sessionUpdate": "turn_completed"}


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_session(root, cwd, updates, sid=None):
    """A Grok session directory as ~/.grok/sessions lays it out:
    <url-encoded cwd>/<session id>/, with updates.jsonl one update per line."""
    sid = sid or f"grok-test-{uuid4().hex}"
    d = root / quote(cwd, safe="") / sid
    d.mkdir(parents=True)
    _write_jsonl(d / "updates.jsonl", [_update(u, T0 + i, sid) for i, u in enumerate(updates)])
    (d / "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": cwd},
                                                "current_model_id": "grok-4.7"}, indent=2))
    return d


def _raw_lines(db, source):
    return list(db.aql.execute(
        "FOR d IN raw FILTER d.source_file == @s SORT d.line RETURN d", bind_vars={"s": source}))


def _rebuild(lines):
    return "".join(x["text"] + ("" if x.get("newline") is False else "\n") for x in lines)


def test_each_agent_message_is_an_episode_paired_with_its_prompt(tmp_path):
    d = make_session(tmp_path, "/home/u/projects/demo", [
        user("first prompt", 0), thought("hidden reasoning"), agent("answer one"), tool_call(),
        agent("answer two"), done(),
        user("second prompt", 1), agent("streamed "), agent("reply"), done(),
    ])
    episodes = list(grok_session_to_episodes(d, host="h", machine_id="m"))

    assert [(e["user_message"], e["response"]) for e in episodes] == [
        ("first prompt", "answer one"),
        ("first prompt", "answer two"),
        ("second prompt", "streamed reply"),
    ]
    first = episodes[0]
    assert first["_key"].startswith("grok-")
    assert len({e["_key"] for e in episodes}) == 3
    assert first["experiment_label"] == "demo"
    assert first["model"] == "grok-4.7"
    assert first["ts"] == "2026-10-22T05:45:21Z"
    assert first["user_ts"] == "2026-10-22T05:45:19Z"
    assert first["host"] == "h" and first["machine_id"] == "m"
    assert first["grok"]["cwd"] == "/home/u/projects/demo"
    assert first["grok"]["prompt_index"] == 0
    assert "hidden reasoning" not in json.dumps(episodes)


def test_turns_before_a_compaction_are_kept(tmp_path):
    # chat_history.jsonl is rewritten at compaction and loses the early turns;
    # updates.jsonl keeps every one, so episodes must come from it.
    d = make_session(tmp_path, "/home/u/projects/demo", [
        user("before compaction", 0), agent("early answer"), done(),
        {"sessionUpdate": "auto_compact_completed"},
        user("after compaction", 1), agent("late answer"), done(),
    ])
    _write_jsonl(d / "chat_history.jsonl", [{"type": "user", "content": "after compaction"},
                                            {"type": "assistant", "content": "late answer"}])
    responses = [e["response"] for e in grok_session_to_episodes(d)]
    assert responses == ["early answer", "late answer"]


def test_scratchpad_session_is_labelled_by_the_project_that_launched_it(tmp_path):
    cwd = f"/tmp/claude-1000/-home-tony-projects-tupay/{uuid4()}/scratchpad/rater-D"
    d = make_session(tmp_path, cwd, [user("rate it", 0), agent("rated"), done()])
    assert [e["experiment_label"] for e in grok_session_to_episodes(d)] == ["tupay"]


def test_raw_keeps_session_files_verbatim_but_not_rewind_snapshots_or_terminal_logs(tmp_path):
    db = get_database()
    ensure_index(db)
    d = make_session(tmp_path, "/home/u/projects/demo", [user("p", 0), agent("a"), done()])
    (d / "usage.json").write_text(json.dumps({"session": {"inputTokens": 5, "costUsdTicks": 9}}, indent=2))
    (d / "system_prompt.txt").write_text("You are Grok.\nBe careful.")
    (d / "tool_definitions.json").write_text('[{"name": "read_file"}]')
    _write_jsonl(d / "events.jsonl", [{"ts": "2026-10-22T05:45:19Z", "type": "turn_started"}])
    (d / "compaction").mkdir()
    (d / "compaction" / "segment_000.md").write_text("# segment\n")
    _write_jsonl(d / "rewind_points.jsonl", [{"file_snapshots": {"a": "big"}}])
    (d / "terminal").mkdir()
    (d / "terminal" / "t1.log").write_text("output\n")
    (d / "updates.jsonl.lock").write_text("")

    ingest_grok_session(db, d, host="h", machine_id="m")

    kept = ["updates.jsonl", "summary.json", "usage.json", "system_prompt.txt", "tool_definitions.json",
            "events.jsonl", "compaction/segment_000.md"]
    for name in kept:
        lines = _raw_lines(db, str(d / name))
        assert lines and all(x["kind"] == "grok" for x in lines), name
        assert _rebuild(lines) == (d / name).read_text(), name
    for name in ["rewind_points.jsonl", "terminal/t1.log", "updates.jsonl.lock"]:
        assert _raw_lines(db, str(d / name)) == [], name


def test_a_rewritten_file_that_shrinks_leaves_no_stale_lines_in_raw(tmp_path):
    db = get_database()
    ensure_index(db)
    d = make_session(tmp_path, "/home/u/projects/demo", [user("p", 0), agent("a"), done()])
    history = d / "chat_history.jsonl"
    _write_jsonl(history, [{"n": i} for i in range(5)])
    ingest_grok_session(db, d, host="h")
    _write_jsonl(history, [{"compacted": True}, {"n": 4}])
    ingest_grok_session(db, d, host="h")

    lines = _raw_lines(db, str(history))
    assert _rebuild(lines) == history.read_text()


def test_a_shrinking_whole_file_leaves_no_stale_lines(tmp_path):
    db = get_database()
    ensure_index(db)
    d = make_session(tmp_path, "/home/u/projects/demo", [user("p", 0), agent("a"), done()])
    usage = d / "usage.json"
    usage.write_text(json.dumps({"a": 1, "b": 2, "c": 3}, indent=2))
    ingest_grok_session(db, d, host="h")
    usage.write_text(json.dumps({"a": 1}, indent=2))
    ingest_grok_session(db, d, host="h")
    assert _rebuild(_raw_lines(db, str(usage))) == usage.read_text()


def test_ingest_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    db = get_database()
    ensure_index(db)
    d = make_session(tmp_path, "/home/u/projects/demo", [user("p", 0), agent("a"), agent("b"), done()])
    dry = make_session(tmp_path, "/home/u/projects/demo", [user("p", 0), agent("dry"), done()])
    episodes = db.collection(EPISODES)

    assert ingest_grok_session(db, d) == 1
    assert ingest_grok_session(db, d) == 1
    keys = [e["_key"] for e in grok_session_to_episodes(d)]
    assert all(episodes.has(k) for k in keys)

    assert ingest_grok_session(db, dry, dry_run=True) == 1
    dry_keys = [e["_key"] for e in grok_session_to_episodes(dry)]
    assert not any(episodes.has(k) for k in dry_keys)
    assert _raw_lines(db, str(dry / "updates.jsonl")) == []


def test_sweep_ingests_only_sessions_changed_since(tmp_path):
    db = get_database()
    ensure_index(db)
    root = tmp_path / "grok-sessions"
    fresh = make_session(root, "/home/u/projects/demo", [user("p", 0), agent("fresh"), done()])
    old = make_session(root, "/home/u/projects/old", [user("p", 0), agent("old"), done()])
    for f in old.rglob("*"):
        os.utime(f, (1_000_000, 1_000_000))

    assert grok_sweep(db, root, since=2_000_000, host="h") == (1, 1)
    assert db.collection(EPISODES).has(next(grok_session_to_episodes(fresh))["_key"])
    assert not db.collection(EPISODES).has(next(grok_session_to_episodes(old))["_key"])
    assert grok_sweep(db, tmp_path / "missing") == (0, 0)


def test_session_with_no_messages_yields_no_episodes(tmp_path):
    d = make_session(tmp_path, "/home/u/projects/demo", [{"sessionUpdate": "available_commands_update"}])
    assert list(grok_session_to_episodes(d)) == []


def test_the_sweep_command_ingests_grok_sessions(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    from khipumaq import cli

    monkeypatch.setattr(cli, "_sweep_windows", lambda *a: None)
    monkeypatch.setattr(cli, "SWEEP_STATE", tmp_path / "last-sweep")
    d = make_session(Path.home() / ".grok" / "sessions", "/home/u/projects/demo",
                     [user("p", 0), agent("swept"), done()])

    assert cli.main(["sweep", "--all"]) == 0
    key = next(grok_session_to_episodes(d))["_key"]
    assert get_database().collection(EPISODES).has(key)
    assert "sweep: grok: 1 episodes from 1 sessions" in capsys.readouterr().out
