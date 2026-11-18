from app.db import get_db


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM visibility_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_visibility(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip()
    if not day:
        day = "2026-11-16"
    with get_db(db_path) as conn:
        closed = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches WHERE status='已闭合' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
        open_n = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches WHERE status='收集中' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
        objects = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects"
        ).fetchone()["c"]
        # 孤儿：对象所属批次已作废（基线种子无作废，恒 0；供后续切口）
        orphan = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects o "
            "JOIN landing_batches b ON b.id=o.batch_id WHERE b.status='已作废'"
        ).fetchone()["c"]
        # 错位：正式闭合凭证与批次固化数、实到已闭合对象数三者不同一判定，
        # 或缺凭证、或同批出现多条编排闭合记录（双写）——任一即计错位。
        mismatch = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches b "
            "LEFT JOIN close_vouchers v ON v.batch_id=b.id "
            "WHERE b.status='已闭合' AND ("
            "  v.id IS NULL"
            "  OR b.closed_object_count IS NULL"
            "  OR v.object_count != b.closed_object_count"
            "  OR b.closed_object_count != ("
            "    SELECT COUNT(*) FROM landing_objects o "
            "    WHERE o.batch_id=b.id AND o.status='已闭合')"
            "  OR (SELECT COUNT(*) FROM close_runs r WHERE r.batch_id=b.id) <> 1"
            ")"
        ).fetchone()["c"]
        vouchers = conn.execute(
            "SELECT COUNT(*) AS c FROM close_vouchers"
        ).fetchone()["c"]
        rolled = conn.execute(
            "SELECT COUNT(*) AS c FROM close_queue WHERE state='已回滚'"
        ).fetchone()["c"]
        dead = conn.execute(
            "SELECT COUNT(*) AS c FROM close_dead_letters"
        ).fetchone()["c"]
        summary = (
            f"截至 {day}：已闭合 {closed} 批，收集中 {open_n} 批，"
            f"对象 {objects}，孤儿 {orphan}，错位 {mismatch}；"
            f"正式凭证 {vouchers} 条，回滚请求 {rolled} 笔（死信 {dead} 条）"
        )
        cur = conn.execute(
            "INSERT INTO visibility_reports("
            "as_of_date, closed_batch_count, open_batch_count, object_count, "
            "orphan_object_count, mismatch_count, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (day, closed, open_n, objects, orphan, mismatch, summary),
        )
        row = conn.execute(
            "SELECT * FROM visibility_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
