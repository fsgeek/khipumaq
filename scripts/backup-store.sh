#!/bin/bash
# Nightly backup of the episodic store. Runs on a machine other than the one
# that hosts the primary, so losing that machine does not also lose the job.
# Three tiers, each fail-loud:
#   1. arangodump the primary (config/db-config.ini) into $BACKUP_ROOT/<UTC date>
#   2. arangorestore that dump into the local ArangoDB — a warm replica, and the
#      database the primary will one day move to
#   3. restic backup $BACKUP_ROOT to the remote repository (encrypted,
#      deduplicated), then apply the retention policy
# Then verify: the replica's episode count equals the primary's, and its content
# equals the dump's, collection by collection. Local dump
# directories older than 30 days are removed unless dated the 1st of a month.
set -euo pipefail
GIT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
CFG="$GIT_ROOT/config/db-config.ini"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/llm-memory}"
REPLICA_ENDPOINT="${REPLICA_ENDPOINT:-tcp://127.0.0.1:8529}"
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-sftp:activitycontext.work:backups/llm-memory-restic}"
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-$HOME/.config/llm-memory/restic-password}"

read -r HOST PORT DB USER PW < <(python3 -c "
import configparser; c = configparser.ConfigParser(); c.read('$CFG'); d = c['database']
print(d['host'], d['port'], d['database'], d['user_name'], d['user_password'])")
PRIMARY="tcp://$HOST:$PORT"
DAY=$(date -u +%F)
DIR="$BACKUP_ROOT/$DAY"
mkdir -p "$DIR"

echo "[1/4] dump $DB from $PRIMARY -> $DIR"
arangodump --server.endpoint "$PRIMARY" --server.username "$USER" --server.password "$PW" \
  --server.database "$DB" --output-directory "$DIR" --compress-output true --overwrite true \
  --log.level warning

echo "[2/4] restore into replica $REPLICA_ENDPOINT"
arangorestore --server.endpoint "$REPLICA_ENDPOINT" --server.username "$USER" --server.password "$PW" \
  --server.database "$DB" --input-directory "$DIR" --overwrite true --log.level warning

echo "[3/4] verify counts"
PYTHONPATH="$GIT_ROOT" "$GIT_ROOT/.venv/bin/python" - "$HOST" "$PORT" "$DB" "$USER" "$PW" "$REPLICA_ENDPOINT" <<'EOF'
import sys
from arango import ArangoClient
host, port, db, user, pw, replica = sys.argv[1:]
rhost = replica.split("://", 1)[1]
n1 = ArangoClient(hosts=f"http://{host}:{port}").db(db, username=user, password=pw).collection("episodes").count()
n2 = ArangoClient(hosts=f"http://{rhost}").db(db, username=user, password=pw).collection("episodes").count()
print(f"primary {n1} episodes, replica {n2}")
if n1 != n2:
    raise SystemExit("VERIFY FAILED: replica count differs from primary")
EOF

echo "[3b/4] verify content: replica equals the dump, collection by collection"
# The primary keeps taking ingests while this runs, so it is compared by count
# only; what must hold exactly is that the replica is the dump. Both sides are
# hashed the same way: each document without _rev/_id (restore may reissue
# them), as a digest of canonical JSON, in _key order.
"$GIT_ROOT/.venv/bin/python" - "$DIR" "$DB" "$USER" "$PW" "$REPLICA_ENDPOINT" <<'EOF'
import gzip, hashlib, json, sys
from pathlib import Path
from arango import ArangoClient
dump, db, user, pw, replica = sys.argv[1:]
def canon(doc):
    doc = {k: v for k, v in doc.items() if k not in ("_rev", "_id")}
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
files = {}
for f in Path(dump).glob("*.data.json.gz"):
    files.setdefault(f.name.split(".data.json.gz")[0].rsplit("_", 1)[0], []).append(f)
rdb = ArangoClient(hosts=f"http://{replica.split('://', 1)[1]}").db(db, username=user, password=pw)
failed = False
for name, paths in sorted(files.items()):
    docs = {}
    for f in paths:
        with gzip.open(f, "rt") as fh:
            for line in fh:
                if line.strip():
                    d = json.loads(line)
                    docs[d["_key"]] = canon(d)
    h1 = hashlib.sha256("".join(k + docs[k] for k in sorted(docs)).encode()).hexdigest()
    rdocs = {d["_key"]: canon(d) for d in rdb.aql.execute(
        "FOR d IN @@c RETURN d", bind_vars={"@c": name}, batch_size=1000, stream=True)}
    h2 = hashlib.sha256("".join(k + rdocs[k] for k in sorted(rdocs)).encode()).hexdigest()
    ok = h1 == h2
    failed |= not ok
    print(f"{name}: dump {len(docs)} docs {h1[:16]}, replica {len(rdocs)} docs {h2[:16]} {'ok' if ok else 'DIFFERS'}")
    if not ok:
        diff = sorted(k for k in docs.keys() | rdocs.keys() if docs.get(k) != rdocs.get(k))
        print(f"  {len(diff)} keys differ, first: {diff[:5]}")
if failed:
    raise SystemExit("VERIFY FAILED: replica content differs from the dump")
EOF

echo "[4/4] restic -> $RESTIC_REPOSITORY"
restic snapshots --quiet >/dev/null 2>&1 || restic init
restic backup "$BACKUP_ROOT" --tag llm-memory --quiet
restic forget --tag llm-memory --keep-daily 30 --keep-monthly 24 --prune --quiet

# Local retention: 30 days of dailies, plus the 1st of every month.
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +30 ! -name '*-01' \
  -exec rm -rf {} + 2>/dev/null || true
echo "done $DAY: $(du -sh "$DIR" | cut -f1) local, $(ls -d "$BACKUP_ROOT"/20* | wc -l) dumps kept"
