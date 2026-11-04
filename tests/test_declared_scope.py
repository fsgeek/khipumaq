"""Results declare their own scope and losses (feedback from Claude Desktop,
2026-11-04: `describe` dumped every label, mostly one-off temp folders, and
`search` returned hits without saying what it had searched or left out)."""
import pytest

from khipumaq import mcp_server
from khipumaq.db import get_database
from khipumaq.index import OPT_IN_LABELS, ensure_index
from khipumaq.ingest import label_from_path
from khipumaq.search import search


@pytest.mark.parametrize("path", ["/tmp/tmp89rc2mut", "/tmp/tmpzl-b3-h4", "/tmp/tmp_abc1234/work/sub"])
def test_a_python_temporary_folder_is_one_label_not_one_per_folder(path):
    # 237 episodes in 2026-09/10 came from judges run in fresh tempfile
    # folders, each becoming a "project" of its own in describe.
    assert label_from_path(path) == "tmp"


def test_other_tmp_paths_keep_their_existing_labels():
    session = "85166569-cdb7-47d8-9207-8bbc41c2667a"  # a scratchpad path carries its session uuid
    assert label_from_path(f"/tmp/claude-1000/-home-tony-projects-tupay/{session}/scratchpad/x") == "tupay"


def test_search_declares_what_it_searched_and_what_it_left_out():
    db = get_database()
    ensure_index(db)
    result = search(db, "nothing will match this zzqqxx", since="2026-01-01", until="2026-02-01")
    assert result["searched"] == {"scope": "all", "since": "2026-01-01", "until": "2026-02-01",
                                  "excluded_labels": sorted(OPT_IN_LABELS)}


def test_an_opt_in_search_excludes_nothing_and_says_so():
    db = get_database()
    result = search(db, "anything", scope="claude-ai-chat")
    assert result["searched"]["scope"] == "claude-ai-chat"
    assert result["searched"]["excluded_labels"] == []


def _stats(n):
    labels = [{"label": f"p{i:02d}", "count": 1000 - i} for i in range(n)]
    return {"episodes": sum(l["count"] for l in labels), "oldest": "a", "newest": "b",
            "labels": labels, "hosts": [{"host": "h", "count": 1}]}


def test_describe_tool_shows_the_largest_labels_and_declares_the_rest(monkeypatch):
    monkeypatch.setattr(mcp_server, "_describe", lambda db: _stats(45))
    monkeypatch.setattr(mcp_server, "get_database", lambda: None)
    out = mcp_server.describe()
    assert [l["label"] for l in out["labels"]] == [f"p{i:02d}" for i in range(30)]
    assert out["labels_omitted"] == {"labels": 15, "episodes": sum(1000 - i for i in range(30, 45)),
                                     "how": "describe(all_labels=true) lists every label"}
    full = mcp_server.describe(all_labels=True)
    assert len(full["labels"]) == 45 and "labels_omitted" not in full


def test_describe_tool_with_few_labels_omits_nothing(monkeypatch):
    monkeypatch.setattr(mcp_server, "_describe", lambda db: _stats(5))
    monkeypatch.setattr(mcp_server, "get_database", lambda: None)
    out = mcp_server.describe()
    assert len(out["labels"]) == 5 and "labels_omitted" not in out
