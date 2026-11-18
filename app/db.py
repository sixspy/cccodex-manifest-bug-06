import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

BUSY_TIMEOUT_MS = 15000


def connect(db_path: str | None = None, timeout: float = BUSY_TIMEOUT_MS / 1000) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    try:
        # WAL：读视图（占用卡片 / 回看台）不被闭合写事务阻塞。
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def exclusive(db_path: str | None = None):
    """闭合裁决专用：抢写锁后再读后写。

    BEGIN IMMEDIATE 让同一批次的裁决严格串行；SQLite 保证同一时刻只有一个
    写事务，后到者在 busy_timeout 内排队，从而「先手」只有一条。
    """
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

-- 闭合队列：每一次「点闭合」都先入队，裁决后才落终态。
-- 提交命运二选一，禁止同挂：已裁决 必须带凭证号且无回滚因；已回滚 必须无凭证号且有回滚因。
CREATE TABLE IF NOT EXISTS close_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  operator TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('排队中','已裁决','已回滚')),
  voucher_no TEXT,
  reason TEXT NOT NULL DEFAULT '',
  code INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at TEXT,
  CHECK (
    (state = '排队中' AND voucher_no IS NULL AND reason = '')
    OR (state = '已裁决' AND voucher_no IS NOT NULL AND reason = '')
    OR (state = '已回滚' AND voucher_no IS NULL AND reason <> '')
  ),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

-- 唯一闭合凭证表：UNIQUE(batch_id) 是「全仓只留一条正式闭合」的硬闸。
CREATE TABLE IF NOT EXISTS close_vouchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  voucher_no TEXT NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  operator TEXT NOT NULL,
  queue_id INTEGER,
  run_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(queue_id) REFERENCES close_queue(id),
  FOREIGN KEY(run_id) REFERENCES close_runs(id)
);

-- 死信队列：整笔回滚的闭合请求只在这里留痕，一个请求至多一条。
-- winner_voucher_no 仅记录「先手是谁」，回滚方自身不挂凭证。
CREATE TABLE IF NOT EXISTS close_dead_letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  queue_id INTEGER UNIQUE,
  operator TEXT NOT NULL,
  reason TEXT NOT NULL,
  code INTEGER,
  winner_voucher_no TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id),
  FOREIGN KEY(queue_id) REFERENCES close_queue(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def make_voucher_no(biz_date: str, batch_id: int) -> str:
    day = "".join(ch for ch in (biz_date or "") if ch.isdigit()) or "00000000"
    return f"CL-{day}-{int(batch_id):04d}"


def _seed(conn: sqlite3.Connection) -> None:
    for zone, name, biz, status in SEED_BATCHES:
        conn.execute(
            "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
            "VALUES (?,?,?,?,?)",
            (zone, name, biz, status, 2 if status == "已闭合" else None),
        )
    closed = conn.execute(
        "SELECT id FROM landing_batches WHERE status='已闭合'"
    ).fetchone()
    if closed:
        bid = closed["id"]
        conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (bid, "part-000.parquet", "hash-silver-a", 1024, "已闭合"),
        )
        conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?,?,?,?)",
            (bid, "part-001.parquet", "hash-silver-b", 2048, "已闭合"),
        )
        conn.execute(
            "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
            (bid, 2, "种子闭合"),
        )
    conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：历史重复记录去重、加唯一硬闸、为既有闭合批补凭证。"""
    # 1) 封闭历史双写：同一批只留最早一条编排闭合记录，再加唯一索引。
    conn.execute(
        "DELETE FROM close_runs WHERE id NOT IN "
        "(SELECT MIN(id) FROM close_runs GROUP BY batch_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_close_runs_batch ON close_runs(batch_id)"
    )

    # 2) 已闭合但缺正式凭证的批（种子 / 老库）补一条，保证回看台只见一条正式闭合。
    missing = conn.execute(
        "SELECT b.* FROM landing_batches b "
        "LEFT JOIN close_vouchers v ON v.batch_id = b.id "
        "WHERE b.status = '已闭合' AND v.id IS NULL ORDER BY b.id"
    ).fetchall()
    for batch in missing:
        n = batch["closed_object_count"]
        if n is None:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM landing_objects "
                "WHERE batch_id=? AND status='已闭合'",
                (batch["id"],),
            ).fetchone()["c"]
        run = conn.execute(
            "SELECT MIN(id) AS rid FROM close_runs WHERE batch_id=?",
            (batch["id"],),
        ).fetchone()
        run_id = run["rid"]
        if run_id is None:
            cur = conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) "
                "VALUES (?,?,1,?)",
                (batch["id"], n, "迁移补录闭合"),
            )
            run_id = cur.lastrowid
        conn.execute(
            "INSERT INTO close_vouchers(batch_id, voucher_no, object_count, operator, queue_id, run_id) "
            "VALUES (?,?,?,?,NULL,?)",
            (batch["id"], make_voucher_no(batch["biz_date"], batch["id"]), n, "迁移补录", run_id),
        )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            _seed(conn)
        _migrate(conn)
