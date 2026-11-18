"""清单闭合：唯一正式闭合裁决。

一次「点闭合」= 先入闭合队列，再在写锁内裁决，最后落终态二选一：

* 先手成功  —— 写唯一闭合凭证、固化数、编排闭合记录；队列行 `已裁决` 带凭证号。
* 后到回滚  —— 整笔回滚（不碰固化数与编排记录），死信队列留回滚因；队列行 `已回滚`。

两条提交命运（已裁决 / 已回滚）由表 CHECK、唯一索引与单写事务硬拦，不会同挂。
正式闭合凭证表的 UNIQUE(batch_id) 保证全仓同批只留一条正式闭合。
"""

import sqlite3

from app.db import exclusive, get_db, make_voucher_no
from app.services.objects import ServiceError

QUEUED = "排队中"
DECIDED = "已裁决"
ROLLED_BACK = "已回滚"


def _voucher_row(conn: sqlite3.Connection, batch_id: int):
    return conn.execute(
        "SELECT * FROM close_vouchers WHERE batch_id=?", (batch_id,)
    ).fetchone()


def _run_row(conn: sqlite3.Connection, run_id: int | None):
    if run_id is None:
        return None
    return conn.execute("SELECT * FROM close_runs WHERE id=?", (run_id,)).fetchone()


def _voucher_view(conn: sqlite3.Connection, voucher) -> dict:
    run = _run_row(conn, voucher["run_id"])
    return {
        "id": voucher["id"],
        "batch_id": voucher["batch_id"],
        "voucher_no": voucher["voucher_no"],
        "object_count": voucher["object_count"],
        "operator": voucher["operator"],
        "queue_id": voucher["queue_id"],
        "run_id": voucher["run_id"],
        "created_at": voucher["created_at"],
        "run": dict(run) if run is not None else None,
    }


def _enqueue(batch_id: int, operator: str, db_path: str | None) -> int:
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO close_queue(batch_id, operator, state) VALUES (?,?,?)",
            (batch_id, operator, QUEUED),
        )
        return int(cur.lastrowid)


def _dead_letter(
    batch_id: int,
    queue_id: int,
    operator: str,
    reason: str,
    code: int,
    winner_voucher_no: str | None,
    db_path: str | None,
) -> None:
    """整笔回滚后的落痕：队列行转 `已回滚`，死信队列留一条，且一个请求至多一条。"""
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE close_queue SET state=?, reason=?, code=?, decided_at=datetime('now') "
            "WHERE id=? AND state=?",
            (ROLLED_BACK, reason, code, queue_id, QUEUED),
        )
        conn.execute(
            "INSERT INTO close_dead_letters("
            "batch_id, queue_id, operator, reason, code, winner_voucher_no) "
            "VALUES (?,?,?,?,?,?)",
            (batch_id, queue_id, operator, reason, code, winner_voucher_no),
        )


def close_batch(
    batch_id: int,
    operator: str = "admin",
    db_path: str | None = None,
) -> dict:
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT id FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)

    queue_id = _enqueue(batch_id, operator, db_path)

    try:
        return _decide(batch_id, operator, queue_id, db_path)
    except ServiceError as exc:
        _dead_letter(
            batch_id,
            queue_id,
            operator,
            exc.message,
            exc.status_code,
            (exc.payload or {}).get("winner_voucher_no"),
            db_path,
        )
        exc.casualty = {
            "queue_id": queue_id,
            "state": ROLLED_BACK,
            "reason": exc.message,
            "code": exc.status_code,
        }
        raise
    except sqlite3.IntegrityError as exc:
        reason = "唯一闭合凭证冲突，本笔整笔回滚（另一笔先手已裁决）"
        _dead_letter(batch_id, queue_id, operator, reason, 409, None, db_path)
        fail = ServiceError(reason, 409, payload={"state": ROLLED_BACK})
        fail.casualty = {
            "queue_id": queue_id,
            "state": ROLLED_BACK,
            "reason": reason,
            "code": 409,
        }
        raise fail from exc


def _decide(batch_id: int, operator: str, queue_id: int, db_path: str | None) -> dict:
    with exclusive(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已作废":
            raise ServiceError("批次已作废，不可闭合", 409)

        # BEGIN IMMEDIATE 已拿到写锁，此后的判断才是裁决依据。
        voucher = _voucher_row(conn, batch_id)
        if voucher is not None:
            raise ServiceError(
                f"先手已写入正式闭合 {voucher['voucher_no']}，本笔整笔回滚",
                409,
                payload={"winner_voucher_no": voucher["voucher_no"]},
            )
        if batch["status"] == "已闭合":
            raise ServiceError("该批已闭合但无正式凭证，请先跑可见性对账核查", 409)

        total = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects "
            "WHERE batch_id=? AND status <> '已作废'",
            (batch_id,),
        ).fetchone()["c"]
        if total <= 0:
            raise ServiceError("空批不可闭合", 400)

        # 编排闭合记录、固化数、凭证同一次写事务；同一判定，双写即拦。
        cur = conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
            (batch_id, total, f"闭合 {total} 个对象 · 操作人 {operator}"),
        )
        run_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE landing_batches SET status='已闭合', closed_object_count=? WHERE id=?",
            (total, batch_id),
        )
        conn.execute(
            "UPDATE landing_objects SET status='已闭合' "
            "WHERE batch_id=? AND status='已登记'",
            (batch_id,),
        )
        conn.execute(
            "INSERT INTO close_vouchers("
            "batch_id, voucher_no, object_count, operator, queue_id, run_id) "
            "VALUES (?,?,?,?,?,?)",
            (batch_id, make_voucher_no(batch["biz_date"], batch_id), total, operator, queue_id, run_id),
        )

        vouched = _voucher_row(conn, batch_id)
        run = _run_row(conn, run_id)
        if vouched is None or run is None:
            raise ServiceError("正式闭合凭证写入失败，本笔整笔回滚", 500)

        # 同一判定：凭证固化数 = 编排记录数 = 批次固化数 = 实到已闭合对象数。
        landed = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects "
            "WHERE batch_id=? AND status='已闭合'",
            (batch_id,),
        ).fetchone()["c"]
        batch_now = conn.execute(
            "SELECT closed_object_count FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()["closed_object_count"]
        if not (vouched["object_count"] == run["object_count"] == batch_now == landed):
            raise ServiceError(
                "固化数与正式闭合不一致，本笔整笔回滚",
                409,
                payload={
                    "voucher_object_count": vouched["object_count"],
                    "run_object_count": run["object_count"],
                    "batch_object_count": batch_now,
                    "landed_object_count": landed,
                },
            )

        cur = conn.execute(
            "UPDATE close_queue SET state=?, voucher_no=?, code=?, decided_at=datetime('now') "
            "WHERE id=? AND state=?",
            (DECIDED, vouched["voucher_no"], 200, queue_id, QUEUED),
        )
        if cur.rowcount != 1:
            raise ServiceError("闭合队列状态异常，本笔整笔回滚", 409)

        return {
            "outcome": "closed",
            "queue_id": queue_id,
            "state": DECIDED,
            "voucher_no": vouched["voucher_no"],
            "voucher": _voucher_view(conn, vouched),
            "run": dict(run),
            "message": f"正式闭合 {vouched['voucher_no']}：固化 {total} 个对象",
        }
