import sqlite3
import threading

from app.db import connect, get_db

LOCK_TIMEOUT_SECONDS = 6.0

_locks_guard = threading.Lock()
_batch_locks: dict[int, threading.Lock] = {}


def batch_lock(batch_id: int) -> threading.Lock:
    """同批写入串行闸：两侧灌数员同批抢写时不会双写互盖。"""
    with _locks_guard:
        lock = _batch_locks.get(batch_id)
        if lock is None:
            lock = threading.Lock()
            _batch_locks[batch_id] = lock
        return lock


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400, run: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.run = run


def list_objects(batch_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def register_object(
    batch_id: int,
    object_key: str,
    content_hash: str,
    byte_size: int,
    db_path: str | None = None,
) -> dict:
    key = (object_key or "").strip()
    digest = (content_hash or "").strip()
    if not key or not digest:
        raise ServiceError("对象键与内容指纹不能为空")
    if byte_size < 0:
        raise ServiceError("字节数不能为负")

    lock = batch_lock(batch_id)
    if not lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        raise ServiceError("批次正在被写入，请重试", 409)
    conn = connect(db_path)
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise ServiceError("批次正在被写入，请重试", 409)
        try:
            batch = conn.execute(
                "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not batch:
                raise ServiceError("批次不存在", 404)
            if batch["status"] != "收集中":
                raise ServiceError("仅收集中批次可登记对象", 409)
            exists = conn.execute(
                "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
                (batch_id, key),
            ).fetchone()
            if exists:
                raise ServiceError("同批对象键已存在", 409)
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?, '已登记')",
                (batch_id, key, digest, int(byte_size)),
            )
            row = conn.execute(
                "SELECT * FROM landing_objects WHERE batch_id=? AND object_key=?",
                (batch_id, key),
            ).fetchone()
            conn.execute("COMMIT")
            return dict(row)
        except ServiceError:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise ServiceError("同批对象键已存在", 409)
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
        lock.release()
