"""Grok CLI sessions (`~/.grok/sessions/<url-encoded cwd>/<session id>/`).

Episodes come from `updates.jsonl`, the session's append-only stream of
updates from the first prompt on. `chat_history.jsonl` is the model's working
copy: it is rewritten at every context compaction and loses the turns before
it, so it is kept in `raw` but never read for episodes."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from khipumaq.index import EPISODES
from khipumaq.ingest import ingest_raw, label_from_path, trim_raw

# File snapshots taken for rewind (about 1 MB a session) and terminal output
# are large and say little the transcript does not; locks are empty.
_SKIP_FILES = {"rewind_points.jsonl"}
_SKIP_DIRS = {"terminal"}


def _iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(update):
    content = update.get("content")
    return content.get("text", "") if isinstance(content, dict) else ""


def _summary(session_dir):
    try:
        return json.loads((session_dir / "summary.json").read_text())
    except (OSError, ValueError):
        return {}


def grok_session_to_episodes(session_dir, host=None, machine_id=None, canonical=str, label=None):
    """Yield one episode per agent message, paired with the prompt it answers.
    Consecutive chunks of one kind are one message; reasoning is not part of
    the response (it stays in `raw`). Keyed by the session and the line the
    message starts on, which never moves: the file only grows."""
    session_dir = Path(session_dir)
    summary = _summary(session_dir)
    info = summary.get("info") or {}
    session = info.get("id") or session_dir.name
    cwd = info.get("cwd") or unquote(session_dir.parent.name)
    model = summary.get("current_model_id")
    source = canonical(session_dir / "updates.jsonl")
    prompt, prompt_ts, prompt_index = "", None, None
    message, start, start_ts = None, None, None
    previous = None

    def episode():
        return {
            "_key": f"grok-{session}-{start}",
            "session_id": session,
            "ts": start_ts,
            "model": model,
            "experiment_label": label or label_from_path(cwd),
            "source_file": source,
            "user_message": prompt,
            "user_ts": prompt_ts,
            "response": message,
            "state": {},
            "state_text": "",
            "activity_log": [],
            "host": host,
            "machine_id": machine_id,
            "grok": {"cwd": cwd, "prompt_index": prompt_index},
        }

    with open(session_dir / "updates.jsonl") as f:
        for lineno, line in enumerate(f):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            update = (record.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate")
            if kind != "agent_message_chunk" and message is not None:
                if message.strip():
                    yield episode()
                message = None
            if kind == "user_message_chunk":
                if previous == "user_message_chunk":
                    prompt += _text(update)
                else:
                    meta = update.get("_meta") or {}
                    prompt, prompt_ts = _text(update), _iso(record["timestamp"])
                    prompt_index = meta.get("promptIndex")
                    model = meta.get("modelId") or model
            elif kind == "agent_message_chunk":
                if message is None:
                    message, start, start_ts = "", lineno, _iso(record["timestamp"])
                message += _text(update)
            previous = kind
    if message is not None and message.strip():
        yield episode()


def _session_files(session_dir):
    for path in sorted(Path(session_dir).rglob("*")):
        rel = path.relative_to(session_dir)
        if (path.is_file() and path.name not in _SKIP_FILES and path.suffix != ".lock"
                and not _SKIP_DIRS.intersection(rel.parts[:-1])):
            yield path


def ingest_grok_session(db, session_dir, dry_run=False, host=None, machine_id=None, canonical=str, label=None):
    """Load one Grok session: its episodes, and every file but the skipped
    ones line by line into `raw` (kind "grok"). Idempotent. Returns the
    episode count."""
    for path in _session_files(session_dir):
        # Only the .jsonl streams are appended to; everything else is written
        # whole, usually with no final newline, and that last line is content.
        length = ingest_raw(db, path, "grok", dry_run=dry_run, host=host, machine_id=machine_id,
                            canonical=canonical, whole=path.suffix != ".jsonl")
        if not dry_run:
            trim_raw(db, host, canonical(path), length)
    col = db.collection(EPISODES)
    count = 0
    for episode in grok_session_to_episodes(session_dir, host, machine_id, canonical, label):
        if not dry_run:
            col.insert(episode, overwrite=True)
        count += 1
    return count


def grok_session_dirs(root):
    """Every session directory under a Grok sessions tree."""
    return sorted(p.parent for p in Path(root).glob("*/*/updates.jsonl"))


def grok_sweep(db, root, since=None, host=None, machine_id=None, canonical=str):
    """Ingest every session with a file modified at or after `since` (epoch
    seconds; None for all). Returns (sessions, episodes)."""
    if not root or not Path(root).is_dir():
        return (0, 0)
    fresh = [d for d in grok_session_dirs(root)
             if since is None or max(f.stat().st_mtime for f in d.rglob("*") if f.is_file()) >= since]
    episodes = sum(ingest_grok_session(db, d, host=host, machine_id=machine_id, canonical=canonical)
                   for d in fresh)
    return (len(fresh), episodes)
