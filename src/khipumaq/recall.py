from khipumaq.index import CHAT, EPISODES

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
    for collection in (EPISODES, CHAT):  # the opt-in store holds keys only a named search returns
        if collection == CHAT and not db.has_collection(CHAT):
            return None
        episode = db.collection(collection).get(key)
        if episode is not None:
            break
    if episode is not None and episode.get("session_id") and episode.get("ts"):
        episode["then"] = next(db.aql.execute(_THEN, bind_vars={
            "@col": collection, "session": episode["session_id"], "ts": episode["ts"], "n": THEN}))
    return episode
