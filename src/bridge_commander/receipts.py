"""Private execution receipts. Arguments and outputs are hashed, never written here."""
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

MAX_CALL_SECONDS = 620
RETENTION_SECONDS = 30 * 86400
MAX_RECEIPTS = 100000
APPLICATION_ID = 1111708242


class ReceiptError(Exception):
    pass


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def result_hash(content, is_error):
    return digest({"content": content, "isError": bool(is_error)})


def device_scope(creds):
    # Tokens can rotate without resetting duplicate protection for the same registered device.
    from urllib.parse import urlsplit
    parts = urlsplit(creds["cloud"])
    return digest({"origin": "%s://%s" % (parts.scheme.lower(), parts.netloc.lower()),
                   "device": str(creds.get("device_id") or "")})


class ReceiptJournal:
    def __init__(self, path, scope, max_receipts=MAX_RECEIPTS):
        self.scope, self.max_receipts = scope, max_receipts
        self.lock = threading.RLock()
        if str(path) != ":memory:":
            path = Path(path).expanduser()
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._private(path.parent, directory=True)
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            self._private(path)
        try:
            self.conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False, isolation_level=None)
        except sqlite3.Error:
            raise ReceiptError("cannot open execution journal; no calls may execute without its receipts") from None
        self.conn.row_factory = sqlite3.Row
        try:
            app_id = self.conn.execute("PRAGMA application_id").fetchone()[0]
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != APPLICATION_ID and (app_id or tables):
                raise ReceiptError("receipt path is not a Bridge execution journal")
            self.conn.execute("PRAGMA application_id=%d" % APPLICATION_ID)
            self.conn.execute("PRAGMA journal_mode=DELETE")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.execute("PRAGMA max_page_count=16384")  # 64 MiB at SQLite's default 4096-byte page size
            self.conn.execute("CREATE TABLE IF NOT EXISTS receipts ("
                              "scope TEXT NOT NULL, id TEXT NOT NULL, tool TEXT NOT NULL, args_hash TEXT NOT NULL, "
                              "state TEXT NOT NULL, result_hash TEXT, is_error INTEGER, expires REAL NOT NULL, "
                              "posted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope, id))")
            # A previous process may have changed the machine before it could record completion.
            self.conn.execute("UPDATE receipts SET state='UNKNOWN', posted=0 WHERE scope=? AND state='RUNNING'",
                              (self.scope,))
        except sqlite3.Error:
            self.conn.close()
            raise ReceiptError("execution journal is invalid; preserve it for reconciliation instead of resetting it") from None
        except Exception:
            self.conn.close()
            raise

    @staticmethod
    def _private(path, directory=False):
        info = path.lstat()
        wanted = stat.S_ISDIR if directory else stat.S_ISREG
        if not wanted(info.st_mode) or (not directory and info.st_nlink != 1):
            raise ReceiptError("receipt path must be a regular private %s" % ("directory" if directory else "file"))
        if os.name == "nt":
            raise ReceiptError("private execution journal ACLs are not yet verified on Windows")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReceiptError("receipt path must be owned by this user with mode %s" % ("0700" if directory else "0600"))

    def begin(self, call, now=None):
        """Atomically claim an unexpired call before execution; repeats return their prior state."""
        now = time.time() if now is None else now
        cid, tool = call.get("id"), call.get("tool")
        if not isinstance(cid, str) or not 1 <= len(cid) <= 128 or not isinstance(tool, str) or not tool:
            raise ReceiptError("invalid call identity")
        args = call.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ReceiptError("call arguments must be an object")
        created, expires = call.get("created_at"), call.get("expires_at")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (created, expires)):
            raise ReceiptError("cloud must provide finite created_at/expires_at for durable calls")
        fingerprint = digest({"tool": tool, "args": args,
                              "created_at": created, "expires_at": expires})
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT * FROM receipts WHERE scope=? AND id=?", (self.scope, cid)).fetchone()
                if row:
                    if row["args_hash"] != fingerprint:
                        raise ReceiptError("call id was reused with a different payload")
                    if row["state"] != "RUNNING":
                        self.conn.execute("UPDATE receipts SET posted=0 WHERE scope=? AND id=?", (self.scope, cid))
                    self.conn.commit()
                    return dict(row), False
                if created <= 0 or created > now + 60 or expires <= now or not 0 < expires - created <= MAX_CALL_SECONDS:
                    raise ReceiptError("call expired or has an invalid execution window; nothing was executed")
                # Expired envelopes cannot execute after their tombstones are removed.
                self.conn.execute("DELETE FROM receipts WHERE expires<? AND state!='RUNNING'", (now - RETENTION_SECONDS,))
                if self.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= self.max_receipts:
                    raise ReceiptError("receipt journal is full; nothing was executed")
                self.conn.execute("INSERT INTO receipts (scope,id,tool,args_hash,state,expires) VALUES (?,?,?,?,?,?)",
                                  (self.scope, cid, tool, fingerprint, "RUNNING", expires))
                self.conn.commit()
                return {"id": cid, "state": "RUNNING"}, True
            except Exception:
                self.conn.rollback()
                raise

    def complete(self, cid, content, is_error):
        hashed = result_hash(content, is_error)
        with self.lock:
            self.conn.execute("UPDATE receipts SET state='COMPLETED',result_hash=?,is_error=?,posted=0 "
                              "WHERE scope=? AND id=? AND state IN ('RUNNING','UNKNOWN')",
                              (hashed, int(bool(is_error)), self.scope, cid))
            row = self.conn.execute("SELECT state,result_hash,is_error FROM receipts WHERE scope=? AND id=?",
                                    (self.scope, cid)).fetchone()
            if not row or row["state"] != "COMPLETED" or row["result_hash"] != hashed:
                raise ReceiptError("completion conflicts with the recorded execution")
        return {"state": "COMPLETED", "result_hash": hashed,
                "result_available": True, "is_error": bool(is_error)}

    def unknown(self, cid):
        with self.lock:
            self.conn.execute("UPDATE receipts SET state='UNKNOWN',posted=0 WHERE scope=? AND id=? AND state='RUNNING'",
                              (self.scope, cid))

    def pending(self, limit=16):
        with self.lock:
            return [dict(row) for row in self.conn.execute(
                "SELECT * FROM receipts WHERE scope=? AND posted=0 AND state IN ('COMPLETED','UNKNOWN') LIMIT ?",
                (self.scope, limit))]

    def posted(self, cid, receipt):
        with self.lock:
            changed = self.conn.execute("UPDATE receipts SET posted=1 WHERE scope=? AND id=? "
                                        "AND state=? AND result_hash IS ?",
                                        (self.scope, cid, receipt["state"], receipt.get("result_hash"))).rowcount
            return bool(changed)

    def close(self):
        with self.lock:
            self.conn.close()
