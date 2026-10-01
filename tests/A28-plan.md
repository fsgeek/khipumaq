# A28 test implementation plan

Goal: pin recall's later-session context and install's thread-index upgrade.

Scope: edit tests only, use the suite's disposable database collections and
isolated user directories, commit once with `test: `, and retain the OTS hook's
stamp commit. The user's pre-approved requirements supply the design; this
instance authors tests only.

1. Update `tests/test_cli.py` to expect `ensure_thread_index` exactly once on an
   existing store, no collection recreation or search-view operations, and 0.
2. Add a disposable document factory in `tests/conftest.py`. In
   `tests/test_recall.py`, cover 0 through 3 later turns and overflow, timestamp
   ordering, 200-character text boundaries, foreign/earlier/same-time exclusions,
   the last turn, missing metadata, and unchanged episode fields/storage.
3. In `tests/test_index.py`, call `ensure_thread_index` twice on a fresh private
   collection and verify one persistent index on `["session_id", "ts"]`.
4. In `tests/test_mcp_server.py`, compare real recall and MCP recall results for
   a thread with truncated text and overflow, using isolated query history.
5. Run focused tests and `uv run pytest -q`, review the diff, and commit only
   tests. Leave any source defect failing or explicitly xfailed and report it.
