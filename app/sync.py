"""Logika tarik data dari banyak mesin ZKTeco (port 4370) ke MySQL."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from zk import ZK

from db import get_conn

CHUNK = 1000
MAX_PARALLEL = 4  # maksimal device yang di-sync bersamaan

_pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL)
_active = set()  # device_id yang sedang antri / berjalan -> 1 device tidak pernah di-sync dobel
_guard = threading.Lock()


def running_ids():
    with _guard:
        return set(_active)


def _claim(device_id):
    with _guard:
        if device_id in _active:
            return False
        _active.add(device_id)
        return True


def _release(device_id):
    with _guard:
        _active.discard(device_id)


def get_device(device_id):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM devices WHERE id=%s", (device_id,))
        return cur.fetchone()


def _connect(d):
    return ZK(d["ip"], port=d["port"], timeout=15, password=d["comm_password"], ommit_ping=True).connect()


def _device_info(conn):
    try:
        mac = conn.get_mac()
    except Exception:  # noqa: BLE001
        mac = None
    return conn.get_serialnumber(), mac, conn.get_firmware_version()


def _save_info(device_id, serial, mac, fw):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE devices SET serial=%s, mac=%s, firmware=%s WHERE id=%s", (serial, mac, fw, device_id))


def test_connection(device_id):
    """Tes konek + simpan SN/MAC/firmware. Raise kalau gagal / device sedang sync."""
    if not _claim(device_id):
        raise RuntimeError("device sedang sync, coba lagi setelah selesai")
    try:
        d = get_device(device_id)
        conn = _connect(d)
        try:
            serial, mac, fw = _device_info(conn)
            _save_info(device_id, serial, mac, fw)
            return {"serial": serial, "mac": mac, "firmware": fw, "time": str(conn.get_time())}
        finally:
            conn.disconnect()
    finally:
        _release(device_id)


FULL_SYNC_EVERY = timedelta(hours=24)  # sync penuh sebagai pengaman
OVERLAP = timedelta(days=1)            # sync cepat menyimpan scan sejak (scan terakhir - 1 hari)


def _do_sync(device_id, trigger, what, date_from, date_to):
    """Worker thread. device_id sudah di-claim oleh submit().

    what='users'      : hanya sync user (karyawan)
    what='attendance' : sync absensi manual (opsional filter tanggal), tanpa user
    trigger='auto'    : sync cepat -> cek jumlah scan dulu, unduh hanya kalau berubah / sudah 24 jam
                        (+ sync user kalau users_due=1)
    """
    started = datetime.now()
    res = {"status": "success", "fetched": 0, "inserted": 0, "message": "", "skipped": False}
    try:
        d = get_device(device_id)
        conn = _connect(d)
        users = logs = None
        full = False
        try:
            serial, mac, fw = _device_info(conn)
            conn.read_sizes()  # murah (~0,07 dtk): ambil jumlah scan di mesin
            dev_count = conn.records

            if what == "users":
                do_users, do_att = True, False
            elif trigger == "auto":
                do_users = bool(d["users_due"])
                forced = d["last_full_sync_at"] is None or started - d["last_full_sync_at"] >= FULL_SYNC_EVERY
                changed = d["last_record_count"] is None or d["last_record_count"] != dev_count
                do_att = forced or changed
                full = forced
            else:
                do_users, do_att = False, True
                full = not date_from and not date_to

            if do_users or do_att:
                conn.disable_device()  # kunci mesin sebentar saat baca data
                try:
                    if do_users:
                        users = conn.get_users()
                    if do_att:
                        logs = conn.get_attendance()
                finally:
                    conn.enable_device()
        finally:
            conn.disconnect()
        _save_info(device_id, serial, mac, fw)

        if users is None and logs is None:
            res.update(skipped=True, message=f"Tidak ada scan baru (jumlah di mesin: {dev_count})")
        else:
            parts = []
            sets, args = [], []
            with get_conn() as c, c.cursor() as cur:
                if users is not None:
                    cur.executemany(
                        """INSERT INTO employees (device_id, user_id, name, privilege, card) VALUES (%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE name=VALUES(name), privilege=VALUES(privilege), card=VALUES(card)""",
                        [(device_id, u.user_id, u.name, u.privilege, str(u.card)) for u in users],
                    )
                    parts.append(f"{len(users)} user")
                    sets += ["users_due=0", "last_user_sync_at=%s"]
                    args.append(started)
                if logs is not None:
                    if trigger == "manual" and (date_from or date_to):
                        if date_from:
                            logs = [a for a in logs if a.timestamp >= date_from]
                        if date_to:
                            end = date_to + timedelta(days=1)
                            logs = [a for a in logs if a.timestamp < end]
                    elif not full:  # sync cepat: hanya scan sejak scan terakhir yang tersimpan
                        cur.execute("SELECT MAX(scan_time) m FROM attendance WHERE device_id=%s", (device_id,))
                        last = cur.fetchone()["m"]
                        if last:
                            since = last - OVERLAP
                            logs = [a for a in logs if a.timestamp >= since]
                    rows = [(device_id, a.user_id, a.timestamp, a.status, a.punch) for a in logs]
                    inserted = 0
                    for i in range(0, len(rows), CHUNK):
                        # INSERT IGNORE + UNIQUE KEY => scan yang sudah ada dilewati
                        cur.executemany(
                            """INSERT IGNORE INTO attendance (device_id, user_id, scan_time, status, punch)
                               VALUES (%s,%s,%s,%s,%s)""",
                            rows[i : i + CHUNK],
                        )
                        inserted += cur.rowcount
                    res.update(fetched=len(rows), inserted=inserted)
                    parts.append(f"{len(rows)} scan dibaca, {inserted} scan baru disimpan")
                    # penanda hanya maju kalau tidak ada filter tanggal manual (supaya tidak ada data terlewat)
                    if not (trigger == "manual" and (date_from or date_to)):
                        sets.append("last_record_count=%s")
                        args.append(dev_count)
                    if full:
                        sets.append("last_full_sync_at=%s")
                        args.append(started)
                if sets:
                    cur.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=%s", args + [device_id])
            res["message"] = ("[penuh] " if full and logs is not None else "") + ", ".join(parts)
    except Exception as e:  # noqa: BLE001
        res.update(status="failed", message=f"{type(e).__name__}: {e}")
    finally:
        try:
            now = datetime.now()
            with get_conn() as c, c.cursor() as cur:
                # sync cepat yang tidak ada data baru tidak dicatat ke riwayat (supaya tidak penuh)
                if not res["skipped"]:
                    cur.execute(
                        """INSERT INTO sync_history (device_id, trigger_type, started_at, finished_at, status,
                           fetched, inserted, message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (device_id, "users" if what == "users" else trigger, started, now,
                         res["status"], res["fetched"], res["inserted"], res["message"]),
                    )
                cur.execute(
                    """UPDATE devices SET last_sync_at=%s, last_status=%s, last_message=%s,
                       next_sync_at=DATE_ADD(%s, INTERVAL interval_minutes MINUTE) WHERE id=%s""",
                    (now, res["status"], res["message"], now, device_id),
                )
        finally:
            _release(device_id)


def submit(device_id, trigger="manual", what="attendance", date_from=None, date_to=None):
    """Antrikan sync 1 device. False kalau device itu sudah antri/berjalan."""
    if not _claim(device_id):
        return False
    try:
        _pool.submit(_do_sync, device_id, trigger, what, date_from, date_to)
    except Exception:
        _release(device_id)
        raise
    return True


def submit_all(trigger="manual", what="attendance", date_from=None, date_to=None):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT id FROM devices WHERE active=1")
        ids = [r["id"] for r in cur.fetchall()]
    return sum(1 for i in ids if submit(i, trigger, what, date_from, date_to)), len(ids)


def tick():
    """Dipanggil scheduler tiap 30 detik: sync device aktif yang jatuh tempo (interval per device)
    atau yang user-nya perlu di-sync (users_due)."""
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT id FROM devices WHERE active=1 AND (next_sync_at IS NULL OR next_sync_at <= NOW() OR users_due=1)")
        ids = [r["id"] for r in cur.fetchall()]
    for i in ids:
        submit(i, "auto")


def mark_users_due():
    """Dipanggil jam 00:00 & 12:00: tandai semua device aktif supaya user-nya di-sync."""
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE devices SET users_due=1 WHERE active=1")
