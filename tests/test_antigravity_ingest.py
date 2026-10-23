import json
import os
from pathlib import Path
from uuid import uuid4

from khipumaq.antigravity import antigravity_session_to_episodes, antigravity_sweep
from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index


def step(i, kind, source="MODEL", **fields):
    return {"step_index": str(i), "source": source, "type": kind, "status": "DONE",
            "created_at": f"2026-10-21T06:32:{i:02d}Z", **fields}


def make_conversation(root, steps, cid=None, workspace=None, stray_after=None):
    """~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl,
    plus an entry in history.jsonl when the conversation was interactive."""
    cid = cid or str(uuid4())
    logs = root / "brain" / cid / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    lines = [json.dumps(s) for s in steps]
    if stray_after is not None:  # the writer sometimes leaves a fragment after a record
        lines.insert(stray_after + 1, 'METADATA>\\nThe current local time is: 2026-07-03.\\n</X>"}')
    (logs / "transcript_full.jsonl").write_text("\n".join(lines) + "\n\n")
    (logs / "transcript.jsonl").write_text("\n".join(lines) + "\n\n")
    if workspace:
        with open(root / "history.jsonl", "a") as f:
            f.write(json.dumps({"display": "p", "timestamp": 1, "workspace": workspace,
                                "conversationId": cid}) + "\n")
    return root / "brain" / cid


def _raw_lines(db, source):
    return list(db.aql.execute(
        "FOR d IN raw FILTER d.source_file == @s SORT d.line RETURN d", bind_vars={"s": source}))


def test_each_planner_response_with_prose_is_an_episode_paired_with_its_request(tmp_path):
    d = make_conversation(tmp_path, [
        step(0, "USER_INPUT", "USER_EXPLICIT", content="<USER_REQUEST>first</USER_REQUEST>"),
        step(1, "PLANNER_RESPONSE", thinking="private", tool_calls=[{"name": "read_file"}],
             input_tokens="10", output_tokens="2", cache_read_tokens="0"),
        step(2, "VIEW_FILE", content="file text"),
        step(3, "PLANNER_RESPONSE", content="first answer", thinking="private", output_tokens="5"),
        step(4, "USER_INPUT", "USER_EXPLICIT", content="second"),
        step(5, "PLANNER_RESPONSE", content="second answer"),
    ], workspace="/home/u/projects/demo")
    episodes = list(antigravity_session_to_episodes(d, host="h", machine_id="m"))

    assert [(e["user_message"], e["response"]) for e in episodes] == [
        ("<USER_REQUEST>first</USER_REQUEST>", "first answer"),
        ("second", "second answer"),
    ]
    first = episodes[0]
    assert first["_key"] == f"antigravity-{d.name}-3"
    assert first["ts"] == "2026-10-21T06:32:03Z" and first["user_ts"] == "2026-10-21T06:32:00Z"
    assert first["experiment_label"] == "demo"
    assert first["antigravity"]["label_source"] == "history"
    assert first["antigravity"]["output_tokens"] == "5"
    assert first["host"] == "h" and first["machine_id"] == "m"
    assert "private" not in json.dumps(episodes)


def test_a_malformed_line_is_skipped_without_losing_its_neighbours(tmp_path):
    d = make_conversation(tmp_path, [
        step(0, "USER_INPUT", "USER_EXPLICIT", content="ask"),
        step(1, "PLANNER_RESPONSE", content="before the fragment"),
        step(2, "PLANNER_RESPONSE", content="after the fragment"),
    ], stray_after=1)
    assert [e["response"] for e in antigravity_session_to_episodes(d)] == [
        "before the fragment", "after the fragment"]


def test_without_history_the_label_comes_from_paths_the_session_touched(tmp_path):
    touched = [step(i, "VIEW_FILE", content=f"/home/tony/projects/tupay/docs/f{i}.md") for i in range(1, 4)]
    d = make_conversation(tmp_path, [step(0, "USER_INPUT", "USER_EXPLICIT", content="judge"), *touched,
                                     step(9, "PLANNER_RESPONSE", content="verdict")])
    [e] = antigravity_session_to_episodes(d)
    assert e["experiment_label"] == "tupay" and e["antigravity"]["label_source"] == "paths"


def test_a_single_passing_mention_is_not_enough_to_label(tmp_path):
    d = make_conversation(tmp_path, [
        step(0, "USER_INPUT", "USER_EXPLICIT", content="see /home/tony/projects/tlaplus/tla2tools.jar"),
        step(1, "PLANNER_RESPONSE", content="ok"),
    ])
    [e] = antigravity_session_to_episodes(d)
    assert e["experiment_label"] == "antigravity" and e["antigravity"]["label_source"] == "none"


def test_sweep_stores_logs_messages_and_artifacts_but_not_checkpoints_or_tool_outputs(tmp_path):
    db = get_database()
    ensure_index(db)
    d = make_conversation(tmp_path, [step(0, "USER_INPUT", "USER_EXPLICIT", content="p"),
                                     step(1, "PLANNER_RESPONSE", content="a")],
                          workspace="/home/u/projects/demo")
    gen = d / ".system_generated"
    (gen / "messages").mkdir()
    (gen / "messages" / "read.json").write_text('{"read": []}')
    (gen / "steps" / "2").mkdir(parents=True)
    (gen / "steps" / "2" / "output.txt").write_text("tool output\n")
    (d / "analysis_results.md").write_text("# Analysis\n")
    (d / ".git").mkdir()
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (d / "scratch").mkdir()
    (d / "scratch" / "x.py").write_text("print(1)\n")

    assert antigravity_sweep(db, tmp_path, host="h") == (1, 1)
    for path in (gen / "logs" / "transcript_full.jsonl", gen / "logs" / "transcript.jsonl",
                 gen / "messages" / "read.json", d / "analysis_results.md", tmp_path / "history.jsonl"):
        lines = _raw_lines(db, str(path))
        rebuilt = "".join(x["text"] + ("" if x.get("newline") is False else "\n") for x in lines)
        assert rebuilt == path.read_text() and all(x["kind"] == "antigravity" for x in lines), path.name
    for path in (gen / "steps" / "2" / "output.txt", d / ".git" / "HEAD", d / "scratch" / "x.py"):
        assert _raw_lines(db, str(path)) == [], path.name


def test_sweep_is_idempotent_and_reads_only_changed_conversations(tmp_path):
    db = get_database()
    ensure_index(db)
    fresh = make_conversation(tmp_path, [step(0, "USER_INPUT", "USER_EXPLICIT", content="p"),
                                         step(1, "PLANNER_RESPONSE", content="fresh")])
    old = make_conversation(tmp_path, [step(0, "USER_INPUT", "USER_EXPLICIT", content="p"),
                                       step(1, "PLANNER_RESPONSE", content="old")])
    for f in old.rglob("*"):
        os.utime(f, (1_000_000, 1_000_000))

    assert antigravity_sweep(db, tmp_path, since=2_000_000, host="h") == (1, 1)
    assert antigravity_sweep(db, tmp_path, since=2_000_000, host="h") == (1, 1)
    episodes = db.collection(EPISODES)
    assert episodes.has(next(antigravity_session_to_episodes(fresh))["_key"])
    assert not episodes.has(next(antigravity_session_to_episodes(old))["_key"])
    assert antigravity_sweep(db, tmp_path / "missing") == (0, 0)


def test_the_sweep_command_ingests_antigravity_conversations(tmp_path, monkeypatch, capsys):
    from khipumaq import cli

    monkeypatch.setattr(cli, "_sweep_windows", lambda *a: None)
    monkeypatch.setattr(cli, "SWEEP_STATE", tmp_path / "last-sweep")
    d = make_conversation(Path.home() / ".gemini" / "antigravity-cli",
                          [step(0, "USER_INPUT", "USER_EXPLICIT", content="p"),
                           step(1, "PLANNER_RESPONSE", content="swept")])

    assert cli.main(["sweep", "--all"]) == 0
    assert get_database().collection(EPISODES).has(next(antigravity_session_to_episodes(d))["_key"])
    assert "sweep: antigravity: 1 episodes from 1 conversations" in capsys.readouterr().out
