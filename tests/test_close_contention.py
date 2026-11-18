"""争用复现单：两名值班同秒抢闭合。

固化数只能写一次、编排闭合记录只能一条、正式闭合凭证全仓同批只能一条；
后到的整笔回滚进死信队列，两种提交命运（已裁决 / 已回滚）不得同挂。

用真实线程 + 各自连接打同一个 SQLite 文件，模拟两个值班同时 POST 闭合。
"""

import threading

import pytest

from app.db import init_db, get_db
from app.services import close_board, close_spot, close_batch
from app.services.objects import ServiceError


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "race.db")
    init_db(path)
    return path


def _open_batch_with(db, count=2):
    with get_db(db) as conn:
        bid = conn.execute(
            "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
            "VALUES (?,?,?, '收集中')",
            (f"race_{conn.total_changes}", "争用铜区", "2026-11-15"),
        ).lastrowid
        for i in range(count):
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?, '已登记')",
                (bid, f"part-{i:03d}.parquet", f"hash-race-{i}", 128 + i),
            )
        return bid


def _race(db, batch_id, operators=("值班甲", "值班乙")):
    barrier = threading.Barrier(len(operators))
    results = []
    lock = threading.Lock()

    def worker(op):
        barrier.wait()
        try:
            payload = close_batch(batch_id, operator=op, db_path=db)
            outcome = ("closed", payload["voucher_no"])
        except ServiceError as exc:
            outcome = ("rolled_back", exc.message)
        with lock:
            results.append((op, outcome))

    threads = [threading.Thread(target=worker, args=(op,)) for op in operators]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    return results


def test_same_second_double_close_single_voucher(db_path):
    bid = _open_batch_with(db_path, 3)
    results = _race(db_path, bid)

    closed = [r for r in results if r[1][0] == "closed"]
    rolled = [r for r in results if r[1][0] == "rolled_back"]
    assert len(closed) == 1, results
    assert len(rolled) == 1, results
    winner_voucher = closed[0][1][1]

    with get_db(db_path) as conn:
        # 正式闭合凭证全仓同批只一条
        assert conn.execute(
            "SELECT COUNT(*) FROM close_vouchers WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        # 编排闭合记录只一条，不再翻倍
        assert conn.execute(
            "SELECT COUNT(*) FROM close_runs WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        # 固化数写一次且与实到一致
        batch = conn.execute(
            "SELECT status, closed_object_count FROM landing_batches WHERE id=?", (bid,)
        ).fetchone()
        assert batch["status"] == "已闭合"
        assert batch["closed_object_count"] == 3
        landed = conn.execute(
            "SELECT COUNT(*) FROM landing_objects WHERE batch_id=? AND status='已闭合'",
            (bid,),
        ).fetchone()[0]
        assert landed == 3
        # 凭证号与胜者一致；失败方无凭证
        voucher = conn.execute(
            "SELECT voucher_no, object_count FROM close_vouchers WHERE batch_id=?", (bid,)
        ).fetchone()
        assert voucher["voucher_no"] == winner_voucher
        assert voucher["object_count"] == 3

        # 提交命运二选一，禁止同挂
        losers = conn.execute(
            "SELECT * FROM close_queue WHERE batch_id=? AND state='已回滚'", (bid,)
        ).fetchall()
        assert len(losers) == 1
        assert losers[0]["voucher_no"] is None
        assert losers[0]["reason"]
        winners = conn.execute(
            "SELECT * FROM close_queue WHERE batch_id=? AND state='已裁决'", (bid,)
        ).fetchall()
        assert len(winners) == 1
        assert winners[0]["voucher_no"] == winner_voucher

        # 死信只一条，且指向败者请求
        dead = conn.execute(
            "SELECT * FROM close_dead_letters WHERE batch_id=?", (bid,)
        ).fetchall()
        assert len(dead) == 1
        assert dead[0]["queue_id"] == losers[0]["id"]
        assert dead[0]["reason"]

    # 回看台只呈现一条正式闭合
    board = next(i for i in close_board(db_path)["items"] if i["batch_id"] == bid)
    assert board["official_count"] == 1
    assert board["official_close"]["voucher_no"] == winner_voucher
    assert board["rolled_back_count"] == 1
    assert board["dead_letter_count"] == 1


def test_five_way_contention_still_single_voucher(db_path):
    bid = _open_batch_with(db_path, 4)
    results = _race(db_path, bid, operators=tuple(f"值班{i}" for i in range(5)))

    closed = [r for r in results if r[1][0] == "closed"]
    assert len(closed) == 1, results
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM close_vouchers WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM close_runs WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM close_dead_letters WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM close_queue WHERE batch_id=? AND state='已回滚'", (bid,)
        ).fetchone()[0] == 4


def test_occupancy_card_shows_voucher_or_dead_letter_reason(db_path):
    bid = _open_batch_with(db_path)
    _race(db_path, bid)

    spot = close_spot(bid, db_path)
    assert spot["chip"] == "正式闭合"
    assert spot["voucher_no"]
    # 卡片亮凭证号，同时亮后到者的死信因（这一笔为什么没成）
    assert spot["dead_letter_reason"]
    assert spot["close_record_count"] == 1
    assert spot["same_verdict"] is True
    assert spot["fixed_object_count"] == spot["landed_object_count"] == 2

    # 回滚方的死信因也能在同一卡片对应的死信接口里查到
    with get_db(db_path) as conn:
        dead = conn.execute(
            "SELECT * FROM close_dead_letters WHERE batch_id=?", (bid,)
        ).fetchone()
        assert dead is not None
        loser = conn.execute(
            "SELECT * FROM close_queue WHERE id=?", (dead["queue_id"],)
        ).fetchone()
        assert loser["state"] == "已回滚"
        assert loser["voucher_no"] is None


def test_random_retry_after_rollback_never_double_writes(db_path):
    """回滚后仍有排队者重选：重试也只保留同一条正式闭合，不新增记录。"""
    bid = _open_batch_with(db_path)
    first = close_batch(bid, operator="值班甲", db_path=db_path)
    assert first["outcome"] == "closed"

    for i in range(3):
        with pytest.raises(ServiceError) as ei:
            close_batch(bid, operator=f"重试{i}", db_path=db_path)
        assert ei.value.status_code == 409
        assert ei.value.casualty["state"] == "已回滚"

    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM close_vouchers WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM close_runs WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM close_dead_letters WHERE batch_id=?", (bid,)
        ).fetchone()[0] == 3
