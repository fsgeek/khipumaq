import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from khipumaq.db import get_database
from khipumaq.gemini import gemini_session_files, gemini_session_to_episodes, gemini_sweep, ingest_gemini_file
from khipumaq.index import EPISODES, ensure_index


def user(text, ts, as_list=False):
    content = [{"text": text}] if as_list else text
    return {"id": str(uuid4()), "timestamp": ts, "type": "user", "content": content}


def gemini(text, ts, tool_calls=None):
    m = {"id": str(uuid4()), "timestamp": ts, "type": "gemini", "content": text,
         "thoughts": [{"subject": "s", "description": "private reasoning"}],
         "tokens": {"input": 10, "output": 2, "cached": 0, "thoughts": 1, "tool": 0, "total": 13},
         "model": "gemini-3-flash-preview"}
    if tool_calls is not None:
        m["toolCalls"] = tool_calls
    return m


def make_chat(tmp_root, project_dir, messages, root=None, sid=None):
    """~/.gemini/tmp/<project>/chats/session-*.json, written whole, as Gemini CLI does."""
    sid = sid or str(uuid4())
    d = tmp_root / project_dir
    (d / "chats").mkdir(parents=True, exist_ok=True)
    if root:
        (d / ".project_root").write_text(root)
    f = d / "chats" / f"session-2026-03-20T17-58-{sid[:8]}.json"
    f.write_text(json.dumps({"sessionId": sid, "projectHash": "x", "startTime": "2026-03-20T17:59:01Z",
                             "lastUpdated": "2026-03-20T18:00:00Z", "messages": messages}, indent=2))
    return f


def _raw_lines(db, source):
    return list(db.aql.execute(
        "FOR d IN raw FILTER d.source_file == @s SORT d.line RETURN d", bind_vars={"s": source}))


def _rebuild(lines):
    return "".join(x["text"] + ("" if x.get("newline") is False else "\n") for x in lines)


def test_each_gemini_message_is_an_episode_paired_with_the_prompt_before_it(tmp_path):
    f = make_chat(tmp_path, "demo", [
        {"id": "i", "timestamp": "2026-03-20T17:59:01Z", "type": "info", "content": "Starting OAuth"},
        user("first prompt", "2026-03-20T18:00:00Z", as_list=True),
        gemini("first answer", "2026-03-20T18:00:02Z"),
        gemini("", "2026-03-20T18:00:03Z", tool_calls=[{"name": "read_file"}]),
        gemini("second answer", "2026-03-20T18:00:04Z", tool_calls=[]),
        {"id": "e", "timestamp": "2026-03-20T18:00:05Z", "type": "error", "content": "auth failed"},
        user("second prompt", "2026-03-20T18:01:00Z"),
        gemini("third answer", "2026-03-20T18:01:02Z"),
    ], root="/home/u/projects/demo")
    episodes = list(gemini_session_to_episodes(f, host="h", machine_id="m"))

    assert [(e["user_message"], e["response"]) for e in episodes] == [
        ("first prompt", "first answer"),
        ("first prompt", "second answer"),
        ("second prompt", "third answer"),
    ]
    first = episodes[0]
    data = json.loads(f.read_text())
    assert first["_key"] == f"gemini-{data['sessionId']}-{data['messages'][2]['id']}"
    assert first["experiment_label"] == "demo"
    assert first["model"] == "gemini-3-flash-preview"
    assert first["ts"] == "2026-03-20T18:00:02Z" and first["user_ts"] == "2026-03-20T18:00:00Z"
    assert first["host"] == "h" and first["machine_id"] == "m"
    assert first["gemini"]["tokens"]["total"] == 13
    assert "private reasoning" not in json.dumps(episodes)


def test_a_project_folder_named_by_hash_is_resolved_to_its_project(tmp_path):
    project = Path.home() / "projects" / "demo" / "sub"
    project.mkdir(parents=True)
    digest = hashlib.sha256(str(project).encode()).hexdigest()
    f = make_chat(tmp_path, digest, [user("p", "2026-03-20T18:00:00Z"), gemini("a", "2026-03-20T18:00:01Z")])
    assert [e["experiment_label"] for e in gemini_session_to_episodes(f)] == ["demo"]


def test_an_unresolvable_hash_is_labelled_gemini(tmp_path):
    f = make_chat(tmp_path, "0" * 64, [user("p", "2026-03-20T18:00:00Z"), gemini("a", "2026-03-20T18:00:01Z")])
    assert [e["experiment_label"] for e in gemini_session_to_episodes(f)] == ["gemini"]


def test_raw_keeps_session_and_log_files_whole_and_skips_tool_outputs(tmp_path):
    db = get_database()
    ensure_index(db)
    f = make_chat(tmp_path, "demo", [user("p", "2026-03-20T18:00:00Z"), gemini("a", "2026-03-20T18:00:01Z")],
                  root="/home/u/projects/demo")
    logs = tmp_path / "demo" / "logs.json"
    logs.write_text(json.dumps([{"sessionId": "s", "message": "p"}], indent=2))
    (tmp_path / "demo" / "tool-outputs").mkdir()
    (tmp_path / "demo" / "tool-outputs" / "big.txt").write_text("blob\n")

    assert gemini_sweep(db, tmp_path, host="h") == (1, 1)
    for path in (f, logs):
        assert _rebuild(_raw_lines(db, str(path))) == path.read_text(), path.name
        assert all(x["kind"] == "gemini" for x in _raw_lines(db, str(path)))
    assert _raw_lines(db, str(tmp_path / "demo" / "tool-outputs" / "big.txt")) == []


def test_a_session_file_rewritten_shorter_leaves_no_stale_lines(tmp_path):
    db = get_database()
    ensure_index(db)
    msgs = [user("p", "2026-03-20T18:00:00Z"), gemini("a", "2026-03-20T18:00:01Z"),
            user("q", "2026-03-20T18:00:02Z"), gemini("b", "2026-03-20T18:00:03Z")]
    f = make_chat(tmp_path, "demo", msgs, root="/home/u/projects/demo")
    ingest_gemini_file(db, f, host="h")
    data = json.loads(f.read_text())
    data["messages"] = data["messages"][:2]
    f.write_text(json.dumps(data, indent=2))
    ingest_gemini_file(db, f, host="h")
    assert _rebuild(_raw_lines(db, str(f))) == f.read_text()


def test_ingest_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    db = get_database()
    ensure_index(db)
    episodes = db.collection(EPISODES)
    f = make_chat(tmp_path, "demo", [user("p", "2026-03-20T18:00:00Z"), gemini("a", "2026-03-20T18:00:01Z")])
    dry = make_chat(tmp_path, "dry", [user("p", "2026-03-20T18:00:00Z"), gemini("d", "2026-03-20T18:00:01Z")])

    assert ingest_gemini_file(db, f) == 1
    assert ingest_gemini_file(db, f) == 1
    assert episodes.has(next(gemini_session_to_episodes(f))["_key"])
    assert ingest_gemini_file(db, dry, dry_run=True) == 1
    assert not episodes.has(next(gemini_session_to_episodes(dry))["_key"])
    assert _raw_lines(db, str(dry)) == []


def test_sweep_reads_only_files_changed_since(tmp_path):
    db = get_database()
    ensure_index(db)
    fresh = make_chat(tmp_path, "demo", [user("p", "2026-03-20T18:00:00Z"), gemini("fresh", "2026-03-20T18:00:01Z")])
    old = make_chat(tmp_path, "old", [user("p", "2026-03-20T18:00:00Z"), gemini("old", "2026-03-20T18:00:01Z")])
    os.utime(old, (1_000_000, 1_000_000))

    assert gemini_sweep(db, tmp_path, since=2_000_000, host="h") == (1, 1)
    assert not db.collection(EPISODES).has(next(gemini_session_to_episodes(old))["_key"])
    assert gemini_session_files(tmp_path) == sorted([fresh, old])
    assert gemini_sweep(db, tmp_path / "missing") == (0, 0)


def test_the_sweep_command_ingests_gemini_sessions(tmp_path, monkeypatch, capsys):
    from khipumaq import cli

    monkeypatch.setattr(cli, "_sweep_windows", lambda *a: None)
    monkeypatch.setattr(cli, "SWEEP_STATE", tmp_path / "last-sweep")
    f = make_chat(Path.home() / ".gemini" / "tmp", "demo",
                  [user("p", "2026-03-20T18:00:00Z"), gemini("swept", "2026-03-20T18:00:01Z")])

    assert cli.main(["sweep", "--all"]) == 0
    assert get_database().collection(EPISODES).has(next(gemini_session_to_episodes(f))["_key"])
    assert "sweep: gemini: 1 episodes from 1 files" in capsys.readouterr().out
