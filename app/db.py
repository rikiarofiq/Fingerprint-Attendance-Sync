import os
import time
from contextlib import contextmanager

import pymysql

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASS", ""),
    database=os.getenv("DB_NAME", "absensi"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False,
)

DEVICES_DDL = """CREATE TABLE IF NOT EXISTS devices (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    ip VARCHAR(64) NOT NULL,
    port INT NOT NULL DEFAULT 4370,
    comm_password INT NOT NULL DEFAULT 0,
    interval_minutes INT NOT NULL DEFAULT 10,
    active TINYINT(1) NOT NULL DEFAULT 1,
    serial VARCHAR(50) NULL,
    mac VARCHAR(32) NULL,
    firmware VARCHAR(100) NULL,
    last_sync_at DATETIME NULL,
    next_sync_at DATETIME NULL,
    last_status VARCHAR(20) NULL,
    last_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_device_addr (ip, port)
)"""

# Penanda sync: jumlah scan terakhir di mesin, sync penuh terakhir, user perlu di-sync
DEVICE_EXTRA_COLS = [
    ("last_record_count", "INT NULL"),
    ("last_full_sync_at", "DATETIME NULL"),
    ("last_user_sync_at", "DATETIME NULL"),
    ("users_due", "TINYINT(1) NOT NULL DEFAULT 1"),
]

SCHEMA = [
    # Karyawan dipisah per device (ID bisa beda antar mesin untuk orang yang sama)
    """CREATE TABLE IF NOT EXISTS employees (
        device_id INT NOT NULL,
        user_id VARCHAR(20) NOT NULL,
        name VARCHAR(100),
        privilege INT,
        card VARCHAR(20),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (device_id, user_id)
    )""",
    # UNIQUE (device_id, user_id, scan_time): 1 scan di 1 mesin hanya tersimpan sekali
    """CREATE TABLE IF NOT EXISTS attendance (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        device_id INT NOT NULL,
        user_id VARCHAR(20) NOT NULL,
        scan_time DATETIME NOT NULL,
        status INT,
        punch INT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_scan (device_id, user_id, scan_time),
        KEY idx_time (scan_time),
        KEY idx_user (user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS sync_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        device_id INT NULL,
        trigger_type VARCHAR(20) NOT NULL,
        started_at DATETIME NOT NULL,
        finished_at DATETIME NULL,
        status VARCHAR(20) NOT NULL,
        fetched INT DEFAULT 0,
        inserted INT DEFAULT 0,
        message TEXT,
        KEY idx_dev (device_id)
    )""",
]


@contextmanager
def get_conn():
    conn = pymysql.connect(**DB)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s",
        (table,),
    )
    return cur.fetchone()["n"] > 0


def _col_exists(cur, table, col):
    cur.execute(
        """SELECT COUNT(*) n FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s""",
        (table, col),
    )
    return cur.fetchone()["n"] > 0


def _migrate_single_device(cur):
    """Versi lama (1 device, tabel settings, attendance.device_sn) -> versi multi device."""
    if not (_table_exists(cur, "attendance") and _col_exists(cur, "attendance", "device_sn")):
        return
    ip, port, pwd, interval = "192.168.18.200", 4370, 0, 10
    if _table_exists(cur, "settings"):
        cur.execute("SELECT * FROM settings WHERE id=1")
        s = cur.fetchone()
        if s:
            ip, port, pwd, interval = s["device_ip"], s["device_port"], s["comm_password"], s["interval_minutes"]
    cur.execute("SELECT device_sn FROM attendance LIMIT 1")
    row = cur.fetchone()
    cur.execute(
        """INSERT INTO devices (name, ip, port, comm_password, interval_minutes, serial)
           VALUES ('Device 1', %s, %s, %s, %s, %s)""",
        (ip, port, pwd, interval, row["device_sn"] if row else None),
    )
    dev = cur.lastrowid

    cur.execute("ALTER TABLE attendance ADD COLUMN device_id INT NULL")
    cur.execute("UPDATE attendance SET device_id=%s", (dev,))
    cur.execute(
        """ALTER TABLE attendance DROP INDEX uq_scan, DROP COLUMN device_sn,
           MODIFY device_id INT NOT NULL, ADD UNIQUE KEY uq_scan (device_id, user_id, scan_time)"""
    )
    if _table_exists(cur, "employees") and not _col_exists(cur, "employees", "device_id"):
        cur.execute(f"ALTER TABLE employees ADD COLUMN device_id INT NOT NULL DEFAULT {int(dev)} FIRST")
        cur.execute("ALTER TABLE employees DROP PRIMARY KEY, ADD PRIMARY KEY (device_id, user_id)")
        cur.execute("ALTER TABLE employees ALTER COLUMN device_id DROP DEFAULT")
    if _table_exists(cur, "sync_history") and not _col_exists(cur, "sync_history", "device_id"):
        cur.execute("ALTER TABLE sync_history ADD COLUMN device_id INT NULL AFTER id, ADD KEY idx_dev (device_id)")
        cur.execute("UPDATE sync_history SET device_id=%s", (dev,))
    cur.execute("DROP TABLE IF EXISTS settings")


def init_db(retries=30):
    for i in range(retries):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(DEVICES_DDL)
                _migrate_single_device(cur)
                for col, ddl in DEVICE_EXTRA_COLS:
                    if not _col_exists(cur, "devices", col):
                        cur.execute(f"ALTER TABLE devices ADD COLUMN {col} {ddl}")
                for stmt in SCHEMA:
                    cur.execute(stmt)
            return
        except pymysql.err.OperationalError:
            if i == retries - 1:
                raise
            time.sleep(2)
