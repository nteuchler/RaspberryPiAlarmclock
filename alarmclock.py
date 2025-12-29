#!/usr/bin/env python3
# File: alarmclock.py

import os
import json
import time
import sqlite3
import subprocess
import threading
import random
import glob
import logging
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template
from gpiozero import Button

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "alarmclock.db")

ALARMTONES_DIR = os.path.join(APP_DIR, "alarmtones")

BUTTON_GPIO = 17  # BCM numbering
button = Button(BUTTON_GPIO, pull_up=True, bounce_time=0.05)

LOG_LEVEL = os.environ.get("ALARM_LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("alarmclock")

# ---- LOCKED life expectancy (not editable via UI) ----
LIFE_EXPECTANCY_YEARS_LOCKED = 79.9

DEFAULT_SETTINGS = {
    "volume_percent": 85,  # raised default
    "alarm_tone_path": os.path.join(APP_DIR, "media", "alarmtone.mp3"),
    "content_type": "stream",
    "content_value": "http://live-icy.gss.dr.dk/A/A05H.mp3",
    "birthdate": "1999-04-15",
}

# VLC volume: 0..512 (256 ~ 100% in VLC terms)
VLC_VOLUME = 256

player_lock = threading.Lock()
player_proc = None
current_mode = "idle"  # idle | alarm | content

app = Flask(__name__)


def normalize_hhmm(value: str) -> str:
    if value is None:
        raise ValueError("missing time")
    value = str(value).strip()
    parts = value.split(":")
    if len(parts) < 2:
        raise ValueError("time must be HH:MM")
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("invalid hour/minute")
    return f"{hh:02d}:{mm:02d}"


def pick_random_alarmtone() -> str:
    candidates = glob.glob(os.path.join(ALARMTONES_DIR, "*.mp3"))
    if candidates:
        choice = random.choice(candidates)
        log.debug("Picked random alarm tone: %s", choice)
        return choice
    fallback = get_setting("alarm_tone_path")
    log.debug("No mp3 in alarmtones/. Using fallback: %s", fallback)
    return fallback


def tz_hint() -> str:
    try:
        return time.tzname[0]
    except Exception:
        return "unknown"


# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_hhmm TEXT NOT NULL,
            days_mask INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_fired_date TEXT
        )
        """
    )
    conn.commit()

    for k, v in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings(k,v) VALUES (?,?)", (k, json.dumps(v)))
    conn.commit()
    conn.close()

    log.info("DB ready at %s", DB_PATH)
    log.info("Timezone: %s (tzname=%s)", tz_hint(), getattr(time, "tzname", None))
    log.info("Alarm tones dir: %s", ALARMTONES_DIR)


def get_setting(key):
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    conn.close()
    if not row:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row["v"])


def set_setting(key, value):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def list_alarms():
    conn = db()
    rows = conn.execute("SELECT * FROM alarms ORDER BY time_hhmm").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_alarm(time_hhmm, days_mask, enabled=True):
    conn = db()
    conn.execute(
        "INSERT INTO alarms(time_hhmm, days_mask, enabled, last_fired_date) VALUES (?,?,?,NULL)",
        (time_hhmm, int(days_mask), 1 if enabled else 0),
    )
    conn.commit()
    conn.close()
    log.info("Created alarm time=%s mask=%s enabled=%s", time_hhmm, int(days_mask), bool(enabled))


def update_alarm(alarm_id, time_hhmm=None, days_mask=None, enabled=None):
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    if not row:
        conn.close()
        return False

    old_time = row["time_hhmm"]
    old_mask = int(row["days_mask"])
    old_enabled = int(row["enabled"])

    new_time = time_hhmm if time_hhmm is not None else old_time
    new_mask = int(days_mask) if days_mask is not None else old_mask
    new_enabled = (1 if enabled else 0) if enabled is not None else old_enabled

    schedule_changed = (new_time != old_time) or (new_mask != old_mask)
    enabled_changed = (new_enabled != old_enabled)

    # Critical fix: if schedule/enabled changes, allow it to fire again today.
    if schedule_changed or (enabled_changed and new_enabled == 1):
        new_last_fired = None
        log.info(
            "Resetting last_fired_date due to update (id=%s schedule_changed=%s enabled_changed=%s)",
            alarm_id, schedule_changed, enabled_changed
        )
    else:
        new_last_fired = row["last_fired_date"]

    cur.execute(
        "UPDATE alarms SET time_hhmm=?, days_mask=?, enabled=?, last_fired_date=? WHERE id=?",
        (new_time, new_mask, new_enabled, new_last_fired, alarm_id),
    )
    conn.commit()
    conn.close()

    log.info("Updated alarm id=%s time=%s mask=%s enabled=%s", alarm_id, new_time, new_mask, bool(new_enabled))
    return True


def delete_alarm(alarm_id):
    conn = db()
    conn.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
    conn.commit()
    conn.close()
    log.info("Deleted alarm id=%s", alarm_id)


def set_last_fired(alarm_id, fired_date_str):
    conn = db()
    conn.execute("UPDATE alarms SET last_fired_date=? WHERE id=?", (fired_date_str, alarm_id))
    conn.commit()
    conn.close()


# ---------- Audio ----------
def _run_amixer(control: str, percent: int):
    cmd = ["amixer", "sset", control, f"{percent}%"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        log.debug("amixer ok: %s", " ".join(cmd))
        return True
    log.debug("amixer fail: %s stderr=%r", " ".join(cmd), (p.stderr or "").strip())
    return False


def set_system_volume(percent: int):
    percent = max(0, min(100, int(percent)))
    log.info("Setting volume to %s%% (amixer + VLC)", percent)

    # Try multiple common controls (USB cards often don't have Master)
    ok_any = False
    for ctl in ["Master", "PCM", "Speaker", "Headphone"]:
        ok_any = _run_amixer(ctl, percent) or ok_any

    if not ok_any:
        log.warning("No amixer controls adjusted (Master/PCM/Speaker/Headphone all failed). Audio may be quiet.")


def stop_audio():
    global player_proc, current_mode
    with player_lock:
        if player_proc and player_proc.poll() is None:
            log.info("Stopping audio (mode=%s, pid=%s)", current_mode, player_proc.pid)
            try:
                player_proc.terminate()
                player_proc.wait(timeout=2)
            except Exception:
                try:
                    player_proc.kill()
                except Exception:
                    pass
        player_proc = None
        current_mode = "idle"


def play_vlc(target: str, loop: bool):
    global player_proc
    args = ["cvlc", "--no-video", "--volume", str(VLC_VOLUME)]
    if LOG_LEVEL not in ("DEBUG", "INFO"):
        args += ["--quiet"]
    if loop:
        args += ["--loop"]
    args += [target]

    with player_lock:
        if player_proc and player_proc.poll() is None:
            stop_audio()
        log.info("Starting VLC loop=%s target=%s", loop, target)
        if LOG_LEVEL in ("DEBUG", "INFO"):
            player_proc = subprocess.Popen(args)
        else:
            player_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_alarm():
    global current_mode
    set_system_volume(get_setting("volume_percent"))
    tone = pick_random_alarmtone()
    play_vlc(tone, loop=True)
    current_mode = "alarm"
    log.info("Mode -> alarm")


def start_content():
    global current_mode
    set_system_volume(get_setting("volume_percent"))
    cval = get_setting("content_value")
    log.info("Starting content value=%s", cval)
    play_vlc(cval, loop=False)
    current_mode = "content"
    log.info("Mode -> content")


# ---------- Death clock ----------
def hours_remaining_until_expected_death():
    birth_s = get_setting("birthdate")
    life_years = LIFE_EXPECTANCY_YEARS_LOCKED

    b = datetime.strptime(birth_s, "%Y-%m-%d").date()
    days = life_years * 365.2425
    expected_death = datetime.combine(b, datetime.min.time()) + timedelta(days=days)
    now = datetime.now()
    delta = expected_death - now
    return expected_death, delta.total_seconds() / 3600.0


# ---------- Scheduler ----------
def alarm_scheduled_datetime_for_today(time_hhmm: str, now_dt: datetime) -> datetime:
    hh, mm = [int(x) for x in time_hhmm.split(":")]
    return now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def scheduler_thread():
    log.info("Scheduler thread started")
    last_tick = datetime.now() - timedelta(seconds=2)
    last_minute = None

    while True:
        try:
            now_dt = datetime.now()

            if last_minute != (now_dt.year, now_dt.month, now_dt.day, now_dt.hour, now_dt.minute):
                last_minute = (now_dt.year, now_dt.month, now_dt.day, now_dt.hour, now_dt.minute)
                log.debug(
                    "Tick now=%s tz=%s last_tick=%s",
                    now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    tz_hint(),
                    last_tick.strftime("%Y-%m-%d %H:%M:%S"),
                )

            alarms = list_alarms()
            today_s = now_dt.date().isoformat()
            wd = now_dt.weekday()  # Mon=0..Sun=6

            for a in alarms:
                if int(a["enabled"]) != 1:
                    continue

                mask = int(a["days_mask"])
                if (mask & (1 << wd)) == 0:
                    continue

                if a["last_fired_date"] == today_s:
                    continue

                try:
                    scheduled = alarm_scheduled_datetime_for_today(a["time_hhmm"], now_dt)
                except Exception:
                    log.warning("Bad time_hhmm for alarm id=%s: %r", a.get("id"), a.get("time_hhmm"))
                    continue

                # Fire when we CROSS the scheduled time
                if last_tick < scheduled <= now_dt:
                    log.info(
                        "ALARM DUE: id=%s scheduled=%s now=%s mask=%s",
                        a["id"],
                        scheduled.strftime("%Y-%m-%d %H:%M:%S"),
                        now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        mask,
                    )
                    set_last_fired(a["id"], today_s)
                    start_alarm()
                    break

            last_tick = now_dt

        except Exception:
            log.exception("Scheduler exception")

        time.sleep(0.25)


# ---------- Button ----------
def on_button_pressed():
    global current_mode
    log.info("Button pressed (mode=%s)", current_mode)
    if current_mode == "alarm":
        stop_audio()
        start_content()
    elif current_mode == "content":
        stop_audio()


button.when_pressed = on_button_pressed


# ---------- Web ----------
@app.route("/")
def index():
    expected_death, hrs = hours_remaining_until_expected_death()
    hrs_int = max(0, int(hrs))
    death_text = f"Du har kun {hrs_int} timer tilbage af livet, brug ikke dem alle på at sove"

    return render_template(
        "index.html",
        alarms=list_alarms(),
        settings={
            "volume_percent": get_setting("volume_percent"),
            "alarm_tone_path": get_setting("alarm_tone_path"),
            "content_type": get_setting("content_type"),
            "content_value": get_setting("content_value"),
            "birthdate": get_setting("birthdate"),
            "life_expectancy_years_locked": LIFE_EXPECTANCY_YEARS_LOCKED,
        },
        expected_death=expected_death.strftime("%Y-%m-%d %H:%M"),
        expected_death_epoch=int(expected_death.timestamp()),
        death_text=death_text,
        mode=current_mode,
        server_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tz=tz_hint(),
    )


@app.route("/api/debug/status", methods=["GET"])
def api_debug_status():
    now_dt = datetime.now()
    return jsonify(
        {
            "server_time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "tzname": getattr(time, "tzname", None),
            "tz_hint": tz_hint(),
            "mode": current_mode,
            "alarms": list_alarms(),
            "settings": {
                "volume_percent": get_setting("volume_percent"),
                "content_type": get_setting("content_type"),
                "content_value": get_setting("content_value"),
                "life_expectancy_years_locked": LIFE_EXPECTANCY_YEARS_LOCKED,
            },
        }
    )


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(
            {
                "volume_percent": get_setting("volume_percent"),
                "alarm_tone_path": get_setting("alarm_tone_path"),
                "content_type": get_setting("content_type"),
                "content_value": get_setting("content_value"),
                "birthdate": get_setting("birthdate"),
                "life_expectancy_years_locked": LIFE_EXPECTANCY_YEARS_LOCKED,
            }
        )

    data = request.get_json(force=True) or {}
    log.debug("POST /api/settings payload=%s", data)

    old_content_value = get_setting("content_value")

    # IMPORTANT: life expectancy is locked; ignore any incoming field for it.
    for k in ["volume_percent", "alarm_tone_path", "content_type", "content_value", "birthdate"]:
        if k in data:
            set_setting(k, data[k])

    if "volume_percent" in data:
        set_system_volume(int(get_setting("volume_percent")))

    new_content_value = get_setting("content_value")
    if current_mode == "content" and new_content_value != old_content_value:
        log.info("Content changed while playing; restarting content")
        stop_audio()
        start_content()

    return jsonify({"ok": True})


@app.route("/api/alarms", methods=["GET", "POST"])
def api_alarms():
    if request.method == "GET":
        return jsonify(list_alarms())

    data = request.get_json(force=True) or {}
    log.debug("POST /api/alarms payload=%s", data)

    try:
        time_hhmm = normalize_hhmm(data.get("time_hhmm"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad time_hhmm: {e}"}), 400

    try:
        days_mask = int(data.get("days_mask", 0))
    except Exception:
        return jsonify({"ok": False, "error": "bad days_mask"}), 400

    if days_mask <= 0 or days_mask > 127:
        return jsonify({"ok": False, "error": "days_mask must be 1..127"}), 400

    enabled = bool(data.get("enabled", True))
    create_alarm(time_hhmm, days_mask, enabled)
    return jsonify({"ok": True})


@app.route("/api/alarms/<int:alarm_id>", methods=["PATCH", "DELETE"])
def api_alarm_one(alarm_id):
    if request.method == "DELETE":
        delete_alarm(alarm_id)
        return jsonify({"ok": True})

    data = request.get_json(force=True) or {}
    log.debug("PATCH /api/alarms/%s payload=%s", alarm_id, data)

    time_hhmm = data.get("time_hhmm")
    if time_hhmm is not None:
        try:
            time_hhmm = normalize_hhmm(time_hhmm)
        except Exception as e:
            return jsonify({"ok": False, "error": f"bad time_hhmm: {e}"}), 400

    if "days_mask" in data:
        try:
            dm = int(data.get("days_mask"))
        except Exception:
            return jsonify({"ok": False, "error": "bad days_mask"}), 400
        if dm <= 0 or dm > 127:
            return jsonify({"ok": False, "error": "days_mask must be 1..127"}), 400

    ok = update_alarm(
        alarm_id,
        time_hhmm=time_hhmm,
        days_mask=data.get("days_mask"),
        enabled=data.get("enabled"),
    )
    return jsonify({"ok": ok})


@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.get_json(force=True) or {}
    action = data.get("action")
    log.debug("POST /api/control action=%s", action)

    if action == "test_alarm":
        start_alarm()
        return jsonify({"ok": True})
    if action == "test_content":
        stop_audio()
        start_content()
        return jsonify({"ok": True})
    if action == "stop":
        stop_audio()
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "unknown action"}), 400


def main():
    init_db()
    t = threading.Thread(target=scheduler_thread, daemon=True, name="scheduler")
    t.start()
    log.info("Starting Flask on 0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
