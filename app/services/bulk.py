"""批量写入：一次请求结算一批对象。

提交边界只启用一条：**整批单事务**。任一键撞库或核齐不符，整笔回滚，
前面已写入的键不保留。结算结果（含失败）写入 bulk_runs，供页面与
清洗任务回看。
"""

import json
import sqlite3

from app.db import connect
from app.services.objects import LOCK_TIMEOUT_SECONDS, ServiceError, batch_lock

BOUNDARY_ATOMIC = "整批单事务"
BOUNDARY_PER_KEY = "逐键独立事务"
ACTIVE_BOUNDARY = BOUNDARY_ATOMIC

MAX_BULK_ITEMS = 500


class _AtomicFailure(Exception):
    """整批事务内失败：回滚后按结算失败落库。"""

    def __init__(
        self,
        message: str,
        status_code: int = 409,
        failed_keys: list[str] | None = None,
        rolled_back: bool = True,
        attempted_keys: list[str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.failed_keys = failed_keys or []
        self.rolled_back = rolled_back
        self.attempted_keys = attempted_keys or []


def _run_row(row) -> dict:
    run = dict(row)
    run["success_keys"] = json.loads(run.get("success_keys") or "[]")
    run["failed_keys"] = json.loads(run.get("failed_keys") or "[]")
    run["ok"] = bool(run["ok"])
    run["rolled_back"] = bool(run["rolled_back"])
    return run


def _insert_run(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    operator: str,
    boundary: str,
    declared_count: int,
    actual_count: int,
    success_count: int,
    failed_count: int,
    ok: bool,
    rolled_back: bool,
    success_keys: list[str],
    failed_keys: list[str],
    detail: str,
) -> dict:
    cur = conn.execute(
        "INSERT INTO bulk_runs("
        "batch_id, operator, commit_boundary, declared_count, actual_count, "
        "success_count, failed_count, ok, rolled_back, success_keys, failed_keys, detail"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            operator,
            boundary,
            int(declared_count),
            int(actual_count),
            int(success_count),
            int(failed_count),
            1 if ok else 0,
            1 if rolled_back else 0,
            json.dumps(success_keys, ensure_ascii=False),
            json.dumps(failed_keys, ensure_ascii=False),
            detail,
        ),
    )
    row = conn.execute(
        "SELECT * FROM bulk_runs WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return _run_row(row)


def _object_count(conn: sqlite3.Connection, batch_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM landing_objects WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]


def _record_failure(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    operator: str,
    boundary: str,
    declared_count: int,
    failed_count: int,
    success_keys: list[str],
    failed_keys: list[str],
    detail: str,
    rolled_back: bool,
    actual_count: int = 0,
) -> dict | None:
    """失败也要留痕；回滚后的连接处于自动提交，写入独立生效。"""
    try:
        return _insert_run(
            conn,
            batch_id=batch_id,
            operator=operator,
            boundary=boundary,
            declared_count=declared_count,
            actual_count=actual_count,
            success_count=len(success_keys),
            failed_count=failed_count,
            ok=False,
            rolled_back=rolled_back,
            success_keys=success_keys,
            failed_keys=failed_keys,
            detail=detail,
        )
    except sqlite3.Error:
        return None


def _coerce_size(value) -> int:
    if isinstance(value, bool):
        raise ValueError
    size = int(value)
    if size < 0:
        raise ValueError
    return size


def _normalize(raw_items, expected_count) -> list[dict]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ServiceError("批量对象不能为空")
    if len(raw_items) > MAX_BULK_ITEMS:
        raise ServiceError(f"单批最多 {MAX_BULK_ITEMS} 枚对象")

    if expected_count is not None and expected_count != "":
        try:
            expected = int(expected_count)
        except (TypeError, ValueError):
            raise ServiceError("批次对象数不是整数")
        if expected != len(raw_items):
            raise ServiceError(
                f"批次对象数 {expected} 与实交 {len(raw_items)} 枚不符，已拒绝"
            )

    items: list[dict] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ServiceError(f"第 {idx} 枚格式不正确")
        key = str(raw.get("object_key", "") or "").strip()
        digest = str(raw.get("content_hash", "") or "").strip()
        if not key or not digest:
            raise ServiceError(f"第 {idx} 枚对象键或指纹为空")
        try:
            size = _coerce_size(raw.get("byte_size", 0))
        except (TypeError, ValueError):
            raise ServiceError(f"第 {idx} 枚字节数不合法")
        if key in seen:
            raise ServiceError(f"键 {key} 在本批重复")
        seen.add(key)
        items.append(
            {"object_key": key, "content_hash": digest, "byte_size": size}
        )
    return items


def _open(db_path: str | None) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.isolation_level = None  # 手动 BEGIN / COMMIT
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def bulk_register(
    batch_id: int,
    raw_items,
    expected_count=None,
    operator: str = "",
    commit_boundary: str | None = None,
    db_path: str | None = None,
) -> dict:
    boundary = commit_boundary or ACTIVE_BOUNDARY
    if boundary == BOUNDARY_PER_KEY:
        raise ServiceError("当前仅启用整批单事务边界")
    if boundary != BOUNDARY_ATOMIC:
        raise ServiceError("提交边界不识别")

    raw_list = raw_items if isinstance(raw_items, list) else []
    declared = len(raw_list)

    lock = batch_lock(batch_id)
    if not lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        conn = _open(db_path)
        try:
            _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail="批次正在被写入，请重试",
                rolled_back=False,
            )
        finally:
            conn.close()
        raise ServiceError("批次正在被写入，请重试", 409)

    conn = _open(db_path)
    try:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] != "收集中":
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail="仅收集中批次可批量写入",
                rolled_back=False,
            )
            raise ServiceError("仅收集中批次可批量写入", 409, run=run)

        try:
            items = _normalize(raw_items, expected_count)
        except ServiceError as exc:
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail=exc.message,
                rolled_back=False,
            )
            raise ServiceError(exc.message, exc.status_code, run=run)

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail="批次正在被写入，请重试",
                rolled_back=False,
            )
            raise ServiceError("批次正在被写入，请重试", 409, run=run)

        attempted: list[str] = []
        try:
            # 事务内复核批次状态，防止检查后被并发闭合
            batch = conn.execute(
                "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not batch:
                raise _AtomicFailure("批次不存在", 404, rolled_back=False)
            if batch["status"] != "收集中":
                raise _AtomicFailure(
                    "仅收集中批次可批量写入", 409, rolled_back=False
                )

            pre_count = _object_count(conn, batch_id)
            for item in items:
                exists = conn.execute(
                    "SELECT 1 FROM landing_objects WHERE batch_id=? AND object_key=?",
                    (batch_id, item["object_key"]),
                ).fetchone()
                if exists:
                    raise _AtomicFailure(
                        f"键 {item['object_key']} 已存在，整批回滚",
                        failed_keys=[item["object_key"]],
                        attempted_keys=attempted,
                    )
                conn.execute(
                    "INSERT INTO landing_objects("
                    "batch_id, object_key, content_hash, byte_size, status"
                    ") VALUES (?,?,?,?,'已登记')",
                    (
                        batch_id,
                        item["object_key"],
                        item["content_hash"],
                        item["byte_size"],
                    ),
                )
                attempted.append(item["object_key"])

            # 核齐：声明数、实写键、批次对象数三者必须一致
            post_count = _object_count(conn, batch_id)
            distinct_keys = conn.execute(
                "SELECT COUNT(DISTINCT object_key) AS c FROM landing_objects WHERE batch_id=?",
                (batch_id,),
            ).fetchone()["c"]
            if (
                len(attempted) != declared
                or post_count != pre_count + declared
                or distinct_keys != post_count
            ):
                raise _AtomicFailure("对象数与实写键不符，整批回滚")

            run = _insert_run(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                actual_count=len(attempted),
                success_count=len(attempted),
                failed_count=0,
                ok=True,
                rolled_back=False,
                success_keys=list(attempted),
                failed_keys=[],
                detail=f"整批提交 {len(attempted)} 枚",
            )
            conn.execute("COMMIT")
            return run
        except _AtomicFailure as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            detail = exc.message
            if exc.attempted_keys:
                detail = (
                    f"{exc.message}（{len(exc.attempted_keys)} 枚已写入后回滚）"
                )
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=exc.failed_keys,
                detail=detail,
                rolled_back=exc.rolled_back,
            )
            raise ServiceError(exc.message, exc.status_code, run=run)
        except sqlite3.IntegrityError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail="对象键撞库，整批回滚",
                rolled_back=True,
            )
            raise ServiceError("对象键撞库，整批回滚", 409, run=run)
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            run = _record_failure(
                conn,
                batch_id=batch_id,
                operator=operator,
                boundary=boundary,
                declared_count=declared,
                failed_count=declared,
                success_keys=[],
                failed_keys=[],
                detail="本地库繁忙，整批未落库",
                rolled_back=False,
            )
            raise ServiceError("本地库繁忙，请稍后重试", 409, run=run)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()
        lock.release()


def list_bulk_runs(
    batch_id: int | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> list[dict]:
    capped = max(1, min(int(limit), 500))
    conn = connect(db_path)
    try:
        if batch_id is None:
            rows = conn.execute(
                "SELECT * FROM bulk_runs ORDER BY id DESC LIMIT ?", (capped,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bulk_runs WHERE batch_id=? ORDER BY id DESC LIMIT ?",
                (batch_id, capped),
            ).fetchall()
        return [_run_row(r) for r in rows]
    finally:
        conn.close()


def get_bulk_run(run_id: int, db_path: str | None = None) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM bulk_runs WHERE id=?", (run_id,)
        ).fetchone()
        return _run_row(row) if row else None
    finally:
        conn.close()
