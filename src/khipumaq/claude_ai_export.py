"""Intake for the claude.ai data export (`conversations.json`).

Unlike the Code-session ingesters, this one never replaces: a record whose key
already exists is left exactly as it is, so a re-run adds nothing and nothing
already stored can be changed by it. Keys are namespaced so they cannot meet a
Code-session episode key. Each chat message's parent link is kept as given;
where the export has none, order falls back to time and the episode says so.
"""
import hashlib
import json

from arango.exceptions import CollectionCreateError

from khipumaq.index import EPISODES, RAW
from khipumaq.ingest import _turn_text

HOST = "claude.ai"
LABEL = "claude-ai"
_CHUNK = 1000
PASTE_LIMIT = 20_000  # characters of one paste kept in an episode (median paste ~7.7k)


def _text(message):
    content = message.get("content")
    return _turn_text(content) if content else (message.get("text") or "")


def _prompt_text(message):
    """What the human put in the prompt box: the typed words, then any pasted
    text. claude.ai keeps a long paste as an attachment with no file name;
    it is the user's own words, so it belongs to the prompt. `recall` returns
    an episode whole, so a paste is cut at PASTE_LIMIT with a marker; the
    whole of it stays in raw."""
    parts = [_text(message)] if _text(message).strip() else []
    for a in message["attachments"]:
        body = a.get("extracted_content")
        if a.get("file_name") or not body:
            continue
        if len(body) > PASTE_LIMIT:
            body = (f"{body[:PASTE_LIMIT]}\n[pasted text cut: {PASTE_LIMIT} of {len(body)}"
                    " characters; the whole is in raw]")
        parts.append(f"[pasted text]\n{body}")
    return "\n\n".join(parts)


def _file_names(message):
    """Names of what the human attached, in order, once each. A prompt of only
    files has no words; the names keep it from reading as silence."""
    names = (f.get("file_name") for f in (*message["attachments"], *message["files"]))
    return list(dict.fromkeys(n for n in names if n))


def _raw_key(*parts):
    return hashlib.sha256("\0".join((HOST, *parts)).encode()).hexdigest()[:32]


def _human_ancestor(message, by_id):
    """The nearest human message at or above `message` by parent links, or None
    when the links run out before reaching one."""
    seen = set()
    while message is not None and message["uuid"] not in seen:
        if message["sender"] == "human":
            return message
        seen.add(message["uuid"])
        message = by_id.get(message.get("parent_message_uuid"))
    return None


def claude_ai_episodes(conversation):
    """One episode per assistant message that has prose. The prompt is its
    human ancestor by parent links, so each reply in a regenerated fork pairs
    with the message it answered; with no usable link it is the latest human
    message before it in time, and `structure` says "timestamp-order"."""
    messages = conversation["chat_messages"]
    by_id = {m["uuid"]: m for m in messages}
    ordered = sorted(messages, key=lambda m: m["created_at"])
    last_human = {}
    latest = None
    for m in ordered:
        if m["sender"] == "human":
            latest = m
        last_human[m["uuid"]] = latest
    for m in messages:
        if m["sender"] != "assistant":
            continue
        response = _text(m)
        if not response.strip():
            continue
        prompt = _human_ancestor(by_id.get(m.get("parent_message_uuid")), by_id)
        structure = "linked"
        if prompt is None:
            prompt, structure = last_human[m["uuid"]], "timestamp-order"
        yield {
            "_key": f"claude-ai-{m['uuid']}",
            "session_id": conversation["uuid"],
            "ts": m["created_at"],
            "model": None,
            "experiment_label": LABEL,
            "source_file": f"{HOST}/{conversation['uuid']}",
            "user_message": _prompt_text(prompt) if prompt else "",
            "user_ts": prompt["created_at"] if prompt else None,
            "user_files": _file_names(prompt) if prompt else [],
            "response": response,
            "state": {},
            "state_text": "",
            "activity_log": [],
            "host": HOST,
            "machine_id": None,
            "agent_id": None,
            "conversation_name": conversation.get("name"),
            "parent_uuid": m.get("parent_message_uuid"),
            "structure": structure,
        }


def claude_ai_raw_documents(conversation):
    """The conversation's metadata and every message, each as one JSON text.
    The export is one large JSON file, not lines, so this is the parsed object
    re-serialized, not the original bytes."""
    uuid = conversation["uuid"]
    source = f"{HOST}/{uuid}"
    meta = {k: v for k, v in conversation.items() if k != "chat_messages"}
    yield {
        "_key": _raw_key("conversation", uuid), "kind": "claude_ai_conversation",
        "host": HOST, "machine_id": None, "source_file": source, "line": None,
        "text": json.dumps(meta, ensure_ascii=False),
    }
    for n, m in enumerate(conversation["chat_messages"]):
        yield {
            "_key": _raw_key("message", m["uuid"]), "kind": "claude_ai",
            "host": HOST, "machine_id": None, "source_file": source, "line": n,
            "text": json.dumps(m, ensure_ascii=False),
        }


def _existing(db, name, keys):
    if not db.has_collection(name):
        return set()
    col = db.collection(name)
    found = set()
    for i in range(0, len(keys), _CHUNK):
        found.update(d["_key"] for d in col.get_many(keys[i:i + _CHUNK]))
    return found


def _ensure(db, name):
    if db.has_collection(name):
        return
    try:
        db.create_collection(name)
    except CollectionCreateError:
        if not db.has_collection(name):  # lost a race with another writer
            raise


def _add_new(db, name, docs, dry_run):
    keys = [d["_key"] for d in docs]
    present = _existing(db, name, keys)
    new = [d for d in docs if d["_key"] not in present]
    if new and not dry_run:
        _ensure(db, name)
        batches = db.collection(name).import_bulk(new, on_duplicate="ignore", batch_size=_CHUNK)
        errors = sum(b["errors"] for b in batches)
        if errors:
            raise RuntimeError(f"{name}: {errors} documents failed to import")
    return {"new": len(new), "existing": len(present)}


def ingest_claude_ai(db, path, dry_run=False):
    """Add every conversation in a claude.ai export that is not already stored.
    Returns {"episodes": {"new", "existing"}, "raw": {"new", "existing"}}.
    When dry_run, counts without writing."""
    with open(path, encoding="utf-8") as f:
        conversations = json.load(f)
    episodes, raws = [], []
    for conversation in conversations:
        episodes.extend(claude_ai_episodes(conversation))
        raws.extend(claude_ai_raw_documents(conversation))
    return {
        "episodes": _add_new(db, EPISODES, episodes, dry_run),
        "raw": _add_new(db, RAW, raws, dry_run),
    }
