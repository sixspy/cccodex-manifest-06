import sqlite3
import threading

import pytest

from app.db import init_db
from app.services.bulk import bulk_register
from app.services.objects import ServiceError


def _open_batch(authed):
    items = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return next(b["id"] for b in items if b["zone_code"] == "bronze_events")


def _obj_keys(authed, bid):
    detail = authed.get(f"/api/batches/{bid}").json()
    return [o["object_key"] for o in detail["objects"]]


def test_bulk_happy_path_writes_all_and_records_run(authed):
    bid = _open_batch(authed)
    items = [
        {"object_key": "raw/a.parquet", "content_hash": "h-a", "byte_size": 10},
        {"object_key": "raw/b.parquet", "content_hash": "h-b", "byte_size": 20},
        {"object_key": "raw/c.parquet", "content_hash": "h-c", "byte_size": 30},
    ]
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={"items": items, "expected_count": 3},
    )
    assert r.status_code == 200
    body = r.json()
    run = body["run"]
    assert run["ok"] is True
    assert run["rolled_back"] is False
    assert run["declared_count"] == 3
    assert run["actual_count"] == 3
    assert run["success_count"] == 3
    assert run["failed_count"] == 0
    assert run["success_keys"] == ["raw/a.parquet", "raw/b.parquet", "raw/c.parquet"]
    assert sorted(_obj_keys(authed, bid)) == [
        "raw/a.parquet",
        "raw/b.parquet",
        "raw/c.parquet",
    ]

    listed = authed.get("/api/bulk-runs", params={"batch_id": bid}).json()["items"]
    assert listed[0]["id"] == run["id"]
    assert listed[0]["ok"] is True


def test_bulk_collision_rolls_back_whole_batch(authed):
    bid = _open_batch(authed)
    first = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/a.parquet", "content_hash": "h-a", "byte_size": 1},
                {"object_key": "raw/b.parquet", "content_hash": "h-b", "byte_size": 2},
            ]
        },
    )
    assert first.status_code == 200
    assert sorted(_obj_keys(authed, bid)) == ["raw/a.parquet", "raw/b.parquet"]

    # 第二批：新键在前、撞库键在中、再来一个新键
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/new-1.parquet", "content_hash": "h1", "byte_size": 1},
                {"object_key": "raw/a.parquet", "content_hash": "hX", "byte_size": 9},
                {"object_key": "raw/new-2.parquet", "content_hash": "h2", "byte_size": 2},
            ],
            "expected_count": 3,
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert "整批回滚" in body["error"]
    run = body["run"]
    assert run["ok"] is False
    assert run["rolled_back"] is True
    assert run["failed_keys"] == ["raw/a.parquet"]
    assert run["success_keys"] == []
    assert run["actual_count"] == 0
    assert run["failed_count"] == 3
    # 半成功不得留下：前序新键已回滚
    assert sorted(_obj_keys(authed, bid)) == ["raw/a.parquet", "raw/b.parquet"]


def test_bulk_count_mismatch_rejected_before_write(authed):
    bid = _open_batch(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/x.parquet", "content_hash": "hx", "byte_size": 1},
            ],
            "expected_count": 2,
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "不符" in body["error"]
    assert body["run"]["ok"] is False
    assert body["run"]["actual_count"] == 0
    assert _obj_keys(authed, bid) == []


def test_bulk_duplicate_key_inside_batch_rejected(authed):
    bid = _open_batch(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/dup.parquet", "content_hash": "h1", "byte_size": 1},
                {"object_key": "raw/dup.parquet", "content_hash": "h2", "byte_size": 2},
            ]
        },
    )
    assert r.status_code == 400
    assert "重复" in r.json()["error"]
    assert _obj_keys(authed, bid) == []


def test_bulk_rejects_per_key_boundary(authed):
    bid = _open_batch(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/a.parquet", "content_hash": "h", "byte_size": 1},
            ],
            "commit_boundary": "逐键独立事务",
        },
    )
    assert r.status_code == 400
    assert "仅启用" in r.json()["error"]
    assert _obj_keys(authed, bid) == []


def test_bulk_rejects_closed_batch(authed):
    batches = authed.get("/api/batches", params={"status": "已闭合"}).json()["items"]
    bid = batches[0]["id"]
    r = authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/z.parquet", "content_hash": "h", "byte_size": 1},
            ]
        },
    )
    assert r.status_code == 409
    assert "收集中" in r.json()["error"]


def test_failed_run_visible_in_detail_and_runs_list(authed):
    bid = _open_batch(authed)
    authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/a.parquet", "content_hash": "h", "byte_size": 1},
            ]
        },
    )
    authed.post(
        f"/api/batches/{bid}/objects/bulk",
        json={
            "items": [
                {"object_key": "raw/a.parquet", "content_hash": "h", "byte_size": 1},
            ]
        },
    )
    detail = authed.get(f"/api/batches/{bid}").json()
    assert detail["bulk_runs"]
    assert detail["bulk_runs"][0]["ok"] is False
    runs = authed.get("/api/bulk-runs").json()["items"]
    assert len(runs) >= 2


def test_two_writers_same_batch_serialized(authed, tmp_path):
    db = tmp_path / "race.db"
    init_db(str(db))
    bid = 3  # bronze_events 种子，收集中
    results = []
    errors = []

    def writer(prefix):
        items = [
            {"object_key": f"race/{prefix}-{i}.parquet", "content_hash": f"h{i}", "byte_size": 1}
            for i in range(5)
        ]
        try:
            results.append(bulk_register(bid, items, db_path=str(db)))
        except ServiceError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("L", "R")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 2
    conn = sqlite3.connect(str(db))
    try:
        keys = [
            r[0]
            for r in conn.execute(
                "SELECT object_key FROM landing_objects WHERE batch_id=?", (bid,)
            ).fetchall()
        ]
    finally:
        conn.close()
    assert len(keys) == 10
    assert len(set(keys)) == 10


def test_bulk_rollback_leaves_no_partial_objects_via_direct_service(tmp_path):
    db = tmp_path / "rollback.db"
    init_db(str(db))
    with pytest.raises(ServiceError):
        bulk_register(
            3,
            [
                {"object_key": "k1", "content_hash": "h1", "byte_size": 1},
                {"object_key": "k1", "content_hash": "h2", "byte_size": 1},
            ],
            db_path=str(db),
        )
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM landing_objects WHERE batch_id=3"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0
