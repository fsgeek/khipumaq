"""Gemini CLI sessions (`~/.gemini/tmp/<project>/chats/session-*.json`).

Each session is one JSON document that Gemini CLI rewrites whole as the
session goes on. The project folder is named after the project (with its path
in `.project_root`) or, in older versions, by sha256 of the project's path."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from khipumaq.index import EPISODES
from khipumaq.ingest import ingest_raw, label_from_path, label_from_project_dir, trim_raw


@lru_cache(maxsize=None)
def _hashed_projects(home):
    """sha256(path) -> path for the home directory and the two levels of
    folders under ~/projects, where Gemini CLI would have been started."""
    home = Path(home)
    candidates = [home, *home.glob("projects/*"), *home.glob("projects/*/*")]
    return {hashlib.sha256(str(p).encode()).hexdigest(): p for p in candidates if p.is_dir()}


def gemini_label(project_dir):
    root = project_dir / ".project_root"
    if root.is_file():
        return label_from_path(root.read_text().strip())
    if re.fullmatch(r"[0-9a-f]{64}", project_dir.name):
        path = _hashed_projects(str(Path.home())).get(project_dir.name)
        return label_from_path(path) if path else "gemini"
    return label_from_project_dir(project_dir.name)


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def gemini_session_to_episodes(path, host=None, machine_id=None, canonical=str, label=None):
    """Yield one episode per Gemini message with prose, paired with the user
    message before it. Tool-call-only messages, info and error lines are not
    episodes; Gemini's reasoning (`thoughts`) stays in `raw`."""
    path = Path(path)
    data = json.loads(path.read_text())
    session = data.get("sessionId") or path.stem
    label = label or gemini_label(path.parent.parent)
    prompt, prompt_ts = "", None
    for m in data.get("messages") or []:
        kind = m.get("type")
        if kind == "user":
            prompt, prompt_ts = _text(m.get("content")), m.get("timestamp")
            continue
        text = _text(m.get("content"))
        if kind != "gemini" or not text.strip():
            continue
        yield {
            "_key": f"gemini-{session}-{m.get('id')}",
            "session_id": session,
            "ts": m.get("timestamp"),
            "model": m.get("model"),
            "experiment_label": label,
            "source_file": canonical(path),
            "user_message": prompt,
            "user_ts": prompt_ts,
            "response": text,
            "state": {},
            "state_text": "",
            "activity_log": [],
            "host": host,
            "machine_id": machine_id,
            "gemini": {"tokens": m.get("tokens"), "project_hash": data.get("projectHash")},
        }


def _store_raw(db, path, dry_run, host, machine_id, canonical):
    length = ingest_raw(db, path, "gemini", dry_run=dry_run, host=host, machine_id=machine_id,
                        canonical=canonical, whole=True)
    if not dry_run:
        trim_raw(db, host, canonical(path), length)


def ingest_gemini_file(db, path, dry_run=False, host=None, machine_id=None, canonical=str):
    """Load one session file: every line to `raw` (kind "gemini"), and its
    episodes. Idempotent. Returns the episode count."""
    _store_raw(db, path, dry_run, host, machine_id, canonical)
    col = db.collection(EPISODES)
    count = 0
    for episode in gemini_session_to_episodes(path, host, machine_id, canonical):
        if not dry_run:
            col.insert(episode, overwrite=True)
        count += 1
    return count


def gemini_session_files(root):
    return sorted(Path(root).glob("*/chats/*.json"))


def gemini_sweep(db, root, since=None, host=None, machine_id=None, canonical=str):
    """Ingest every session file modified at or after `since` (epoch seconds;
    None for all), and keep each project's other JSON (logs.json, checkpoints)
    in `raw`. Tool output folders are skipped. Returns (session files, episodes)."""
    if not root or not Path(root).is_dir():
        return (0, 0)
    fresh = lambda f: since is None or f.stat().st_mtime >= since
    for f in sorted(Path(root).glob("*/*.json")):
        if fresh(f):
            _store_raw(db, f, False, host, machine_id, canonical)
    sessions = [f for f in gemini_session_files(root) if fresh(f)]
    episodes = sum(ingest_gemini_file(db, f, host=host, machine_id=machine_id, canonical=canonical)
                   for f in sessions)
    return (len(sessions), episodes)
