"""The local queue: what keeps a shift's production when the internet does not.

A factory's internet goes down. Not as an edge case — as a Tuesday. If the
gateway holds readings only in memory and only while connected, then every
outage is a hole in the plant's history, and the first thing a pilot customer
notices is that AMP's numbers disagree with the numbers on the machine.

So readings go to DISK FIRST and are removed only once AMP has them. SQLite
because it is in the Python standard library, survives a power cut mid-write,
and a plant PC cannot be assumed to have anything else installed.

THREE RULES THIS FILE KEEPS, each the answer to a way queues lie:

  1. A BUFFERED READING KEEPS ITS OWN TIMESTAMP AND SAYS IT WAS BUFFERED.
     Uploading a 3-hour-old value must not make it current. It happened when it
     happened; `buffered_for` says how long it waited, and the AMP side uses
     that to keep it out of "live" without throwing away the history.

  2. A DROP IS RECORDED, NEVER SILENT. The queue is bounded, because a gateway
     with a full disk is a gateway that has stopped. When it overflows, the
     OLDEST records go and `dropped` counts them — and the next records carry a
     `gap` marker so AMP knows the stream is not continuous rather than quietly
     drawing a straight line across the hole.

  3. A RECORD IS REMOVED ONLY AFTER AMP HAS IT. Delivery is therefore
     at-least-once, so every record carries a `record_id` the AMP side can
     deduplicate on. The alternative — delete on send — is at-most-once, which
     for a production counter means losing parts on every flaky connection.
"""
import json
import os
import sqlite3
import time
import uuid

DEFAULT_MAX_RECORDS = 200_000       # ~50MB of typical payloads; days of a pilot


class Buffer:
    """A durable FIFO of outbound payloads."""

    def __init__(self, path, max_records=DEFAULT_MAX_RECORDS):
        self.path = path
        self.max_records = int(max_records)
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # WAL so a reader (the health screen) never blocks the writer (the poll
        # loop), and so an abrupt power loss leaves a recoverable file.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS outbound (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id   TEXT    NOT NULL UNIQUE,
                queued_at   REAL    NOT NULL,
                payload     TEXT    NOT NULL
            )""")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_outbound_id ON outbound(id)")

    # ── writing ─────────────────────────────────────────────────────
    def put(self, payload: dict, now=None) -> str:
        """Queue one payload. Returns its record_id. Never raises on overflow."""
        now = time.time() if now is None else now
        record_id = payload.get("record_id") or uuid.uuid4().hex
        payload = dict(payload)
        payload["record_id"] = record_id
        if self._gap_pending():
            # The last thing that happened to this queue was a drop. Say so ON
            # the next record rather than in a log nobody reads.
            payload["gap_before"] = True
            self._set_meta("gap_pending", "")
        self._db.execute("INSERT INTO outbound (record_id, queued_at, payload) VALUES (?, ?, ?)",
                         (record_id, now, json.dumps(payload, default=str)))
        self._enforce_bound()
        return record_id

    def _enforce_bound(self):
        over = self.depth() - self.max_records
        if over <= 0:
            return
        # Oldest first. Losing the oldest keeps the gateway useful (recent state
        # is what an operator is looking at); losing the newest would mean a
        # full queue permanently shows a stale machine.
        self._db.execute(
            "DELETE FROM outbound WHERE id IN (SELECT id FROM outbound ORDER BY id LIMIT ?)",
            (over,))
        self._bump_meta("dropped", over)
        self._set_meta("gap_pending", "1")
        self._set_meta("last_drop_at", str(time.time()))

    # ── reading ─────────────────────────────────────────────────────
    def peek(self, limit=100):
        """The next records to publish, oldest first. Does NOT remove them."""
        rows = self._db.execute(
            "SELECT id, record_id, queued_at, payload FROM outbound ORDER BY id LIMIT ?",
            (limit,)).fetchall()
        out = []
        for row_id, record_id, queued_at, raw in rows:
            try:
                payload = json.loads(raw)
            except ValueError:
                # A corrupt row cannot be published and must not wedge the queue
                # forever behind it. Dropped, counted, and the gap flagged.
                self._db.execute("DELETE FROM outbound WHERE id = ?", (row_id,))
                self._bump_meta("corrupt", 1)
                self._set_meta("gap_pending", "1")
                continue
            out.append((row_id, record_id, queued_at, payload))
        return out

    def ack(self, row_ids):
        """Remove records AMP has confirmed. The only path that deletes on success."""
        if not row_ids:
            return 0
        marks = ",".join("?" for _ in row_ids)
        cur = self._db.execute(f"DELETE FROM outbound WHERE id IN ({marks})", list(row_ids))
        self._set_meta("last_publish_at", str(time.time()))
        return cur.rowcount

    # ── what the health screen asks ─────────────────────────────────
    def depth(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbound").fetchone()[0]

    def oldest_queued_at(self):
        """When the oldest waiting record was taken. None when the queue is empty.

        None here means EMPTY, which is the healthy state — the health report
        must not print it as 0, which would read as "1970".
        """
        row = self._db.execute("SELECT queued_at FROM outbound ORDER BY id LIMIT 1").fetchone()
        return row[0] if row else None

    def stats(self, now=None):
        now = time.time() if now is None else now
        oldest = self.oldest_queued_at()
        return {
            "queued": self.depth(),
            "max_records": self.max_records,
            "oldest_queued_at": oldest,
            "oldest_age_s": (now - oldest) if oldest is not None else None,
            "dropped": int(self._get_meta("dropped") or 0),
            "corrupt": int(self._get_meta("corrupt") or 0),
            "last_publish_at": float(self._get_meta("last_publish_at") or 0) or None,
            "last_drop_at": float(self._get_meta("last_drop_at") or 0) or None,
        }

    # ── meta ────────────────────────────────────────────────────────
    def _get_meta(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def _bump_meta(self, key, by):
        self._set_meta(key, int(self._get_meta(key) or 0) + int(by))

    def _gap_pending(self):
        return bool(self._get_meta("gap_pending"))

    def close(self):
        try:
            self._db.close()
        except Exception:                            # noqa: BLE001 - closing anyway
            pass


def stamp_for_publish(payload: dict, queued_at: float, now=None) -> dict:
    """Mark a record with how long it waited. Called at publish time, not queue time.

    THE POINT: a value that waited three hours is still true of three hours ago.
    `buffered_for` lets the AMP side file it as history without ever letting it
    count as the machine's current state — which is the difference between an
    outage that AMP recovers from and an outage that makes AMP wrong.
    """
    now = time.time() if now is None else now
    waited = max(0.0, now - float(queued_at))
    out = dict(payload)
    out["buffered_for"] = round(waited, 1)
    # One second of ordinary queueing is not "buffered" in any sense a person
    # means; five is. Below the threshold the flag would fire on every packet
    # and stop meaning anything.
    if waited > 5.0:
        out["buffered"] = True
    return out
