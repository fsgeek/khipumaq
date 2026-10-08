"""Claude.ai chat export intake: new records only, nothing existing is touched."""
import json
from uuid import uuid4

import pytest

from khipumaq import index
from khipumaq.claude_ai_export import (
    PASTE_LIMIT,
    claude_ai_episodes,
    claude_ai_raw_documents,
    ingest_claude_ai,
)
from khipumaq.db import get_database
from khipumaq.index import ensure_chat_index, ensure_index

NULL_PARENT = "00000000-0000-4000-8000-000000000000"


def msg(sender, text, parent, ts, uuid=None, content=None):
    uuid = uuid or str(uuid4())
    blocks = content if content is not None else [{"type": "text", "text": text}]
    return {
        "uuid": uuid, "text": text, "sender": sender, "created_at": ts,
        "updated_at": ts, "content": blocks, "attachments": [], "files": [],
        "parent_message_uuid": parent,
    }


def conv(messages, name="Fixture chat", uuid=None):
    return {
        "uuid": uuid or str(uuid4()), "name": name, "summary": "",
        "created_at": messages[0]["created_at"], "updated_at": messages[-1]["created_at"],
        "account": {"uuid": "acct"}, "chat_messages": messages,
    }


def write_export(tmp_path, conversations):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False))
    return path


def forked_conversation():
    """One human message with two assistant replies (a regeneration), then a
    follow-up that continues only the second reply."""
    h1 = msg("human", "Why is the sky blue?", NULL_PARENT, "2025-01-01T10:00:00.000000Z")
    a1 = msg("assistant", "Rayleigh scattering.", h1["uuid"], "2025-01-01T10:00:01.000000Z")
    a2 = msg("assistant", "Short wavelengths scatter more.", h1["uuid"], "2025-01-01T10:00:09.000000Z")
    h2 = msg("human", "And sunsets?", a2["uuid"], "2025-01-01T10:01:00.000000Z")
    a3 = msg("assistant", "Longer path, so red survives.", h2["uuid"], "2025-01-01T10:01:02.000000Z")
    return conv([h1, a1, a2, h2, a3]), (h1, a1, a2, h2, a3)


def episodes_by_key(db):
    """The default store, which an import must never touch."""
    return {d["_key"]: d for d in db.collection(index.EPISODES).all()}


def chat_by_key(db):
    """The opt-in store, where chat episodes go."""
    return {d["_key"]: d for d in db.collection(index.CHAT).all()} if db.has_collection(index.CHAT) else {}


def test_each_reply_in_a_fork_pairs_with_its_own_parent_not_the_preceding_message():
    c, (h1, a1, a2, h2, a3) = forked_conversation()
    eps = {e["response"]: e for e in claude_ai_episodes(c)}
    assert set(eps) == {a1["text"], a2["text"], a3["text"]}
    # a1 follows a2's sibling in time order only if pairing used order, not links
    assert eps[a1["text"]]["user_message"] == "Why is the sky blue?"
    assert eps[a2["text"]]["user_message"] == "Why is the sky blue?"
    assert eps[a3["text"]]["user_message"] == "And sunsets?"
    assert all(e["session_id"] == c["uuid"] for e in eps.values())
    assert all(e["structure"] == "linked" for e in eps.values())


def test_conversation_without_parent_links_falls_back_to_time_order_and_says_so():
    h1 = msg("human", "first question", None, "2024-03-01T10:00:00.000000Z")
    a1 = msg("assistant", "first answer", None, "2024-03-01T10:00:05.000000Z")
    h2 = msg("human", "second question", None, "2024-03-01T10:01:00.000000Z")
    a2 = msg("assistant", "second answer", None, "2024-03-01T10:01:05.000000Z")
    eps = list(claude_ai_episodes(conv([h1, a1, h2, a2])))
    assert [(e["user_message"], e["response"]) for e in eps] == [
        ("first question", "first answer"), ("second question", "second answer")]
    assert {e["structure"] for e in eps} == {"timestamp-order"}
    assert {e["parent_uuid"] for e in eps} == {None}  # no parent is invented


def test_only_text_blocks_are_the_response_and_empty_turns_make_no_episode_but_stay_in_raw():
    h = msg("human", "run it", NULL_PARENT, "2025-02-01T10:00:00.000000Z")
    thinking_only = msg("assistant", "", h["uuid"], "2025-02-01T10:00:01.000000Z", content=[
        {"type": "thinking", "thinking": "private reasoning"},
        {"type": "tool_use", "name": "x", "input": {}}])
    answer = msg("assistant", "done", h["uuid"], "2025-02-01T10:00:02.000000Z", content=[
        {"type": "thinking", "thinking": "more reasoning"},
        {"type": "text", "text": "done"}])
    c = conv([h, thinking_only, answer])
    eps = list(claude_ai_episodes(c))
    assert [e["response"] for e in eps] == ["done"]
    assert "reasoning" not in json.dumps(eps)
    raws = list(claude_ai_raw_documents(c))
    assert len(raws) == 1 + 3  # conversation metadata plus every message
    assert any("private reasoning" in r.get("text", "") for r in raws)


def test_raw_documents_round_trip_every_message_and_the_conversation_metadata():
    c, _ = forked_conversation()
    raws = list(claude_ai_raw_documents(c))
    by_kind = {}
    for r in raws:
        by_kind.setdefault(r["kind"], []).append(r)
    assert len(by_kind["claude_ai"]) == len(c["chat_messages"])
    assert [json.loads(r["text"]) for r in sorted(by_kind["claude_ai"], key=lambda r: r["line"])] \
        == c["chat_messages"]
    (meta,) = by_kind["claude_ai_conversation"]
    assert json.loads(meta["text"]) == {k: v for k, v in c.items() if k != "chat_messages"}
    assert len({r["_key"] for r in raws}) == len(raws)


def test_a_prompt_that_was_only_files_names_them_instead_of_reading_as_silence():
    h = msg("human", "", NULL_PARENT, "2025-03-01T10:00:00.000000Z")
    h["attachments"] = [{"file_name": "draft.pdf", "file_size": 1, "file_type": "pdf",
                         "extracted_content": "the full text of the draft"}]
    h["files"] = [{"file_name": "draft.pdf", "file_uuid": "u1"},
                  {"file_name": "figure.png", "file_uuid": "u2"}]
    a = msg("assistant", "I read both.", h["uuid"], "2025-03-01T10:00:05.000000Z")
    (episode,) = claude_ai_episodes(conv([h, a]))
    assert episode["user_message"] == ""
    assert episode["user_files"] == ["draft.pdf", "figure.png"]
    assert "full text of the draft" not in json.dumps(episode)  # names only, not content


def pasted(text):
    """claude.ai stores a long paste as an attachment with no file name."""
    return {"file_name": "", "file_size": len(text), "file_type": "txt", "extracted_content": text}


def test_pasted_text_is_part_of_the_prompt_and_follows_the_typed_words():
    h = msg("human", "Please summarize:", NULL_PARENT, "2025-04-01T10:00:00.000000Z")
    h["attachments"] = [pasted("the pasted body")]
    a = msg("assistant", "Summary.", h["uuid"], "2025-04-01T10:00:05.000000Z")
    (episode,) = claude_ai_episodes(conv([h, a]))
    assert episode["user_message"] == "Please summarize:\n\n[pasted text]\nthe pasted body"
    assert episode["user_files"] == []  # a paste has no name to list


def test_a_prompt_that_is_only_a_paste_is_the_paste():
    h = msg("human", "", NULL_PARENT, "2025-04-01T10:00:00.000000Z")
    h["attachments"] = [pasted("only this")]
    a = msg("assistant", "ok", h["uuid"], "2025-04-01T10:00:05.000000Z")
    (episode,) = claude_ai_episodes(conv([h, a]))
    assert episode["user_message"] == "[pasted text]\nonly this"


def test_a_huge_paste_is_cut_with_a_marker_that_says_how_much_and_where_the_rest_is():
    body = "x" * (PASTE_LIMIT + 500)
    h = msg("human", "", NULL_PARENT, "2025-04-01T10:00:00.000000Z")
    h["attachments"] = [pasted(body)]
    a = msg("assistant", "ok", h["uuid"], "2025-04-01T10:00:05.000000Z")
    (episode,) = claude_ai_episodes(conv([h, a]))
    kept = episode["user_message"]
    assert "x" * PASTE_LIMIT in kept and "x" * (PASTE_LIMIT + 1) not in kept
    assert f"[pasted text cut: {PASTE_LIMIT} of {len(body)} characters; the whole is in raw]" in kept
    raw = [r for r in claude_ai_raw_documents(conv([h, a])) if r["kind"] == "claude_ai"]
    assert body in "".join(r["text"] for r in raw)  # nothing is lost, only not indexed


def test_keys_are_namespaced_so_they_cannot_collide_with_code_session_keys():
    c, messages = forked_conversation()
    for e in claude_ai_episodes(c):
        assert e["_key"].startswith("claude-ai-")
        assert e["_key"] not in {m["uuid"] for m in messages}


def test_existing_documents_are_never_overwritten(tmp_path):
    db = get_database()
    ensure_chat_index(db)
    c, (h1, a1, *_rest) = forked_conversation()
    path = write_export(tmp_path, [c])
    key = next(e["_key"] for e in claude_ai_episodes(c) if e["response"] == a1["text"])
    sentinel = {"_key": key, "response": "PRE-EXISTING, DO NOT TOUCH", "user_message": "x",
                "experiment_label": "someone-else"}
    db.collection(index.CHAT).insert(sentinel)
    raw_key = next(r["_key"] for r in claude_ai_raw_documents(c) if r["kind"] == "claude_ai")
    db.create_collection(index.RAW) if not db.has_collection(index.RAW) else None
    db.collection(index.RAW).insert({"_key": raw_key, "text": "PRE-EXISTING RAW"})

    result = ingest_claude_ai(db, path)

    assert db.collection(index.CHAT).get(key)["response"] == "PRE-EXISTING, DO NOT TOUCH"
    assert db.collection(index.RAW).get(raw_key)["text"] == "PRE-EXISTING RAW"
    assert result["episodes"] == {"new": 2, "existing": 1}


def test_unrelated_existing_episodes_are_untouched_and_rerun_adds_nothing(tmp_path):
    db = get_database()
    ensure_index(db)
    bystander = {"_key": f"bystander-{uuid4()}", "response": "mine", "user_message": "q",
                 "experiment_label": "wamason-com", "ts": "2026-01-01T00:00:00Z"}
    db.collection(index.EPISODES).insert(bystander)
    default_before = episodes_by_key(db)
    chat_before = chat_by_key(db)
    path = write_export(tmp_path, [forked_conversation()[0]])

    first = ingest_claude_ai(db, path)
    assert first["episodes"] == {"new": 3, "existing": 0}
    assert episodes_by_key(db) == default_before  # the default store is not touched at all
    snapshot = chat_by_key(db)
    assert len(snapshot) == len(chat_before) + 3
    assert all(snapshot[k] == v for k, v in chat_before.items())
    raw_snapshot = {d["_key"]: d for d in db.collection(index.RAW).all()}

    second = ingest_claude_ai(db, path)

    assert second["episodes"] == {"new": 0, "existing": 3}
    assert second["raw"]["new"] == 0
    assert chat_by_key(db) == snapshot
    assert {d["_key"]: d for d in db.collection(index.RAW).all()} == raw_snapshot
    assert db.collection(index.EPISODES).get(bystander["_key"])["response"] == "mine"


def test_dry_run_reports_counts_and_writes_nothing(tmp_path):
    db = get_database()
    ensure_chat_index(db)
    before = chat_by_key(db)
    raw_before = db.collection(index.RAW).count() if db.has_collection(index.RAW) else 0
    result = ingest_claude_ai(db, write_export(tmp_path, [forked_conversation()[0]]), dry_run=True)
    assert result["episodes"] == {"new": 3, "existing": 0}
    assert result["raw"]["new"] == 5 + 1
    assert chat_by_key(db) == before
    assert (db.collection(index.RAW).count() if db.has_collection(index.RAW) else 0) == raw_before


def test_cli_dry_run_prints_counts_and_a_real_run_then_a_rerun_adds_nothing(tmp_path, capsys):
    from khipumaq.cli import main

    path = write_export(tmp_path, [forked_conversation()[0]])
    before = chat_by_key(get_database())

    assert main(["import-claude-ai", str(path), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["episodes"] == {"new": 3, "existing": 0}
    assert chat_by_key(get_database()) == before

    assert main(["import-claude-ai", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["episodes"] == {"new": 3, "existing": 0}
    assert len(chat_by_key(get_database())) == len(before) + 3

    assert main(["import-claude-ai", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["episodes"] == {"new": 0, "existing": 3}
    assert len(chat_by_key(get_database())) == len(before) + 3


def test_excluded_conversations_are_left_out_entirely_and_the_result_says_so(tmp_path, capsys):
    from khipumaq.cli import main

    kept, _ = forked_conversation()
    left_out, _ = forked_conversation()
    path = write_export(tmp_path, [kept, left_out])
    exclude = tmp_path / "exclude.txt"
    exclude.write_text(f"# client work, declared on the plaza\n{left_out['uuid']}\n\n")
    db = get_database()
    ensure_chat_index(db)
    before = len(chat_by_key(db))

    assert main(["import-claude-ai", str(path), "--exclude-file", str(exclude), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert main(["import-claude-ai", str(path), "--exclude-file", str(exclude)]) == 0
    real = json.loads(capsys.readouterr().out)

    assert dry == real
    assert real["excluded"] == 1
    assert real["episodes"] == {"new": 3, "existing": 0}  # one conversation, not two
    stored = chat_by_key(db)
    assert len(stored) == before + 3
    sessions = {e.get("session_id") for e in stored.values()}
    assert kept["uuid"] in sessions and left_out["uuid"] not in sessions
    raws = {d["source_file"] for d in db.collection(index.RAW).all() if d.get("kind", "").startswith("claude_ai")}
    assert f"claude.ai/{left_out['uuid']}" not in raws and f"claude.ai/{kept['uuid']}" in raws


def test_recall_by_key_finds_a_chat_episode_and_shows_what_the_session_said_next(tmp_path):
    from khipumaq.recall import recall

    db = get_database()
    c, (h1, a1, a2, h2, a3) = forked_conversation()
    ingest_claude_ai(db, write_export(tmp_path, [c]))

    episode = recall(db, f"claude-ai-{a1['uuid']}")

    assert episode["response"] == "Rayleigh scattering."
    assert episode["session_id"] == c["uuid"]
    assert [t["key"] for t in episode["then"]["turns"]] == [f"claude-ai-{a2['uuid']}", f"claude-ai-{a3['uuid']}"]
    assert recall(db, f"claude-ai-{uuid4()}") is None  # absent in both stores
