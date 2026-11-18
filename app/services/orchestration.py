"""闭合记录编排：队列、唯一凭证、死信三方的同一条正式闭合影射。

编排回看台只认 `close_vouchers`——同批一条正式闭合，凭证号即归属；
队列里的后到请求以 `已回滚` + 死信因呈现，不计为闭合记录。
"""

from app.db import get_db

CLOSED = "已闭合"
ROLLED_BACK = "已回滚"


def list_vouchers(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT v.*, b.zone_code, b.zone_name, b.biz_date, b.status AS batch_status, "
            "       r.detail AS run_detail, r.created_at AS run_created_at "
            "FROM close_vouchers v "
            "JOIN landing_batches b ON b.id = v.batch_id "
            "LEFT JOIN close_runs r ON r.id = v.run_id "
            "ORDER BY v.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_voucher(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT v.*, b.zone_code, b.zone_name, b.biz_date, b.status AS batch_status, "
            "       r.detail AS run_detail, r.created_at AS run_created_at "
            "FROM close_vouchers v "
            "JOIN landing_batches b ON b.id = v.batch_id "
            "LEFT JOIN close_runs r ON r.id = v.run_id "
            "WHERE v.batch_id=?",
            (batch_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_queue(batch_id: int | None = None, db_path: str | None = None) -> list[dict]:
    sql = (
        "SELECT q.*, b.zone_name, b.zone_code, "
        "       d.id AS dead_letter_id, d.reason AS dead_letter_reason "
        "FROM close_queue q "
        "JOIN landing_batches b ON b.id = q.batch_id "
        "LEFT JOIN close_dead_letters d ON d.queue_id = q.id "
    )
    params: tuple = ()
    if batch_id is not None:
        sql += "WHERE q.batch_id=? "
        params = (batch_id,)
    sql += "ORDER BY q.id DESC"
    with get_db(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def list_dead_letters(batch_id: int | None = None, db_path: str | None = None) -> list[dict]:
    sql = (
        "SELECT d.*, b.zone_name, b.zone_code "
        "FROM close_dead_letters d "
        "JOIN landing_batches b ON b.id = d.batch_id "
    )
    params: tuple = ()
    if batch_id is not None:
        sql += "WHERE d.batch_id=? "
        params = (batch_id,)
    sql += "ORDER BY d.id DESC"
    with get_db(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def close_board(db_path: str | None = None) -> dict:
    """编排回看台：每批一行，只取唯一正式闭合，后到请求计为回滚。"""
    with get_db(db_path) as conn:
        batches = conn.execute(
            "SELECT * FROM landing_batches ORDER BY id DESC"
        ).fetchall()
        items = []
        for batch in batches:
            bid = batch["id"]
            voucher = conn.execute(
                "SELECT * FROM close_vouchers WHERE batch_id=?", (bid,)
            ).fetchone()
            run = None
            if voucher is not None and voucher["run_id"] is not None:
                run = conn.execute(
                    "SELECT * FROM close_runs WHERE id=?", (voucher["run_id"],)
                ).fetchone()
            rolled = conn.execute(
                "SELECT COUNT(*) AS c FROM close_queue WHERE batch_id=? AND state=?",
                (bid, ROLLED_BACK),
            ).fetchone()["c"]
            dead = conn.execute(
                "SELECT COUNT(*) AS c FROM close_dead_letters WHERE batch_id=?",
                (bid,),
            ).fetchone()["c"]
            queued = conn.execute(
                "SELECT COUNT(*) AS c FROM close_queue WHERE batch_id=? AND state='排队中'",
                (bid,),
            ).fetchone()["c"]
            official = (
                {
                    "voucher_no": voucher["voucher_no"],
                    "object_count": voucher["object_count"],
                    "operator": voucher["operator"],
                    "created_at": voucher["created_at"],
                    "run_id": voucher["run_id"],
                    "run_object_count": run["object_count"] if run is not None else None,
                }
                if voucher is not None
                else None
            )
            items.append(
                {
                    "batch_id": bid,
                    "zone_name": batch["zone_name"],
                    "zone_code": batch["zone_code"],
                    "biz_date": batch["biz_date"],
                    "status": batch["status"],
                    "closed_object_count": batch["closed_object_count"],
                    "official_close": official,
                    "official_count": 1 if voucher is not None else 0,
                    "rolled_back_count": rolled,
                    "dead_letter_count": dead,
                    "pending_count": queued,
                }
            )
        return {"items": items}


def close_spot(batch_id: int, db_path: str | None = None) -> dict:
    """占用卡片：批次占用芯片 + 凭证号或死信因。

    一个批次只可能亮一张占用卡片；卡片上的固化数取自正式闭合凭证，
    与批次固化数、编排记录数在闭合时已同一判定。
    """
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            return {}
        voucher = conn.execute(
            "SELECT * FROM close_vouchers WHERE batch_id=?", (batch_id,)
        ).fetchone()
        queue = conn.execute(
            "SELECT * FROM close_queue WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        dead = conn.execute(
            "SELECT * FROM close_dead_letters WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        decided = conn.execute(
            "SELECT COUNT(*) AS c FROM close_queue WHERE batch_id=? AND state='已裁决'",
            (batch_id,),
        ).fetchone()["c"]
        rolled = conn.execute(
            "SELECT COUNT(*) AS c FROM close_queue WHERE batch_id=? AND state='已回滚'",
            (batch_id,),
        ).fetchone()["c"]
        landed = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects "
            "WHERE batch_id=? AND status='已闭合'",
            (batch_id,),
        ).fetchone()["c"]

        if voucher is not None:
            chip = "正式闭合"
            chip_tone = "ok"
            line = (
                f"占用凭证 {voucher['voucher_no']} · 固化数 "
                f"{voucher['object_count']}"
            )
            voucher_no = voucher["voucher_no"]
            fixed_count = voucher["object_count"]
            if rolled:
                line += f" · 后到 {rolled} 笔整笔回滚"
        elif batch["status"] == "已闭合":
            chip = "凭证缺失"
            chip_tone = "warn"
            line = "该批标为已闭合但无正式凭证，请跑可见性对账核查"
            voucher_no = None
            fixed_count = batch["closed_object_count"]
        elif dead is not None:
            chip = "闭合回滚"
            chip_tone = "bad"
            line = f"死信因：{dead['reason']}"
            voucher_no = None
            fixed_count = None
        elif queue is not None and queue["state"] == "排队中":
            chip = "排队中"
            chip_tone = "warn"
            line = "闭合请求排队裁决中"
            voucher_no = None
            fixed_count = None
        else:
            chip = "未占用"
            chip_tone = "idle"
            line = "尚未闭合，无正式凭证"
            voucher_no = None
            fixed_count = None

        # 死信因随卡片亮出：无正式闭合时它就是主因；已有正式闭合时它是
        # 后到请求的回滚因，仍要让值班看到「这一笔为什么没成」。
        dead_reason = dead["reason"] if dead is not None else None

        consistent = (
            voucher is not None
            and batch["closed_object_count"] == voucher["object_count"] == landed
        )

        return {
            "batch": dict(batch),
            "chip": chip,
            "chip_tone": chip_tone,
            "line": line,
            "voucher_no": voucher_no,
            "dead_letter_reason": dead_reason,
            "fixed_object_count": fixed_count,
            "landed_object_count": landed,
            "close_record_count": 1 if voucher is not None else 0,
            "decided_queue_count": decided,
            "rolled_back_queue_count": rolled,
            "same_verdict": bool(consistent),
            "voucher": dict(voucher) if voucher is not None else None,
            "last_queue": dict(queue) if queue is not None else None,
            "last_dead_letter": dict(dead) if dead is not None else None,
        }
