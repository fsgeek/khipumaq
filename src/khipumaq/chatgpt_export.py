"""ChatGPT data exports (`conversations.json`, or `conversations-NNN.json`
shards of one export), spec docs/spec-chatgpt-import-2026-10-07.md.

Additive only: a document whose key exists is never replaced. Episodes go to
the opt-in chat collection under label "chatgpt-chat". A conversation's
`mapping` is a tree; every reply on every branch is an episode, paired with
its prompt by `parent` links, and only by time when the links are broken.
Raw keeps every node with a message and each conversation's metadata, one
version per export: the parsed object re-serialized, not the original bytes."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from khipumaq import index

LABEL = "chatgpt-chat"
_CHUNK = 1000


def _raw_key(export_id, conversation_id, part):
    return hashlib.sha256(f"chatgpt\0{export_id}\0{conversation_id}\0{part}".encode()).hexdigest()[:32]


def _iso(t):
    if not isinstance(t, (int, float)):
        return None
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _hidden(message):
    return bool((message.get("metadata") or {}).get("is_visually_hidden_from_conversation"))


def _response_text(message):
    parts = (message.get("content") or {}).get("parts") or []
    return "\n".join(p for p in parts if isinstance(p, str))


def _prompt_text(message):
    # The user's spoken words arrive as audio_transcription dicts; images and
    # asset pointers carry no prose.
    parts = (message.get("content") or {}).get("parts") or []
    return "\n".join(
        p if isinstance(p, str) else p.get("text", "")
        for p in parts
        if isinstance(p, str) or (isinstance(p, dict) and p.get("content_type") == "audio_transcription"))


def _kind(message):
    """"reply", "memory-write", or None when the message makes no episode."""
    if (message.get("author") or {}).get("role") != "assistant" or _hidden(message):
        return None
    if (message.get("content") or {}).get("content_type") != "text":
        return None
    if not _response_text(message).strip():
        return None
    if "recipient" not in message or message["recipient"] == "all":
        return "reply"
    return "memory-write" if message["recipient"] == "bio" else None


def _is_prompt(node):
    m = node.get("message")
    return (m is not None and (m.get("author") or {}).get("role") == "user" and not _hidden(m)
            and (m.get("content") or {}).get("content_type") != "user_editable_context")


def _linked_prompt(mapping, node):
    """(prompt node or None, linked?) by walking parent links. Linked when the
    walk reaches a prompt or the conversation's root; a missing parent, a
    message node with no parent, or a cycle means the links can't be used."""
    seen = {node["id"]}
    current = node
    while True:
        parent = current.get("parent")
        if parent is None:
            return None, current.get("message") is None
        if parent in seen or parent not in mapping:
            return None, False
        seen.add(parent)
        current = mapping[parent]
        if _is_prompt(current):
            return current, True


def _prompt_by_time(mapping, node):
    t = (node.get("message") or {}).get("create_time")
    if not isinstance(t, (int, float)):
        return None
    earlier = [n for n in mapping.values() if _is_prompt(n)
               and isinstance(n["message"].get("create_time"), (int, float)) and n["message"]["create_time"] < t]
    return max(earlier, key=lambda n: (n["message"]["create_time"], n["id"]), default=None)


def _active_path(c):
    mapping, current = c.get("mapping") or {}, c.get("current_node")
    path = set()
    while current in mapping and current not in path:
        path.add(current)
        current = mapping[current].get("parent")
    return path


def chatgpt_episodes(conversation, export_id):
    """Yield one episode per reply or memory write in one conversation."""
    mapping = conversation.get("mapping") or {}
    active = _active_path(conversation)
    for node in mapping.values():
        message = node.get("message")
        if message is None:
            continue
        kind = _kind(message)
        if kind is None:
            continue
        prompt, linked = _linked_prompt(mapping, node)
        if not linked:
            prompt = _prompt_by_time(mapping, node)
        pm = prompt["message"] if prompt else {}
        yield {
            "_key": f"chatgpt-{message.get('id')}",
            "session_id": conversation.get("id"),
            "ts": _iso(message.get("create_time")),
            "model": (message.get("metadata") or {}).get("model_slug"),
            "experiment_label": LABEL,
            "source_file": f"chatgpt:{conversation.get('id')}",
            "user_message": _prompt_text(pm) if prompt else "",
            "user_ts": _iso(pm.get("create_time")) if prompt else None,
            "response": _response_text(message),
            "state": {},
            "state_text": "",
            "activity_log": [],
            "host": None,
            "machine_id": None,
            "agent_id": None,
            "conversation_name": conversation.get("title"),
            "parent_uuid": node.get("parent"),
            "structure": "linked" if linked else "timestamp-order",
            "kind": kind,
            "active_path": node["id"] in active,
            "export_id": export_id,
        }


def chatgpt_raw_documents(conversation, export_id):
    """One document per node with a message, and one for the conversation's
    metadata (everything but `mapping`). Keys include the export id, so the
    same message in two exports is two documents."""
    cid = conversation.get("id")
    for node in (conversation.get("mapping") or {}).values():
        message = node.get("message")
        if message is None:
            continue
        yield {
            "_key": _raw_key(export_id, cid, node["id"]),
            "kind": "chatgpt",
            "export_id": export_id,
            "conversation_id": cid,
            "node_id": node["id"],
            "message_id": message.get("id"),
            "content_sha256": hashlib.sha256(_canonical(message.get("content")).encode()).hexdigest(),
            "text": json.dumps(node, ensure_ascii=False),
        }
    yield {
        "_key": _raw_key(export_id, cid, "conversation"),
        "kind": "chatgpt_conversation",
        "export_id": export_id,
        "conversation_id": cid,
        "text": json.dumps({k: v for k, v in conversation.items() if k != "mapping"}, ensure_ascii=False),
    }


def _present(db, collection, keys):
    if not db.has_collection(collection):
        return set()
    col = db.collection(collection)
    found = set()
    for i in range(0, len(keys), _CHUNK):
        found.update(d["_key"] for d in col.get_many(keys[i:i + _CHUNK]))
    return found


def _other_versions(db, export_id):
    """message id -> content hashes stored from other exports."""
    if not db.has_collection(index.RAW):
        return {}
    versions = {}
    for r in db.aql.execute(
            f"FOR r IN {index.RAW} FILTER r.kind == 'chatgpt' AND r.export_id != @e "
            "RETURN [r.message_id, r.content_sha256]", bind_vars={"e": export_id}, stream=True):
        versions.setdefault(r[0], set()).add(r[1])
    return versions


def ingest_chatgpt(db, paths, export_id, dry_run=False, exclude=frozenset()):
    """Add one export (one file or its shards). Returns counts of new and
    existing episodes and raw documents, conflicts (messages whose content
    differs from another export's stored version), and conversations excluded.
    A dry run reads the store and writes nothing."""
    conversations, excluded = [], set()
    for path in paths:
        for c in json.loads(Path(path).read_text(encoding="utf-8")):
            if c.get("id") in exclude:
                excluded.add(c["id"])
            else:
                conversations.append(c)
    episodes = [e for c in conversations for e in chatgpt_episodes(c, export_id)]
    raws = [r for c in conversations for r in chatgpt_raw_documents(c, export_id)]
    have_episodes = _present(db, index.CHAT, [e["_key"] for e in episodes])
    have_raw = _present(db, index.RAW, [r["_key"] for r in raws])
    others = _other_versions(db, export_id)
    conflicts = len({r["message_id"] for r in raws if r["kind"] == "chatgpt"
                     and others.get(r["message_id"]) and r["content_sha256"] not in others[r["message_id"]]})
    new_episodes = [e for e in episodes if e["_key"] not in have_episodes]
    new_raw = [r for r in raws if r["_key"] not in have_raw]
    if not dry_run:
        index.ensure_chat_index(db)
        if not db.has_collection(index.RAW):
            db.create_collection(index.RAW)
        if new_episodes:
            db.collection(index.CHAT).import_bulk(new_episodes, on_duplicate="ignore", batch_size=_CHUNK)
        if new_raw:
            db.collection(index.RAW).import_bulk(new_raw, on_duplicate="ignore", batch_size=_CHUNK)
    return {
        "episodes": {"new": len(new_episodes), "existing": len(episodes) - len(new_episodes)},
        "raw": {"new": len(new_raw), "existing": len(raws) - len(new_raw)},
        "conflicts": conflicts,
        "excluded": len(excluded),
    }
