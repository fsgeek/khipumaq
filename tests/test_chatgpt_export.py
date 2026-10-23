"""Independent, tests-first contract for spec-chatgpt-import-2026-10-07.md.

There is deliberately no importer to consult. A wrong or silent spec found by
this test author is the point of the split. Only synthetic history lives here.
Raw JSON below means parsed objects re-serialized, NOT original export bytes.

Read-only measurements (old / new; counts only):
* 764 / 943 conversations, 762 shared, 2 / 181 exclusive; all 762 shared
  conversations have different mapping sizes. 28,020 shared messages, 25
  differing contents, 38 differing asset parts, 0 differing string parts,
  0 differing parts after removing asset metadata, 0 differing create_times.
  All 28,020 whole messages differ: conflict counting cannot simply compare
  entire messages without treating export-format differences as conflicts.
* Message create_time: 1,287 / 1 null, 36,717 / 35,800 floats, 0 strings;
  user nulls 331 / 0; assistant text replies with null time 1 / 1.
  update_time: old 36,843 null and 1,161 floats; new 35,801 missing.
  Conversation create_time/update_time: 764 / 943 floats, 0 null/strings.
* Parent cycles 0 / 0; missing current_node targets 0 / 0. Null-message
  roots 764 / 943. Node id differs from mapping key 0 / 0, but message id
  differs from node id 2 / 0: tree traversal must use NODE identities.
* Multimodal dict parts 1,049 / 278. Each export has 116 nonempty dict text
  parts (audio_transcription), 57 in user messages. The spec's blanket rule
  to ignore dict parts therefore loses user prose from episode prompts.
  Tests honor that explicit rule and require the prose to survive in raw.
* user_editable_context: 388 / 0, all 388 user-authored; 386 nonempty
  instruction fields, 388 nonempty profile fields. Raw-only retains these
  words but makes them unavailable to episode search; this is a policy gap.
* User string prompts over 20,000 characters: 33 / 38; maximum 328,155 in
  both. A cap now has a measured use case, but no cap/truncation contract is
  specified: these tests require full text, including the final character.

Explicit resolutions of spec silences (owner should review):
* Raw payload is the WHOLE node, not just message, preserving parent/children
  and arbitrary node fields. Null-message roots have no raw document, as the
  more specific Raw rule says, despite invariant 5 saying "every node".
* Episode key uses message id; parent_uuid is the immediate NODE parent id,
  not the nearest user's id. source_file is stable per conversation across
  shards/exports; its exact spelling is not specified or asserted.
* Missing/None timestamps remain absent or null, never conversation time,
  wall clock, or epoch. Numeric strings (unobserved) get no invented policy.
  No-user fallback stays empty; a dangling parent id remains the source id.
  Absent model/host/machine_id/agent_id/title remain absent or null.
* Multiple string parts join with newline; whitespace-only text is empty.
  weight=0 alone does not exclude a visible reply. A present recipient of
  None/empty is not the same as an absent recipient (only absent means all).
* Conflicts count distinct message CONTENT differences against an earlier
  export, including user asset metadata; other message metadata changes do
  not count. Identical content in another export is not a conflict. A rerun
  adds nothing; whether it repeats the prior conflict count is unspecified.
* Missing current_node yields active_path=False; malformed cycles must
  terminate. Neither malformed shape was observed; tests pin conservative
  behavior without inventing an active branch or a prompt.
* Exclusion is specified only for CLI, so no extra ingest keyword is imposed.
  Blank lines, whitespace, full-line # comments and duplicate ids are tested;
  inline comments, missing files, unknown ids, duplicate shards, export-id
  reuse with changed input and partial-import recovery remain unspecified.
* Provenance fields are listed without host/agent values or source_file
  format. Missing identity fields, invalid dates and equal-time fallback
  tie-breaking have no specified error policy; no arbitrary policy is added.

Plan: pure tree/source/raw tests; measured edge-shape tests; disposable-store
additivity, conflict, opt-in and dry-run tests; CLI shard/exclusion tests.
Imports happen inside tests so every case collects before the intended red
failure (ModuleNotFoundError), without a collection-time import error.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
import json
import socket
from uuid import uuid4

import pytest

from khipumaq import index
from khipumaq.db import get_database
from khipumaq.index import ensure_chat_index


BASE = 1735689600.0  # 2025-01-01T00:00:00Z
ABSENT = object()


def importer():
    return import_module("khipumaq.chatgpt_export")


def node(role, text, parent, offset=0, *, recipient=ABSENT,
         content_type="text", parts=ABSENT, hidden=False, message_id=None):
    nid = str(uuid4())
    message = {
        "id": message_id or nid,
        "author": {"role": role, "name": None, "metadata": {}},
        "content": {"content_type": content_type,
                    "parts": [text] if parts is ABSENT else parts},
        "create_time": BASE + offset,
        "metadata": {"model_slug": "synthetic-model"},
    }
    if recipient is not ABSENT:
        message["recipient"] = recipient
    if hidden:
        message["metadata"]["is_visually_hidden_from_conversation"] = True
    return {"id": nid, "parent": parent, "children": [], "message": message}


def conversation(nodes, *, current=ABSENT):
    root_id = str(uuid4())
    root = {"id": root_id, "parent": None, "children": [], "message": None}
    mapping = {root_id: root, **{n["id"]: deepcopy(n) for n in nodes}}
    for n in nodes:
        parent = mapping[n["id"]]["parent"]
        if parent == "ROOT":
            parent = root_id
            mapping[n["id"]]["parent"] = parent
        if parent in mapping:
            mapping[parent]["children"].append(n["id"])
    cid = str(uuid4())
    return {
        "id": cid, "conversation_id": cid, "title": "Synthetic fixture",
        "create_time": BASE, "update_time": BASE + 100,
        "current_node": nodes[-1]["id"] if current is ABSENT else current,
        "mapping": mapping, "is_archived": False,
        "extra_conversation_field": {"retain": [1, 2]},
    }


def simple_conversation():
    user = node("user", "synthetic question", "ROOT")
    answer = node("assistant", "synthetic answer", user["id"], 1)
    return conversation([user, answer]), user, answer


def forked_conversation():
    user = node("user", "first question", "ROOT")
    inactive = node("assistant", "first branch", user["id"], 40)
    active = node("assistant", "second branch", user["id"], 10)
    followup = node("user", "followup question", active["id"], 20)
    final = node("assistant", "followup answer", followup["id"], 30)
    # Neither mapping order nor timestamp order is a valid parent lookup.
    c = conversation([final, followup, inactive, user, active], current=final["id"])
    return c, (user, inactive, active, followup, final)


def write_export(tmp_path, conversations, name="conversations.json"):
    path = tmp_path / name
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    return path


def payloads(api, c, export_id="synthetic-export"):
    return [json.loads(r["text"]) for r in api.chatgpt_raw_documents(c, export_id)]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def assert_raw_round_trip(api, c, export_id="synthetic-export"):
    expected = [n for n in c["mapping"].values() if n.get("message") is not None]
    expected.append({k: v for k, v in c.items() if k != "mapping"})
    assert Counter(map(canonical, payloads(api, c, export_id))) == Counter(map(canonical, expected))


def disposable_db():
    # conftest redirects both constants and real-driver literal names.
    assert all(getattr(index, name).startswith("test_suite_")
               for name in ("EPISODES", "RAW", "CHAT", "VIEW", "CHAT_VIEW"))
    return get_database()


def documents(db, collection):
    return {d["_key"]: d for d in db.collection(collection).all()} if db.has_collection(collection) else {}


def store_snapshot(db):
    # Include _rev and unrelated records: equality must detect replacement.
    return {name: documents(db, name) for name in (index.EPISODES, index.CHAT, index.RAW)}


def as_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_forks_pair_by_parent_and_preserve_every_reply():
    api = importer()
    c, (user, inactive, active, followup, final) = forked_conversation()
    eps = {e["response"]: e for e in api.chatgpt_episodes(c, "export-a")}
    assert set(eps) == {"first branch", "second branch", "followup answer"}
    for n in (inactive, active):
        assert eps[n["message"]["content"]["parts"][0]]["user_message"] == "first question"
    assert eps["followup answer"]["user_message"] == "followup question"
    assert {e["structure"] for e in eps.values()} == {"linked"}
    assert {e["session_id"] for e in eps.values()} == {c["id"]}
    assert eps["first branch"]["parent_uuid"] == user["id"]
    assert eps["followup answer"]["parent_uuid"] == followup["id"]


def test_active_path_marks_only_current_node_ancestors():
    api = importer()
    c, _ = forked_conversation()
    eps = {e["response"]: e for e in api.chatgpt_episodes(c, "export-a")}
    assert {r: e["active_path"] for r, e in eps.items()} == {
        "first branch": False, "second branch": True, "followup answer": True}


def test_reordering_mapping_and_equal_timestamps_changes_no_linked_episode():
    api = importer()
    c, _ = forked_conversation()
    for n in c["mapping"].values():
        if n["message"]:
            n["message"]["create_time"] = BASE
    reordered = deepcopy(c)
    reordered["mapping"] = dict(reversed(list(c["mapping"].items())))
    assert {e["_key"]: e for e in api.chatgpt_episodes(c, "a")} == {
        e["_key"]: e for e in api.chatgpt_episodes(reordered, "a")}


def test_prompt_walk_skips_assistant_system_tool_and_hidden_user_nodes():
    api = importer()
    user = node("user", "visible prompt", "ROOT")
    system = node("system", "system text", user["id"], 1)
    hidden = node("user", "hidden prompt", system["id"], 2, hidden=True)
    reasoning = node("assistant", "reasoning", hidden["id"], 3, content_type="thoughts")
    tool = node("tool", "tool output", reasoning["id"], 4)
    answer = node("assistant", "answer", tool["id"], 5)
    c = conversation([answer, tool, hidden, reasoning, system, user], current=answer["id"])
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "visible prompt"
    assert e["parent_uuid"] == tool["id"]
    assert e["structure"] == "linked"
    assert as_utc(e["user_ts"]) == datetime.fromtimestamp(BASE, timezone.utc)
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("recipient,kind", [(ABSENT, "reply"), ("all", "reply"), ("bio", "memory-write")],
                         ids=["new-without-recipient", "old-reply", "old-memory-write"])
def test_episode_source_recipient_selects_reply_or_memory_write(recipient, kind):
    api = importer()
    c, user, answer = simple_conversation()
    m = c["mapping"][answer["id"]]["message"]
    if recipient is not ABSENT:
        m["recipient"] = recipient
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["kind"] == kind
    assert e["response"] == "synthetic answer"
    assert e["user_message"] == "synthetic question"
    assert e["experiment_label"] == "chatgpt-chat"
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("recipient", ["python", "web", "web.run", "canmore.create_textdoc",
                                      "canmore.update_textdoc", "unknown-tool", "", None])
def test_tool_canvas_and_other_recipients_make_no_episode(recipient):
    api = importer()
    c, _, answer = simple_conversation()
    c["mapping"][answer["id"]]["message"]["recipient"] = recipient
    assert list(api.chatgpt_episodes(c, "a")) == []
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("content_type", ["thoughts", "reasoning_recap", "code", "multimodal_text",
                                          "execution_output", "tether_browsing_display",
                                          "tether_quote", "system_error", "user_editable_context"])
def test_nontext_assistant_content_is_raw_only(content_type):
    api = importer()
    c, _, answer = simple_conversation()
    c["mapping"][answer["id"]]["message"]["content"]["content_type"] = content_type
    assert list(api.chatgpt_episodes(c, "a")) == []
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("role", ["system", "tool", "user"])
def test_nonassistant_text_is_raw_only(role):
    api = importer()
    c = conversation([node(role, "synthetic text", "ROOT")])
    assert list(api.chatgpt_episodes(c, "a")) == []
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("recipient", [ABSENT, "all", "bio"], ids=["new", "reply", "memory-write"])
def test_hidden_assistant_text_is_raw_only(recipient):
    api = importer()
    c = conversation([node("assistant", "hidden synthetic text", "ROOT", hidden=True, recipient=recipient)])
    assert list(api.chatgpt_episodes(c, "a")) == []
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("parts", [[], [""], ["  \n\t"], [{"text": "asset text"}]])
def test_empty_or_nonstring_response_makes_no_episode(parts):
    api = importer()
    c = conversation([node("assistant", "", "ROOT", parts=parts)])
    assert list(api.chatgpt_episodes(c, "a")) == []
    assert_raw_round_trip(api, c)


def test_weight_zero_alone_does_not_hide_a_reply():
    api = importer()
    c, _, answer = simple_conversation()
    c["mapping"][answer["id"]]["message"].update(weight=0, status="finished_successfully", end_turn=True)
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["response"] == "synthetic answer"


def test_prompt_and_response_join_string_parts_only():
    api = importer()
    c, user, answer = simple_conversation()
    c["mapping"][user["id"]]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": ["typed first", {"content_type": "image_asset_pointer", "asset_pointer": "synthetic-asset"},
                  "typed last"]}
    c["mapping"][answer["id"]]["message"]["content"]["parts"] = ["answer first", {"text": "ignored"}, "answer last"]
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "typed first\ntyped last"
    assert e["response"] == "answer first\nanswer last"
    assert_raw_round_trip(api, c)


def test_audio_transcription_dict_prose_is_part_of_the_prompt():
    # Owner's decision (2026-10-22), overruling the spec's "dict parts contribute
    # nothing": the test author measured 57 user prompts per export that exist
    # only as audio_transcription parts. They are the user's own words, so they
    # are the prompt; other dict parts (images, asset pointers) still are not.
    api = importer()
    c, user, _ = simple_conversation()
    c["mapping"][user["id"]]["message"]["content"] = {
        "content_type": "multimodal_text", "parts": [
            {"content_type": "audio_transcription", "text": "synthetic spoken words"}]}
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "synthetic spoken words"
    assert "synthetic spoken words" in canonical(payloads(api, c))
    assert_raw_round_trip(api, c)


def test_user_editable_context_is_raw_only_and_not_substituted_for_a_prompt():
    api = importer()
    context = node("user", "", "ROOT", hidden=True, content_type="user_editable_context")
    context["message"]["content"] = {"content_type": "user_editable_context",
                                     "user_instructions": "synthetic instructions",
                                     "user_profile": "synthetic profile"}
    user = node("user", "actual prompt", context["id"], 1)
    answer = node("assistant", "answer", user["id"], 2)
    c = conversation([context, user, answer])
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "actual prompt"
    assert "synthetic instructions" not in canonical(e)
    assert "synthetic profile" not in canonical(e)
    assert_raw_round_trip(api, c)


def test_raw_round_trips_whole_nodes_metadata_and_unknown_fields_without_mutation():
    api = importer()
    c, _ = forked_conversation()
    for n in c["mapping"].values():
        n["unknown_node_field"] = {"synthetic": "雪", "values": [None, 3]}
        if n["message"]:
            n["message"]["unknown_message_field"] = [1, {"preserve": True}]
    before = deepcopy(c)
    list(api.chatgpt_episodes(c, "a"))
    assert_raw_round_trip(api, c)
    assert c == before
    raws = list(api.chatgpt_raw_documents(c, "a"))
    assert len(raws) == 6  # five messages + metadata; null root has no raw row
    assert len({r["_key"] for r in raws}) == len(raws)


def test_episode_fields_utc_fractional_time_and_message_identity():
    api = importer()
    c, user, answer = simple_conversation()
    c["mapping"][answer["id"]]["message"]["create_time"] = BASE + 1.25
    (e,) = api.chatgpt_episodes(c, "export-a")
    assert e["_key"] == f"chatgpt-{answer['message']['id']}"
    assert e["session_id"] == c["id"]
    assert e["experiment_label"] == "chatgpt-chat"
    assert e["export_id"] == "export-a"
    assert e["conversation_name"] == "Synthetic fixture"
    assert e["model"] == "synthetic-model"
    assert e["state"] == {} and e["state_text"] == "" and e["activity_log"] == []
    assert isinstance(e["source_file"], str) and e["source_file"]
    assert e["parent_uuid"] == user["id"]
    assert as_utc(e["ts"]) == datetime.fromtimestamp(BASE + 1.25, timezone.utc)
    assert as_utc(e["user_ts"]) == datetime.fromtimestamp(BASE, timezone.utc)
    assert all(e.get(field) is None for field in ("host", "machine_id", "agent_id"))


def test_missing_fields_stay_missing_or_null_instead_of_borrowing_conversation_values():
    api = importer()
    c, user, answer = simple_conversation()
    del c["title"]
    c["default_model_slug"] = "must-not-be-borrowed"
    for n in (user, answer):
        del c["mapping"][n["id"]]["message"]["create_time"]
    c["mapping"][answer["id"]]["message"]["metadata"] = {}
    (e,) = api.chatgpt_episodes(c, "a")
    for field in ("ts", "user_ts", "model", "conversation_name", "host", "machine_id", "agent_id"):
        assert e.get(field) is None
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("null_role", ["user", "assistant", "both"])
def test_measured_null_creation_times_are_not_fabricated(null_role):
    api = importer()
    c, user, answer = simple_conversation()
    for role, n, field in (("user", user, "user_ts"), ("assistant", answer, "ts")):
        if null_role in (role, "both"):
            c["mapping"][n["id"]]["message"]["create_time"] = None
    (e,) = api.chatgpt_episodes(c, "a")
    for role, field in (("user", "user_ts"), ("assistant", "ts")):
        if null_role in (role, "both"):
            assert e.get(field) is None
    assert e["structure"] == "linked"
    assert e["user_message"] == "synthetic question"
    assert_raw_round_trip(api, c)


def test_unlinked_nodes_use_nearest_preceding_user_by_time_and_declare_fallback():
    api = importer()
    u1 = node("user", "first question", None, 0)
    a1 = node("assistant", "first answer", None, 1)
    u2 = node("user", "second question", None, 2)
    a2 = node("assistant", "second answer", None, 3)
    c = conversation([a2, u1, u2, a1])
    eps = {e["response"]: e for e in api.chatgpt_episodes(c, "a")}
    assert {r: e["user_message"] for r, e in eps.items()} == {
        "first answer": "first question", "second answer": "second question"}
    assert {e["structure"] for e in eps.values()} == {"timestamp-order"}
    assert {e.get("parent_uuid") for e in eps.values()} == {None}


def test_dangling_parent_falls_back_without_rewriting_source_parent():
    api = importer()
    c, _, answer = simple_conversation()
    missing_id = str(uuid4())
    c["mapping"][answer["id"]]["parent"] = missing_id
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["structure"] == "timestamp-order"
    assert e["parent_uuid"] == missing_id
    assert e["user_message"] == "synthetic question"


def test_no_prior_user_does_not_borrow_a_future_prompt():
    api = importer()
    answer = node("assistant", "unpaired answer", None, 0)
    future = node("user", "future prompt", None, 1)
    c = conversation([future, answer])
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == ""
    assert e.get("user_ts") is None
    assert e["structure"] == "timestamp-order"


def test_tree_uses_node_ids_even_when_message_ids_differ():
    api = importer()
    user = node("user", "node-linked prompt", "ROOT", message_id=str(uuid4()))
    answer = node("assistant", "node-linked answer", user["id"], 1, message_id=str(uuid4()))
    c = conversation([answer, user], current=answer["id"])
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "node-linked prompt"
    assert e["structure"] == "linked" and e["active_path"] is True
    assert e["parent_uuid"] == user["id"]
    assert e["_key"] == f"chatgpt-{answer['message']['id']}"
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("current", [None, "missing-node-id"])
def test_missing_current_node_does_not_invent_an_active_path(current):
    api = importer()
    c, _ = forked_conversation()
    c["current_node"] = current
    eps = list(api.chatgpt_episodes(c, "a"))
    assert len(eps) == 3 and all(e["active_path"] is False for e in eps)


def test_parent_cycle_terminates_and_does_not_invent_a_user():
    api = importer()
    answer = node("assistant", "cyclic answer", None)
    tool = node("tool", "cyclic tool", answer["id"], 1)
    answer["parent"] = tool["id"]
    c = conversation([answer, tool], current=answer["id"])
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == "" and e.get("user_ts") is None
    assert e["structure"] == "timestamp-order"
    assert_raw_round_trip(api, c)


@pytest.mark.parametrize("length", [20000, 20001, 328155])
def test_large_measured_shape_prompt_is_complete_in_episode_and_raw(length):
    api = importer()
    c, user, _ = simple_conversation()
    body = "x" * (length - 1) + "終"
    c["mapping"][user["id"]]["message"]["content"]["parts"] = [body]
    (e,) = api.chatgpt_episodes(c, "a")
    assert e["user_message"] == body
    assert_raw_round_trip(api, c)


def test_keys_and_source_are_stable_across_exports_but_raw_keys_are_distinct():
    api = importer()
    c, _ = forked_conversation()
    ea = list(api.chatgpt_episodes(c, "export-a"))
    eb = list(api.chatgpt_episodes(c, "export-b"))
    assert {e["_key"] for e in ea} == {e["_key"] for e in eb}
    assert all(e["_key"].startswith("chatgpt-") for e in ea)
    message_ids = {n["message"]["id"] for n in c["mapping"].values() if n["message"]}
    assert {e["_key"] for e in ea}.isdisjoint(message_ids | {f"claude-ai-{m}" for m in message_ids})
    assert {e["source_file"] for e in ea} == {e["source_file"] for e in eb}
    ra = list(api.chatgpt_raw_documents(c, "export-a"))
    rb = list(api.chatgpt_raw_documents(c, "export-b"))
    assert {r["_key"] for r in ra}.isdisjoint(r["_key"] for r in rb)
    assert {r["_key"] for r in ra} == {r["_key"] for r in api.chatgpt_raw_documents(c, "export-a")}
    assert {r["export_id"] for r in ra} == {"export-a"}
    assert {r["export_id"] for r in rb} == {"export-b"}


def test_pure_conversion_never_opens_database_or_network(monkeypatch):
    api = importer()

    def forbidden(*args, **kwargs):
        pytest.fail("pure export conversion attempted database/network access")

    monkeypatch.setattr("khipumaq.db.get_database", forbidden)
    if hasattr(api, "get_database"):
        monkeypatch.setattr(api, "get_database", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    c, _, _ = simple_conversation()
    assert len(list(api.chatgpt_episodes(c, "a"))) == 1
    assert_raw_round_trip(api, c)


def test_ingest_calls_ensure_chat_index_and_leaves_default_store_byte_identical(tmp_path, monkeypatch):
    api = importer()
    db = disposable_db()
    sentinel = {"_key": f"bystander-{uuid4()}", "response": "synthetic existing code episode",
                "user_message": "q", "experiment_label": "claude_code"}
    db.collection(index.EPISODES).insert(sentinel)
    before = canonical(documents(db, index.EPISODES))
    calls = []
    original = index.ensure_chat_index

    def tracked(database):
        calls.append(database)
        return original(database)

    monkeypatch.setattr(index, "ensure_chat_index", tracked)
    if hasattr(api, "ensure_chat_index"):
        monkeypatch.setattr(api, "ensure_chat_index", tracked)
    c, _ = forked_conversation()
    before_chat = documents(db, index.CHAT)
    result = api.ingest_chatgpt(db, [write_export(tmp_path, [c])], "a")
    assert calls and all(database is db for database in calls)
    assert result["episodes"] == {"new": 3, "existing": 0}
    assert result["raw"] == {"new": 6, "existing": 0}
    assert result["conflicts"] == 0
    assert canonical(documents(db, index.EPISODES)) == before
    after = documents(db, index.CHAT)
    assert all(after[k] == v for k, v in before_chat.items())
    new = [v for k, v in after.items() if k not in before_chat]
    assert len(new) == 3 and {e["experiment_label"] for e in new} == {"chatgpt-chat"}


def test_preexisting_episode_raw_and_unrelated_records_keep_their_revisions(tmp_path):
    api = importer()
    db = disposable_db()
    ensure_chat_index(db)
    if not db.has_collection(index.RAW):
        db.create_collection(index.RAW)
    c, _ = forked_conversation()
    episode = next(iter(api.chatgpt_episodes(c, "a")))
    raw = next(iter(api.chatgpt_raw_documents(c, "a")))
    db.collection(index.CHAT).insert({"_key": episode["_key"], "response": "KEEP EPISODE",
                                      "experiment_label": "someone-else"})
    db.collection(index.RAW).insert({"_key": raw["_key"], "text": "KEEP RAW"})
    if not db.has_collection("queries"):
        db.create_collection("queries")
    qkey = f"bystander-{uuid4()}"
    db.collection("queries").insert({"_key": qkey, "text": "KEEP QUERY"})
    before = store_snapshot(db)
    query_before = db.collection("queries").get(qkey)
    result = api.ingest_chatgpt(db, [write_export(tmp_path, [c])], "a")
    assert result["episodes"] == {"new": 2, "existing": 1}
    assert result["raw"] == {"new": 5, "existing": 1}
    for name, docs in before.items():
        after = documents(db, name)
        assert all(after[k] == v for k, v in docs.items())
    assert db.collection("queries").get(qkey) == query_before


def test_identical_input_is_idempotent_including_raw_and_revisions(tmp_path):
    api = importer()
    db = disposable_db()
    c, _ = forked_conversation()
    paths = [write_export(tmp_path, [c])]
    first = api.ingest_chatgpt(db, paths, "a")
    assert first["episodes"] == {"new": 3, "existing": 0}
    before = store_snapshot(db)
    second = api.ingest_chatgpt(db, paths, "a")
    assert second["episodes"] == {"new": 0, "existing": 3}
    assert second["raw"] == {"new": 0, "existing": 6}
    assert second["conflicts"] == 0
    assert store_snapshot(db) == before


@pytest.mark.parametrize("change,expected_conflicts", [
    ("assistant-text", 1), ("asset-metadata", 1), ("message-metadata-only", 0), ("identical", 0)])
def test_two_exports_preserve_both_raw_versions_and_first_episode(tmp_path, change, expected_conflicts):
    api = importer()
    db = disposable_db()
    old, user, answer = simple_conversation()
    old["mapping"][user["id"]]["message"]["content"] = {
        "content_type": "multimodal_text", "parts": ["same user text", {
            "content_type": "image_asset_pointer", "asset_pointer": "synthetic-asset",
            "metadata": {"synthetic_version": 1}}]}
    new = deepcopy(old)
    if change == "assistant-text":
        new["mapping"][answer["id"]]["message"]["content"]["parts"] = ["changed answer"]
    elif change == "asset-metadata":
        new["mapping"][user["id"]]["message"]["content"]["parts"][1]["metadata"]["synthetic_version"] = 2
    elif change == "message-metadata-only":
        new["mapping"][answer["id"]]["message"]["metadata"]["unrelated_export_field"] = True
    old_path = write_export(tmp_path, [old], "old.json")
    new_path = write_export(tmp_path, [new], "new.json")
    api.ingest_chatgpt(db, [old_path], "old")
    before = store_snapshot(db)
    dry = api.ingest_chatgpt(db, [new_path], "new", dry_run=True)
    assert store_snapshot(db) == before
    real = api.ingest_chatgpt(db, [new_path], "new")
    assert real == dry
    assert real["episodes"] == {"new": 0, "existing": 1}
    assert real["raw"] == {"new": 3, "existing": 0}
    assert real["conflicts"] == expected_conflicts
    assert documents(db, index.CHAT) == before[index.CHAT]
    all_raw = documents(db, index.RAW)
    for c, export_id in ((old, "old"), (new, "new")):
        for expected in api.chatgpt_raw_documents(c, export_id):
            actual = all_raw[expected["_key"]]
            assert actual["export_id"] == export_id
            assert json.loads(actual["text"]) == json.loads(expected["text"])
    assert all(all_raw[k] == v for k, v in before[index.RAW].items())
    snapshot = store_snapshot(db)
    rerun = api.ingest_chatgpt(db, [new_path], "new")
    assert rerun["episodes"] == {"new": 0, "existing": 1}
    assert rerun["raw"] == {"new": 0, "existing": 3}
    assert store_snapshot(db) == snapshot


def test_conflict_first_means_import_order_not_export_name(tmp_path):
    api = importer()
    db = disposable_db()
    c, _, answer = simple_conversation()
    later_named = deepcopy(c)
    later_named["mapping"][answer["id"]]["message"]["content"]["parts"] = ["imported first"]
    api.ingest_chatgpt(db, [write_export(tmp_path, [later_named], "new.json")], "z-new")
    key = next(iter(api.chatgpt_episodes(c, "a-old")))["_key"]
    first = db.collection(index.CHAT).get(key)
    result = api.ingest_chatgpt(db, [write_export(tmp_path, [c], "old.json")], "a-old")
    assert result["conflicts"] == 1
    assert db.collection(index.CHAT).get(key) == first
    assert first["response"] == "imported first" and first["export_id"] == "z-new"


def test_multiple_conflicts_count_messages_including_non_episode_nodes(tmp_path):
    api = importer()
    db = disposable_db()
    old, user, answer = simple_conversation()
    new = deepcopy(old)
    for n in (user, answer):
        new["mapping"][n["id"]]["message"]["content"]["parts"] = ["changed synthetic text"]
    api.ingest_chatgpt(db, [write_export(tmp_path, [old], "old.json")], "old")
    result = api.ingest_chatgpt(db, [write_export(tmp_path, [new], "new.json")], "new")
    assert result["conflicts"] == 2


def test_dry_run_matches_real_counts_and_changes_no_documents_or_schema(tmp_path):
    api = importer()
    db = disposable_db()
    c, _ = forked_conversation()
    paths = [write_export(tmp_path, [c])]
    before = store_snapshot(db)
    collections_before = {c["name"] for c in db.collections()}
    views_before = {v["name"] for v in db.views()}
    dry = api.ingest_chatgpt(db, paths, "a", dry_run=True)
    assert dry["episodes"] == {"new": 3, "existing": 0}
    assert dry["raw"] == {"new": 6, "existing": 0}
    assert store_snapshot(db) == before
    assert {c["name"] for c in db.collections()} == collections_before
    assert {v["name"] for v in db.views()} == views_before
    real = api.ingest_chatgpt(db, paths, "a")
    assert dry == real
    before_rerun = store_snapshot(db)
    dry_existing = api.ingest_chatgpt(db, paths, "a", dry_run=True)
    assert dry_existing["episodes"] == {"new": 0, "existing": 3}
    assert dry_existing["raw"] == {"new": 0, "existing": 6}
    assert store_snapshot(db) == before_rerun
    assert api.ingest_chatgpt(db, paths, "a") == dry_existing


def test_shards_are_one_export_and_can_be_rerun_in_reverse_order(tmp_path):
    api = importer()
    db = disposable_db()
    a, _, _ = simple_conversation()
    b, _, _ = simple_conversation()
    paths = [write_export(tmp_path, [a], "conversations-000.json"),
             write_export(tmp_path, [b], "conversations-001.json")]
    result = api.ingest_chatgpt(db, paths, "sharded")
    assert result["episodes"] == {"new": 2, "existing": 0}
    assert result["raw"] == {"new": 6, "existing": 0}
    before = store_snapshot(db)
    rerun = api.ingest_chatgpt(db, list(reversed(paths)), "sharded")
    assert rerun["episodes"] == {"new": 0, "existing": 2}
    assert rerun["raw"] == {"new": 0, "existing": 6}
    assert store_snapshot(db) == before


def test_chat_is_searchable_only_by_explicit_opt_in_including_unfiltered_legacy_query(tmp_path):
    api = importer()
    from khipumaq.search import search

    db = disposable_db()
    c, _, answer = simple_conversation()
    token = "chatgptsynthetic" + uuid4().hex
    c["mapping"][answer["id"]]["message"]["content"]["parts"] = [token]
    default_before = canonical(documents(db, index.EPISODES))
    api.ingest_chatgpt(db, [write_export(tmp_path, [c])], "a")
    key = next(iter(api.chatgpt_episodes(c, "a")))["_key"]
    assert "chatgpt-chat" in index.OPT_IN_LABELS
    assert search(db, token, scope="all")["hits"] == []
    assert {h["key"] for h in search(db, token, scope="chatgpt-chat")["hits"]} == {key}
    assert search(db, token, scope="claude-ai-chat")["hits"] == []
    # No scope predicate: an older client still cannot see the chat collection.
    legacy = list(db.aql.execute(
        "FOR d IN @@view SEARCH ANALYZER(d.response IN TOKENS(@q, 'text_en'), 'text_en') "
        "OPTIONS {waitForSync: true} RETURN d._key",
        bind_vars={"@view": index.VIEW, "q": token}))
    assert legacy == []
    assert canonical(documents(db, index.EPISODES)) == default_before
    # python-arango returns the view's properties as a dict (corrected by the owner;
    # the test author's sandbox could not reach the database to run this).
    assert set(db.view(index.VIEW)["links"]) == {index.EPISODES}
    assert set(db.view(index.CHAT_VIEW)["links"]) == {index.CHAT}


def test_cli_shards_dry_run_real_run_and_idempotent_rerun(tmp_path, capsys):
    importer()
    from khipumaq.cli import main

    db = disposable_db()
    c, _ = forked_conversation()
    other, _, _ = simple_conversation()
    paths = [write_export(tmp_path, [c], "conversations-000.json"),
             write_export(tmp_path, [other], "conversations-001.json")]
    args = ["import-chatgpt", "--export-id", "cli-export", *map(str, paths)]
    before = store_snapshot(db)
    assert main([*args, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert store_snapshot(db) == before
    assert main(args) == 0
    real = json.loads(capsys.readouterr().out)
    assert real == dry
    assert real["episodes"] == {"new": 4, "existing": 0}
    assert real["raw"] == {"new": 9, "existing": 0}
    snapshot = store_snapshot(db)
    assert main(args) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["episodes"] == {"new": 0, "existing": 4}
    assert rerun["raw"] == {"new": 0, "existing": 9}
    assert store_snapshot(db) == snapshot


@pytest.mark.parametrize("exclude_all", [False, True], ids=["some", "all"])
def test_cli_exclusions_comments_whitespace_duplicates_and_no_raw_leak(tmp_path, capsys, exclude_all):
    api = importer()
    from khipumaq.cli import main

    db = disposable_db()
    kept, _, _ = simple_conversation()
    omitted, _, _ = simple_conversation()
    path = write_export(tmp_path, [kept, omitted])
    exclude = tmp_path / "exclude.txt"
    lines = ["# synthetic exclusion file", "", f"  {omitted['id']}  ",
             omitted["id"], "   # indented comment", "   "]
    if exclude_all:
        lines.append(kept["id"])
    exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = ["import-chatgpt", "--export-id", "excluded-export", str(path), "--exclude-file", str(exclude)]
    before = store_snapshot(db)
    assert main([*args, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert store_snapshot(db) == before
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert dry == result
    assert result["excluded"] == (2 if exclude_all else 1)
    assert result["episodes"] == {"new": 0 if exclude_all else 1, "existing": 0}
    assert result["raw"] == {"new": 0 if exclude_all else 3, "existing": 0}
    chat, raw = documents(db, index.CHAT), documents(db, index.RAW)
    omitted_conversations = [omitted, kept] if exclude_all else [omitted]
    for c in omitted_conversations:
        assert all(e.get("session_id") != c["id"] for e in chat.values())
        assert all(r["_key"] not in raw for r in api.chatgpt_raw_documents(c, "excluded-export"))
        assert all(c["id"] not in r.get("text", "") for r in raw.values())
    if not exclude_all:
        assert any(e.get("session_id") == kept["id"] for e in chat.values())
        assert all(r["_key"] in raw for r in api.chatgpt_raw_documents(kept, "excluded-export"))
    assert documents(db, index.EPISODES) == before[index.EPISODES]
