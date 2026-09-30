# A27 test implementation plan

Goal: preserve every complete transcript line and keep suite fixtures out of live collections.

Scope: tests only; one `test: ` commit, followed by the existing OTS hook.

1. In `tests/conftest.py`, give episodes, raw, queries and the search view unique
   suite names. Redirect real-driver collection calls and legacy AQL to them;
   drop private collections and the view at session teardown. Keep a read-only
   handle to real raw for the leak regression. Existing fake databases retain
   their names. Use the checkout database configuration.
2. In `tests/test_raw_ingest.py`, ingest Claude and Codex fixtures through their
   public ingest functions. Compare every stored line and parsed JSON object,
   reconstruct bytes, and cover subagents, incomplete lines, binary lines,
   re-ingestion/growth, dry runs, search exclusion and tool-only turns.
3. Run targeted tests, then `uv run pytest -q`. Source defects remain failing or
   receive a strict, explained xfail. Inspect the diff and commit only tests.

The user's pre-approved A27 requirements are the design; execution stays in
this test-author instance to preserve separation from the source author.
