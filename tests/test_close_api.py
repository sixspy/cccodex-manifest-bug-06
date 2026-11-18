"""接口层：点闭合一次裁决，占用卡片 / 闭合队列 / 批次附页 / 编排回看台四处同查。"""

import threading

import pytest

from app.db import get_db


def _open_batch(client, keys=("raw/a.parquet", "raw/b.parquet"), zone="race_api"):
    batches = client.get("/api/batches", params={"status": "收集中"}).json()["items"]
    bid = batches[0]["id"]
    for i, key in enumerate(keys):
        r = client.post(
            f"/api/batches/{bid}/objects",
            json={"object_key": key, "content_hash": f"h{i}", "byte_size": 10},
        )
        assert r.status_code == 200, r.text
    return bid


def test_close_returns_voucher_and_spot(authed):
    bid = _open_batch(authed)
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["outcome"] == "closed"
    assert body["voucher_no"]
    assert body["voucher"]["object_count"] == 2
    assert body["run"]["object_count"] == 2

    spot = authed.get(f"/api/batches/{bid}/close-spot").json()
    assert spot["chip"] == "正式闭合"
    assert spot["voucher_no"] == body["voucher_no"]
    assert spot["fixed_object_count"] == 2
    assert spot["same_verdict"] is True
    assert spot["close_record_count"] == 1

    # 四处同查：队列 / 回看台 / 附页取到的都是同一条正式闭合
    queue = authed.get("/api/close-queue", params={"batch_id": bid}).json()["items"]
    assert len(queue) == 1
    assert queue[0]["state"] == "已裁决"
    assert queue[0]["voucher_no"] == body["voucher_no"]

    board = authed.get("/api/close-board").json()["items"]
    row = next(x for x in board if x["batch_id"] == bid)
    assert row["official_count"] == 1
    assert row["official_close"]["voucher_no"] == body["voucher_no"]
    assert row["official_close"]["object_count"] == 2
    assert row["official_close"]["run_object_count"] == 2


def test_second_click_rolls_back_with_dead_letter(authed):
    bid = _open_batch(authed, keys=("raw/x.parquet",))
    first = authed.post(f"/api/batches/{bid}/close", json={})
    assert first.status_code == 200
    winner = first.json()["voucher_no"]

    second = authed.post(f"/api/batches/{bid}/close", json={})
    assert second.status_code == 409
    body = second.json()
    assert body["ok"] is False
    assert body["outcome"] == "rolled_back"
    assert body["voucher_no"] is None
    assert body["dead_letter"]["state"] == "已回滚"
    assert body["dead_letter"]["reason"]
    # 白话：说清归属，不吐栈
    assert winner in body["error"] or winner in body["detail"].get("winner_voucher_no", "")
    assert "Traceback" not in second.text

    dead = authed.get("/api/dead-letters", params={"batch_id": bid}).json()["items"]
    assert len(dead) == 1
    assert dead[0]["winner_voucher_no"] == winner
    assert dead[0]["reason"]

    board = authed.get("/api/close-board").json()["items"]
    row = next(x for x in board if x["batch_id"] == bid)
    assert row["official_count"] == 1
    assert row["official_close"]["voucher_no"] == winner
    assert row["rolled_back_count"] == 1

    spot = authed.get(f"/api/batches/{bid}/close-spot").json()
    assert spot["chip"] == "正式闭合"
    assert spot["voucher_no"] == winner


def test_contended_api_calls_leave_single_voucher(authed):
    bid = _open_batch(authed, keys=("raw/q1.parquet", "raw/q2.parquet", "raw/q3.parquet"))

    codes = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def hit():
        barrier.wait()
        r = authed.post(f"/api/batches/{bid}/close", json={})
        with lock:
            codes.append(r.status_code)

    ts = [threading.Thread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert sorted(codes) == [200, 409], codes

    vouchers = authed.get("/api/close-vouchers").json()["items"]
    mine = [v for v in vouchers if v["batch_id"] == bid]
    assert len(mine) == 1
    assert mine[0]["object_count"] == 3

    board = authed.get("/api/close-board").json()["items"]
    row = next(x for x in board if x["batch_id"] == bid)
    assert row["official_count"] == 1
    assert row["official_close"]["voucher_no"] == mine[0]["voucher_no"]


def test_visibility_flags_double_write_as_mismatch(authed):
    """旧缺陷遗留的双写必须被可见性对账抓成错位；新写入由唯一索引当场拦下。"""
    import os

    from app.db import get_db

    bid = _open_batch(authed, keys=("raw/m.parquet",))
    assert authed.post(f"/api/batches/{bid}/close", json={}).status_code == 200

    db = os.environ["DATABASE_PATH"]

    # 新写入：唯一索引当场拦住第二条编排闭合记录
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with get_db(db) as conn:
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) "
                "VALUES (?,?,1,?)",
                (bid, 9, "故障注入重复闭合"),
            )

    # 遗留双写：模拟旧库直接落两条 + 固化数被第二次覆盖
    with get_db(db) as conn:
        conn.execute("DROP INDEX IF EXISTS ux_close_runs_batch")
        conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, detail) "
            "VALUES (?,?,1,?)",
            (bid, 9, "遗留双写"),
        )
        conn.execute(
            "UPDATE landing_batches SET closed_object_count=9 WHERE id=?", (bid,)
        )

    report = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert "正式凭证" in report["summary"]
    assert report["mismatch_count"] >= 1
