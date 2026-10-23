"""Antigravity CLI conversations (`~/.gemini/antigravity-cli/brain/<id>/`).

Each conversation keeps a JSONL transcript in
`.system_generated/logs/transcript_full.jsonl`, one step per line (the
`conversations/*.db` beside it hold the same in undocumented protobuf, and
are not read). The transcript does not name the workspace: `history.jsonl`
does for interactive conversations, and headless ones (`agy -p`) are
labelled by the project paths they touched, or not at all."""
import json
import re
from collections import Counter
from pathlib import Path

from khipumaq.index import EPISODES
from khipumaq.ingest import ingest_raw, label_from_path, trim_raw

_PROJECT_PATH = re.compile(
    r"/home/[^/\s\"']+/projects/[A-Za-z0-9._-]+"
    r"|/tmp/claude-\d+/-home-[^-/]+-projects-[A-Za-z0-9._-]+?(?=[/\"' ]|-[0-9a-f]{8}-)")
# A path named once is usually a passing reference (a jar, a doc); a project
# the session worked in is named again and again.
_MIN_PATH_MENTIONS = 3


def _records(path):
    """Steps in a transcript. The writer occasionally emits a malformed line
    (a bad escape, or a stray fragment after a complete record); those are
    skipped here and kept verbatim in `raw`."""
    for line in Path(path).read_text(errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _workspaces(history):
    found = {}
    if Path(history).is_file():
        for line in Path(history).read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("conversationId") and row.get("workspace"):
                found.setdefault(row["conversationId"], row["workspace"])
    return found


def antigravity_label(conversation_dir, transcript, workspaces):
    """(label, how it was found): "history", "paths" or "none"."""
    if conversation_dir.name in workspaces:
        return label_from_path(workspaces[conversation_dir.name]), "history"
    counts = Counter(label_from_path(p) for p in _PROJECT_PATH.findall(transcript.read_text(errors="replace")))
    if counts and counts.most_common(1)[0][1] >= _MIN_PATH_MENTIONS:
        return counts.most_common(1)[0][0], "paths"
    return "antigravity", "none"


def antigravity_session_to_episodes(conversation_dir, host=None, machine_id=None, canonical=str, workspaces=None):
    """Yield one episode per planner response with prose, paired with the
    user input before it. Tool-call-only responses are not episodes; the
    model's `thinking` stays in `raw`."""
    conversation_dir = Path(conversation_dir)
    transcript = conversation_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
    if workspaces is None:
        workspaces = _workspaces(conversation_dir.parent.parent / "history.jsonl")
    label, label_source = antigravity_label(conversation_dir, transcript, workspaces)
    prompt, prompt_ts = "", None
    for s in _records(transcript):
        if s.get("type") == "USER_INPUT":
            prompt, prompt_ts = s.get("content") or "", s.get("created_at")
            continue
        text = s.get("content") or ""
        if s.get("type") != "PLANNER_RESPONSE" or not text.strip():
            continue
        yield {
            "_key": f"antigravity-{conversation_dir.name}-{s.get('step_index')}",
            "session_id": conversation_dir.name,
            "ts": s.get("created_at"),
            "model": None,  # not in the transcript
            "experiment_label": label,
            "source_file": canonical(transcript),
            "user_message": prompt,
            "user_ts": prompt_ts,
            "response": text,
            "state": {},
            "state_text": "",
            "activity_log": [],
            "host": host,
            "machine_id": machine_id,
            "antigravity": {
                "label_source": label_source,
                **{k: s.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens") if k in s},
            },
        }


def _conversation_files(conversation_dir):
    """Logs, messages, and the files the agent wrote at the top level. Not
    the checkpoint .git, per-step tool output, or scratch."""
    gen = conversation_dir / ".system_generated"
    files = [*(gen / "logs").glob("*"), *(gen / "messages").rglob("*"),
             *(p for p in conversation_dir.iterdir() if p.is_file())]
    return sorted(p for p in files if p.is_file())


def _store_raw(db, path, host, machine_id, canonical):
    length = ingest_raw(db, path, "antigravity", host=host, machine_id=machine_id,
                        canonical=canonical, whole=path.suffix != ".jsonl")
    trim_raw(db, host, canonical(path), length)


def antigravity_sweep(db, root, since=None, host=None, machine_id=None, canonical=str):
    """Ingest every conversation with a file modified at or after `since`
    (epoch seconds; None for all). Returns (conversations, episodes)."""
    root = Path(root) if root else None
    if not root or not (root / "brain").is_dir():
        return (0, 0)
    fresh = lambda f: since is None or f.stat().st_mtime >= since
    history = root / "history.jsonl"
    if history.is_file() and fresh(history):
        _store_raw(db, history, host, machine_id, canonical)
    workspaces = _workspaces(history)
    conversations = [p.parents[2] for p in sorted(root.glob("brain/*/.system_generated/logs/transcript_full.jsonl"))
                     if any(fresh(f) for f in _conversation_files(p.parents[2]))]
    col = db.collection(EPISODES)
    count = 0
    for d in conversations:
        for f in _conversation_files(d):
            _store_raw(db, f, host, machine_id, canonical)
        for episode in antigravity_session_to_episodes(d, host, machine_id, canonical, workspaces):
            col.insert(episode, overwrite=True)
            count += 1
    return (len(conversations), count)
