EPISODES = "episodes"
RAW = "raw"  # every source line, verbatim; never in VIEW (A27)
VIEW = "episodes_search"
# The chat exports (claude.ai, ChatGPT; one label per source) are the user's
# gift to the ayllu, and a read cannot be taken back, so whoever wants them has
# to ask. They live in their own collection and view, which the default view
# never contains, so a client running older code cannot return them under
# scope "all" either. A search reaches them only by naming one of these labels.
CHAT = "episodes_chat"
CHAT_VIEW = "episodes_chat_search"
OPT_IN_LABELS = ("claude-ai-chat", "chatgpt-chat")
# The fix: index conversation (both sides) AND flattened state — not state alone.
INDEXED_FIELDS = ["user_message", "response", "state_text"]
ANALYZER = "text_en"  # built-in: tokenize, lowercase, stem, stopwords -> BM25


def _view_properties(collection=None):
    return {
        "links": {
            collection or EPISODES: {
                "fields": {f: {"analyzers": [ANALYZER]} for f in INDEXED_FIELDS}
            }
        }
    }


def ensure_index(db):
    """Idempotently ensure the episodes collection and the conversation-inclusive
    ArangoSearch view exist."""
    if not db.has_collection(EPISODES):
        db.create_collection(EPISODES)
    ensure_thread_index(db)

    props = _view_properties()
    if VIEW in [v["name"] for v in db.views()]:
        db.update_arangosearch_view(VIEW, props)
    else:
        db.create_arangosearch_view(VIEW, properties=props)


def ensure_thread_index(db):
    """Idempotently index episodes by (session_id, ts), so `recall` can show
    what the same session said next (A28)."""
    db.collection(EPISODES).add_index({"type": "persistent", "fields": ["session_id", "ts"]})


def ensure_chat_index(db):
    """Idempotently ensure the opt-in collection, its (session_id, ts) index and
    its own ArangoSearch view exist. Nothing here touches the default store."""
    if not db.has_collection(CHAT):
        db.create_collection(CHAT)
    db.collection(CHAT).add_index({"type": "persistent", "fields": ["session_id", "ts"]})
    props = _view_properties(CHAT)
    if CHAT_VIEW in [v["name"] for v in db.views()]:
        db.update_arangosearch_view(CHAT_VIEW, props)
    else:
        db.create_arangosearch_view(CHAT_VIEW, properties=props)
