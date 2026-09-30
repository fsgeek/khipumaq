"""One-off: ingest yanantin's archive of Claude Code transcripts (issue #1,
khipumaq amendment A27), so the lines that live ingest dropped, and sessions
already deleted from their machines, reach the store.

The archive is `<root>/<machine>/<project dir>/<session>.jsonl` (subagent
transcripts beside each session, as on a live machine). Each file is recorded
under the path and host it came from, so a file that is also still on its
machine gets the same `raw` keys and episodes as the live sweep, not a copy.
Labels follow A25: a project directory that is a symlink on this machine was
renamed, and its archived sessions take the new name. The old VM has no host
name to recover; it is recorded under its archive name.

Idempotent. --dry-run counts without writing."""
import sys
from pathlib import Path

from khipumaq.db import get_database
from khipumaq.ingest import claude_session_files, ingest_claude_session, label_from_project_dir, read_machine_id

ROOT = Path.home() / ".yanantin" / "corpus" / "claude-projects"
LIVE = Path.home() / ".claude" / "projects"
MACHINES = {
    "wsl-threadripper": ("WAM-THREADRIPPER", read_machine_id()),
    "ubuntu24": ("wam-desktop", "e328729faaa04bc1ba3ea69cbdb21870"),
    "ubuntu-vm-snapshot": ("ubuntu-vm-snapshot", None),
}


def label(project_dir):
    live = LIVE / project_dir
    return label_from_project_dir(live.resolve().name if live.is_symlink() else project_dir)


def main(argv):
    dry_run = "--dry-run" in argv
    db = get_database()
    for machine, (host, machine_id) in MACHINES.items():
        base = ROOT / machine
        files = sorted(base.glob("*/*.jsonl"))
        episodes = lines = 0
        for f in files:
            original = LIVE / f.relative_to(base)
            canonical = lambda p, f=f, original=original: str(original.parent / Path(p).relative_to(f.parent))
            episodes += ingest_claude_session(db, f, label(f.parent.name), dry_run=dry_run,
                                              host=host, machine_id=machine_id, canonical=canonical)
            lines += sum(1 for s in claude_session_files(f) for _ in open(s, "rb"))
        print(f"{machine} as {host}: {len(files)} sessions, {episodes} episodes, ~{lines} lines"
              + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
