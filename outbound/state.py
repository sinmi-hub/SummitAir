"""Durable outbound attempts shared by the trigger and the single server process."""
from contextlib import contextmanager
import base64
import json
import secrets
import sqlite3
import time

HEADER = "X-SummitAir-Attempt"
TRANSFER_TIMEOUT = 60
DIAL_TIMEOUT = 120


def client_state(token: str, leg: str) -> str:
    return base64.b64encode(json.dumps({"attempt": token, "leg": leg}).encode()).decode()


def decode_client_state(value):
    try:
        data = json.loads(base64.b64decode(value, validate=True))
        if (isinstance(data, dict) and isinstance(data.get("attempt"), str)
                and data.get("leg") in {"pstn", "openai"}):
            return data["attempt"], data["leg"]
    except (ValueError, TypeError, UnicodeDecodeError):
        pass
    return None


class Store:
    def __init__(self, path):
        self.path = path + ".outbound-state"
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                token TEXT PRIMARY KEY, original TEXT UNIQUE, target TEXT UNIQUE,
                openai_id TEXT UNIQUE, target_answered INTEGER NOT NULL DEFAULT 0,
                phase TEXT NOT NULL, deadline REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS routes (
                call_id TEXT PRIMARY KEY, domain TEXT NOT NULL)""")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def create(self):
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO attempts (token, phase, deadline) VALUES (?, 'dialing', ?)",
                       (token, time.time() + DIAL_TIMEOUT))
        return token

    def observe(self, token, leg, call_id, event):
        """Bind IDs from either the HTTP result or a signed webhook; claim transfer once."""
        column = "original" if leg == "pstn" else "target"
        with self.transaction() as db:
            row = db.execute("SELECT * FROM attempts WHERE token=?", (token,)).fetchone()
            if row is None or (row[column] and row[column] != call_id):
                return False
            # Target metadata is meaningful only after we claimed a transfer.
            if leg == "openai" and row["phase"] == "dialing":
                return False
            db.execute(f"UPDATE attempts SET {column}=? WHERE token=?", (call_id, token))
            if row["phase"] in {"cleanup", "done"}:
                # A late answer/HTTP result after a timeout must also be cleaned up.
                if event != "call.hangup":
                    db.execute("UPDATE attempts SET phase='cleanup' WHERE token=?", (token,))
                return False
            if event == "call.hangup":
                db.execute("UPDATE attempts SET phase='cleanup' WHERE token=?", (token,))
            elif leg == "pstn" and event == "call.answered" and row["phase"] == "dialing":
                db.execute("UPDATE attempts SET phase='transferring', deadline=? WHERE token=?",
                           (time.time() + TRANSFER_TIMEOUT, token))
                return True
            elif leg == "openai" and event == "call.answered":
                db.execute("UPDATE attempts SET target_answered=1 WHERE token=?", (token,))
                if row["openai_id"]:
                    db.execute("UPDATE attempts SET phase='active', deadline=NULL WHERE token=?", (token,))
        return False

    def route(self, call_id, token=None):
        """Persist domain selection before accepting, including inbound calls and retries."""
        with self.transaction() as db:
            row = db.execute("SELECT domain FROM routes WHERE call_id=?", (call_id,)).fetchone()
            if row:
                return row["domain"]
            domain = "inbound"
            if token is not None:
                attempt = db.execute("SELECT * FROM attempts WHERE token=?", (token,)).fetchone()
                if (attempt is None or attempt["phase"] not in {"transferring", "active"}
                        or (attempt["openai_id"] and attempt["openai_id"] != call_id)
                        or (attempt["deadline"] is not None and attempt["deadline"] < time.time())):
                    raise ValueError("unknown, expired, or already bound outbound attempt")
                db.execute("UPDATE attempts SET openai_id=? WHERE token=?", (call_id, token))
                if attempt["target_answered"]:
                    db.execute("UPDATE attempts SET phase='active', deadline=NULL WHERE token=?", (token,))
                domain = "outbound"
            db.execute("INSERT INTO routes VALUES (?, ?)", (call_id, domain))
            return domain

    def fail(self, token):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET phase='cleanup' WHERE token=? AND phase!='done'", (token,))

    def recover(self):
        # Control sessions cannot safely be resumed after a process restart.
        with self.transaction() as db:
            db.execute("UPDATE attempts SET phase='cleanup' WHERE phase!='done'")

    def cleanup_due(self):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET phase='cleanup' WHERE deadline<=? AND phase!='done'",
                       (time.time(),))
            return [dict(row) for row in db.execute("SELECT * FROM attempts WHERE phase='cleanup'")]

    def finish(self, attempt):
        with self.transaction() as db:
            # Do not erase work if another webhook discovered a leg during HTTP cleanup.
            db.execute("""UPDATE attempts SET phase='done', deadline=NULL
                WHERE token=? AND phase='cleanup' AND original IS ? AND target IS ?""",
                       (attempt["token"], attempt["original"], attempt["target"]))
