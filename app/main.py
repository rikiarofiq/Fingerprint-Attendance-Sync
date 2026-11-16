import os
from datetime import datetime

import pymysql
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

import sync
from db import get_conn, init_db

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

init_db()
scheduler = BackgroundScheduler()
# Cek tiap 30 detik device mana yang sudah waktunya sync (interval diatur per device)
scheduler.add_job(sync.tick, "interval", seconds=30, id="tick", max_instances=1,
                  coalesce=True, next_run_time=datetime.now())
# Sync user (nama karyawan) otomatis tengah malam & tengah hari
scheduler.add_job(sync.mark_users_due, "cron", hour="0,12", minute=0, id="users_due")
scheduler.start()


def parse_date(v):
    return datetime.strptime(v, "%Y-%m-%d") if v else None


def fetch_devices():
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM devices ORDER BY name")
        return cur.fetchall()


def status_payload():
    running = sync.running_ids()
    return {"running": len(running) > 0, "running_ids": sorted(running)}


@app.context_processor
def inject_status():
    return {"status": status_payload()}


@app.get("/api/status")
def api_status():
    return jsonify(status_payload())


# ---------------------------------------------------------------- Dashboard
@app.route("/")
def dashboard():
    f_from = request.args.get("from") or datetime.now().strftime("%Y-%m-%d")
    f_to = request.args.get("to") or f_from
    q = request.args.get("q", "").strip()
    dev = request.args.get("device", "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 100

    where = ["a.scan_time >= %s", "a.scan_time < DATE_ADD(%s, INTERVAL 1 DAY)"]
    params = [f_from, f_to]
    if dev.isdigit():
        where.append("a.device_id = %s")
        params.append(int(dev))
    if q:
        where.append("(a.user_id = %s OR e.name LIKE %s)")
        params += [q, f"%{q}%"]
    w = " AND ".join(where)
    frm = """FROM attendance a
             JOIN devices d ON d.id = a.device_id
             LEFT JOIN employees e ON e.device_id = a.device_id AND e.user_id = a.user_id"""

    with get_conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) n {frm} WHERE {w}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT a.*, e.name, d.name device_name, d.ip device_ip, d.mac device_mac
                {frm} WHERE {w} ORDER BY a.scan_time DESC LIMIT %s OFFSET %s""",
            params + [per_page, (page - 1) * per_page],
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT (SELECT COUNT(*) FROM attendance) logs,
                      (SELECT COUNT(*) FROM devices) devs,
                      (SELECT COUNT(*) FROM devices WHERE active=1) devs_active,
                      (SELECT COUNT(*) FROM devices WHERE last_status='failed') devs_failed"""
        )
        stats = cur.fetchone()
        cur.execute(
            """SELECT h.*, d.name device_name FROM sync_history h
               LEFT JOIN devices d ON d.id = h.device_id ORDER BY h.id DESC LIMIT 15"""
        )
        history = cur.fetchall()

    return render_template(
        "dashboard.html", rows=rows, total=total, page=page, per_page=per_page,
        f_from=f_from, f_to=f_to, q=q, dev=dev, stats=stats, history=history,
        devices=fetch_devices(),
    )


@app.post("/sync")
def manual_sync():
    """Sync manual: 1 device (device_id) atau semua device aktif (kosong)."""
    try:
        d_from = parse_date(request.form.get("sync_from"))
        d_to = parse_date(request.form.get("sync_to"))
    except ValueError:
        flash("Format tanggal salah", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    dev = request.form.get("device_id", "")
    if dev.isdigit():
        ok = sync.submit(int(dev), "manual", "attendance", d_from, d_to)
        flash("Sync dimulai." if ok else "Device ini sedang sync, tunggu sampai selesai.",
              "info" if ok else "warning")
    else:
        started, total = sync.submit_all("manual", "attendance", d_from, d_to)
        if total == 0:
            flash("Belum ada device aktif. Tambahkan di Master Device.", "warning")
        else:
            flash(f"Sync dimulai untuk {started} dari {total} device aktif"
                  + ("" if started == total else " (sisanya sedang sync)") + ".", "info")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/sync-users")
def manual_sync_users():
    """Sync user (nama karyawan) manual: 1 device atau semua device aktif."""
    dev = request.form.get("device_id", "")
    if dev.isdigit():
        ok = sync.submit(int(dev), "manual", "users")
        flash("Sync user dimulai." if ok else "Device ini sedang sync, tunggu sampai selesai.",
              "info" if ok else "warning")
    else:
        started, total = sync.submit_all("manual", "users")
        flash(f"Sync user dimulai untuk {started} dari {total} device aktif.", "info" if total else "warning")
    return redirect(request.referrer or url_for("devices_page"))


# ------------------------------------------------------------ Master device
def read_device_form():
    name = request.form["name"].strip()
    ip = request.form["ip"].strip()
    port = int(request.form.get("port") or 4370)
    pwd = int(request.form.get("comm_password") or 0)
    interval = int(request.form["interval_minutes"])
    if not name or not ip or interval < 1 or not (1 <= port <= 65535):
        raise ValueError
    return name, ip, port, pwd, interval, 1 if request.form.get("active") else 0


@app.get("/devices")
def devices_page():
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT d.*, (SELECT COUNT(*) FROM attendance a WHERE a.device_id=d.id) scans
               FROM devices d ORDER BY d.name"""
        )
        devices = cur.fetchall()
    return render_template("devices.html", devices=devices, running_ids=sync.running_ids())


@app.post("/devices")
def device_add():
    try:
        name, ip, port, pwd, interval, active = read_device_form()
    except (KeyError, ValueError):
        flash("Input tidak valid (nama & IP wajib, interval minimal 1 menit).", "danger")
        return redirect(url_for("devices_page"))
    try:
        with get_conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO devices (name, ip, port, comm_password, interval_minutes, active)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (name, ip, port, pwd, interval, active),
            )
            new_id = cur.lastrowid
    except pymysql.err.IntegrityError:
        flash(f"Device dengan IP {ip}:{port} sudah ada.", "danger")
        return redirect(url_for("devices_page"))
    flash(f"Device '{name}' ditambahkan. Sync pertama berjalan otomatis maksimal 30 detik lagi.", "success")
    try:  # isi SN / MAC / firmware langsung kalau device terjangkau
        sync.test_connection(new_id)
    except Exception:  # noqa: BLE001
        pass
    return redirect(url_for("devices_page"))


@app.route("/devices/<int:device_id>/edit", methods=["GET", "POST"])
def device_edit(device_id):
    d = sync.get_device(device_id)
    if not d:
        flash("Device tidak ditemukan.", "danger")
        return redirect(url_for("devices_page"))
    if request.method == "POST":
        try:
            name, ip, port, pwd, interval, active = read_device_form()
        except (KeyError, ValueError):
            flash("Input tidak valid.", "danger")
            return redirect(url_for("device_edit", device_id=device_id))
        try:
            with get_conn() as c, c.cursor() as cur:
                # next_sync_at diatur ulang supaya interval baru langsung berlaku
                cur.execute(
                    """UPDATE devices SET name=%s, ip=%s, port=%s, comm_password=%s, interval_minutes=%s,
                       active=%s, next_sync_at=DATE_ADD(NOW(), INTERVAL %s MINUTE) WHERE id=%s""",
                    (name, ip, port, pwd, interval, active, interval, device_id),
                )
        except pymysql.err.IntegrityError:
            flash(f"Device dengan IP {ip}:{port} sudah ada.", "danger")
            return redirect(url_for("device_edit", device_id=device_id))
        flash("Device diperbarui.", "success")
        return redirect(url_for("devices_page"))
    return render_template("device_edit.html", d=d)


@app.post("/devices/<int:device_id>/test")
def device_test(device_id):
    try:
        info = sync.test_connection(device_id)
        flash(f"Terhubung! SN: {info['serial']}, MAC: {info['mac']}, Firmware: {info['firmware']}, "
              f"jam mesin: {info['time']}", "success")
    except Exception as e:  # noqa: BLE001
        flash(f"Gagal konek: {e}", "danger")
    return redirect(url_for("devices_page"))


@app.post("/devices/<int:device_id>/toggle")
def device_toggle(device_id):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE devices SET active = 1 - active, next_sync_at = NULL WHERE id=%s", (device_id,))
    return redirect(url_for("devices_page"))


@app.post("/devices/<int:device_id>/delete")
def device_delete(device_id):
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM attendance WHERE device_id=%s", (device_id,))
        if cur.fetchone()["n"] > 0:
            flash("Device sudah punya data absensi, tidak bisa dihapus. Nonaktifkan saja.", "warning")
            return redirect(url_for("devices_page"))
        if device_id in sync.running_ids():
            flash("Device sedang sync, coba lagi nanti.", "warning")
            return redirect(url_for("devices_page"))
        cur.execute("DELETE FROM employees WHERE device_id=%s", (device_id,))
        cur.execute("DELETE FROM sync_history WHERE device_id=%s", (device_id,))
        cur.execute("DELETE FROM devices WHERE id=%s", (device_id,))
    flash("Device dihapus.", "success")
    return redirect(url_for("devices_page"))


@app.get("/settings")
def settings_redirect():
    return redirect(url_for("devices_page"))
