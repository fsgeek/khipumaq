from khipumaq.index import ANALYZER, CHAT_VIEW, OPT_IN_LABELS, VIEW

_AQL = """
FOR doc IN @@view
  SEARCH ANALYZER(
    doc.user_message IN TOKENS(@q, @analyzer) OR
    doc.response     IN TOKENS(@q, @analyzer) OR
    doc.state_text   IN TOKENS(@q, @analyzer),
    @analyzer)
  OPTIONS { waitForSync: true }
  __SCOPE_FILTER__
  __SINCE_FILTER__
  __UNTIL_FILTER__
  LET score = BM25(doc)
  SORT score DESC
  LIMIT @limit
  RETURN { _key: doc._key, cycle: doc.cycle, score: score,
           ts: doc.ts, experiment_label: doc.experiment_label,
           source_file: doc.source_file,
           user_message: doc.user_message, response: doc.response,
           state_text: doc.state_text }
"""

_COUNT_ALL = """
FOR doc IN @@view
  SEARCH ANALYZER(__ALL_TOKENS__, @analyzer)
  OPTIONS { waitForSync: true }
  __SCOPE_FILTER__
  __SINCE_FILTER__
  __UNTIL_FILTER__
  COLLECT WITH COUNT INTO n
  RETURN n
"""

_FIELDS = ("response", "user_message", "state_text")


def _matched_field(doc, query):
    """Attribute a hit to the field that best explains it. The search engine
    matched on a stemmed analyzer (text_en), so a literal substring check can
    miss a legitimate match (e.g. query "truncation" -> stem "truncat"). We
    therefore prefer the field with the most literal token overlap, but never
    return None for a doc the engine *did* match: if stemming hides every
    literal token, fall back to the longest non-empty field. This keeps
    matched_field/snippet honest about which prose carried the hit rather than
    silently blanking it (the failure that motivated this project)."""
    tokens = set(query.lower().split())
    best, best_overlap = None, 0
    for field in _FIELDS:
        text = (doc.get(field) or "").lower()
        overlap = sum(1 for t in tokens if t in text)
        if overlap > best_overlap:
            best, best_overlap = field, overlap
    if best is not None:
        return best
    # Stemmed-only match: the engine matched but no literal token survived.
    # Attribute to the longest field that actually has content.
    nonempty = [(len(doc.get(f) or ""), f) for f in _FIELDS if doc.get(f)]
    return max(nonempty)[1] if nonempty else None


def search(db, query, scope="all", limit=10, view=VIEW, since=None, until=None):
    """Search an ArangoSearch view with BM25 ranking. Defaults to the
    conversation-inclusive view (user_message + response + state_text); `view`
    can target another (e.g. a state-only view for controlled comparison).
    `scope` partitions by `experiment_label`: the default "all" searches every
    corpus in the default view, any other value restricts results to episodes
    with that label (e.g. "claude_code" so a live-session query cannot surface
    taste_open episodes). The opt-in labels (`OPT_IN_LABELS`) are not in the
    default view at all: naming one as `scope` searches their own view, and
    nothing else reaches them.
    `since`/`until` bound the episode timestamp (ISO date or datetime strings,
    inclusive/exclusive). Returns {"total": N, "hits": [...]}: `total` is how
    many episodes matched before LIMIT, so a caller can see it is looking at
    ten of eight thousand and narrow, rather than mistake the page for the
    answer (amendment A7). `total_all` counts only episodes holding every
    query word (A26)."""
    if scope in OPT_IN_LABELS and view == VIEW:
        view = CHAT_VIEW
    bind_vars = {"@view": view, "q": query, "analyzer": ANALYZER, "limit": limit}
    aql = _with_filters(_AQL, bind_vars, scope, since, until)
    cursor = db.aql.execute(aql, bind_vars=bind_vars, full_count=True)
    hits = []
    for doc in cursor:
        field = _matched_field(doc, query)
        snippet = (doc.get(field) or "")[:200] if field else ""
        hits.append(
            {
                "key": doc["_key"],
                "cycle": doc["cycle"],
                "score": doc["score"],
                # Provenance (amendment A1): a hit's date, project, and
                # source path are worth more than its snippet.
                "ts": doc.get("ts"),
                "experiment_label": doc.get("experiment_label"),
                "source_file": doc.get("source_file"),
                "matched_field": field,
                "snippet": snippet,
            }
        )
    return {"total": cursor.statistics()["fullCount"],
            "total_all": _count_all(db, query, view, scope, since, until),
            "hits": hits}


def _with_filters(aql, bind_vars, scope, since, until):
    filters = {
        # Opt-in labels live in their own view; the NOT IN is a second layer for one misfiled in this one.
        "__SCOPE_FILTER__": (
            ("FILTER doc.experiment_label NOT IN @opt_in", "opt_in", list(OPT_IN_LABELS))
            if scope == "all" else ("FILTER doc.experiment_label == @scope", "scope", scope)
        ),
        "__SINCE_FILTER__": ("FILTER doc.ts >= @since", "since", since),
        "__UNTIL_FILTER__": ("FILTER doc.ts < @until", "until", until),
    }
    for marker, (clause, name, value) in filters.items():
        if value is None:
            aql = aql.replace(f"  {marker}\n", "")
        else:
            aql = aql.replace(marker, clause)
            bind_vars[name] = value
    return aql


def _count_all(db, query, view, scope, since, until):
    """How many episodes hold every word of the query, each in any field
    (A26). `total` counts episodes holding any word, so for the usual query,
    a handful of keywords, it measures the commonest word: "ubuntu26-test"
    matched every episode that says "test". Same analyzer, so stemming and
    stopwords count as they do for ranking."""
    tokens = list(dict.fromkeys(next(db.aql.execute(
        "RETURN TOKENS(@q, @analyzer)", bind_vars={"q": query, "analyzer": ANALYZER}))))
    if not tokens:
        return 0
    bind_vars = {"@view": view, "analyzer": ANALYZER}
    clauses = []
    for i, token in enumerate(tokens):
        bind_vars[f"t{i}"] = token
        clauses.append("(" + " OR ".join(f"doc.{f} == @t{i}" for f in _FIELDS) + ")")
    aql = _with_filters(_COUNT_ALL.replace("__ALL_TOKENS__", " AND ".join(clauses)), bind_vars, scope, since, until)
    return next(db.aql.execute(aql, bind_vars=bind_vars))
