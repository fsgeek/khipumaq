from khipumaq.index import EPISODES

THEN = 3  # later turns shown with a recalled episode

_THEN = """
LET later = (FOR e IN @@col FILTER e.session_id == @session AND e.ts > @ts SORT e.ts RETURN e)
RETURN {
  turns: (FOR e IN later LIMIT @n RETURN {
    key: e._key, ts: e.ts,
    user_message: SUBSTRING(e.user_message, 0, 200),
    response: SUBSTRING(e.response, 0, 200)}),
  more: MAX([LENGTH(later) - @n, 0])
}
"""


def recall(db, key):
    """Return one episode IN FULL by its `_key`, or None if absent. This is the
    second half of the reach: `search` ranks and hands back a 200-char snippet
    plus the episode's `key`; `recall(db, key)` fetches that whole episode so the
    instance never has to leave the memory surface to read what it found. A pure
    point lookup — no view, no BM25.

    `then` holds what the same session said next: up to THEN later turns, both
    sides cut to 200 characters, and how many more follow (A28). Of twelve week-
    old episodes instances opened, four had been revised before they were
    opened, three of them later in the same session ("I stand corrected",
    "withdrawn"), and nothing in the revised episode said so."""
    episode = db.collection(EPISODES).get(key)
    if episode is not None and episode.get("session_id") and episode.get("ts"):
        episode["then"] = next(db.aql.execute(_THEN, bind_vars={
            "@col": EPISODES, "session": episode["session_id"], "ts": episode["ts"], "n": THEN}))
    return episode
