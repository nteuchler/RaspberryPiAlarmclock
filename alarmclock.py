#!/usr/bin/env python3
import os
import json
import time
import signal
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, date

from flask import Flask, request, jsonify, render_template
from gpiozero import Button

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "alarmclock.db")

# ----- GPIO button (change pin if you want) -----
BUTTON_GPIO = 17  # BCM numbering
button = Button(BUTTON_GPIO, pull_up=True, bounce_time=0.05)

# ----- Defaults -----
DEFAULT_SETTINGS = {
    "volume_percent": 65,  # 0..100
    "alarm_tone_path": os.path.join(APP_DIR, "media", "alarmtone.mp3"),
    # morning content:
    "content_type": "stream",  # "stream" or "file"
    "content_value": "http://icecast.omroep.nl/radio1-bb-mp3",  # example
    # death clock:
    "birthdate": "1999-04-15",
    # Denmark newborn boys life expectancy 2023-2024: 79.9 years (Stats Denmark)
    "life_expectancy_years": 79.9,
}

# Audio process handle
player_lock = threading.Lock()
player_proc = None
current_mode = "idle"  # idle | alarm | content

app = Flask(__name__)

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS alarms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_hhmm TEXT NOT NULL,           -- "07:30"
        days_mask INTEGER NOT NULL,        -- bitmask Mon..Sun: bit0=Mon ... bit6=Sun
        enabled INTEGER NOT NULL DEFAULT 1,
        last_fired_date TEXT              -- "YYYY-MM-DD" to prevent multiple fires/day
    )
    """)

    conn.commit()

    # seed settings
    for k, v in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings(k,v) VALUES (?,?)", (k, json.dumps(v)))
    conn.commit()
    conn.close()

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
    cur.execute("INSERT INTO settings(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value)))
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

def update_alarm(alarm_id, time_hhmm=None, days_mask=None, enabled=None):
    conn = db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
    if not row:
        conn.close()
        return False

    new_time = time_hhmm if time_hhmm is not None else row["time_hhmm"]
    new_mask = int(days_mask) if days_mask is not None else row["days_mask"]
    new_enabled = (1 if enabled else 0) if enabled is not None else row["enabled"]

    cur.execute(
        "UPDATE alarms SET time_hhmm=?, days_mask=?, enabled=? WHERE id=?",
        (new_time, new_mask, new_enabled, alarm_id),
    )
    conn.commit()
    conn.close()
    return True

def delete_alarm(alarm_id):
    conn = db()
    conn.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
    conn.commit()
    conn.close()

def set_last_fired(alarm_id, fired_date_str):
    conn = db()
    conn.execute("UPDATE alarms SET last_fired_date=? WHERE id=?", (fired_date_str, alarm_id))
    conn.commit()
    conn.close()

# ---------- Audio ----------
def set_system_volume(percent: int):
    percent = max(0, min(100, int(percent)))
    # Works for most ALSA setups; adjust "Master" if your mixer name differs
    subprocess.run(["amixer", "sset", "Master", f"{percent}%"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def stop_audio():
    global player_proc, current_mode
    with player_lock:
        if player_proc and player_proc.poll() is None:
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
    """
    target: file path or stream URL
    loop: if True, loop forever
    """
    global player_proc
    args = ["cvlc", "--no-video", "--quiet"]
    if loop:
        args += ["--loop"]
    args += [target]

    with player_lock:
        # kill any prior playback
        if player_proc and player_proc.poll() is None:
            stop_audio()
        player_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_alarm():
    global current_mode
    set_system_volume(get_setting("volume_percent"))
    tone = get_setting("alarm_tone_path")
    # Loop alarm tone
    play_vlc(tone, loop=True)
    current_mode = "alarm"

def start_content():
    global current_mode
    set_system_volume(get_setting("volume_percent"))
    ctype = get_setting("content_type")
    cval = get_setting("content_value")
    # For content, don't loop by default; radio streams naturally continue
    if ctype == "file":
        play_vlc(cval, loop=False)
    else:
        play_vlc(cval, loop=False)
    current_mode = "content"

# ---------- Death clock ----------
def hours_remaining_until_expected_death():
    birth_s = get_setting("birthdate")
    life_years = float(get_setting("life_expectancy_years"))

    b = datetime.strptime(birth_s, "%Y-%m-%d").date()
    # Convert years to days using mean tropical year-ish (good enough for a countdown display)
    days = life_years * 365.2425
    expected_death = datetime.combine(b, datetime.min.time()) + timedelta(days=days)
    now = datetime.now()
    delta = expected_death - now
    return expected_death, delta.total_seconds() / 3600.0

# ---------- Scheduler loop ----------
def is_due_today(alarm_row, now_dt: datetime) -> bool:
    if int(alarm_row["enabled"]) != 1:
        return False

    time_hhmm = alarm_row["time_hhmm"]
    try:
        hh, mm = [int(x) for x in time_hhmm.split(":")]
    except ValueError:
        return False

    # weekday bit: Mon=0 .. Sun=6
    wd = (now_dt.weekday())  # Mon=0..Sun=6
    mask = int(alarm_row["days_mask"])
    if (mask & (1 << wd)) == 0:
        return False

    # only trigger at matching minute (and once per day)
    if now_dt.hour != hh or now_dt.minute != mm:
        return False

    today_s = now_dt.date().isoformat()
    if alarm_row["last_fired_date"] == today_s:
        return False

    return True

def scheduler_thread():
    while True:
        try:
            now_dt = datetime.now()
            alarms = list_alarms()
            for a in alarms:
                if is_due_today(a, now_dt):
                    # fire
                    set_last_fired(a["id"], now_dt.date().isoformat())
                    start_alarm()
                    break
        except Exception:
            # keep running even if something weird happens
            pass

        time.sleep(1)

# ---------- Button behavior ----------
def on_button_pressed():
    global current_mode
    # If alarm is ringing -> switch to morning content
    if current_mode == "alarm":
        stop_audio()
        start_content()
    # If already playing content -> stop (optional)
    elif current_mode == "content":
        stop_audio()

button.when_pressed = on_button_pressed

# ---------- Web ----------
@app.route("/")
def index():
    expected_death, hrs = hours_remaining_until_expected_death()
    return render_template(
        "index.html",
        alarms=list_alarms(),
        settings={
            "volume_percent": get_setting("volume_percent"),
            "alarm_tone_path": get_setting("alarm_tone_path"),
            "content_type": get_setting("content_type"),
            "content_value": get_setting("content_value"),
            "birthdate": get_setting("birthdate"),
            "life_expectancy_years": get_setting("life_expectancy_years"),
        },
        expected_death=expected_death.strftime("%Y-%m-%d %H:%M"),
        hours_remaining=hrs,
        mode=current_mode,
    )

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({
            "volume_percent": get_setting("volume_percent"),
            "alarm_tone_path": get_setting("alarm_tone_path"),
            "content_type": get_setting("content_type"),
            "content_value": get_setting("content_value"),
            "birthdate": get_setting("birthdate"),
            "life_expectancy_years": get_setting("life_expectancy_years"),
        })

    data = request.get_json(force=True) or {}
    for k in ["volume_percent", "alarm_tone_path", "content_type", "content_value", "birthdate", "life_expectancy_years"]:
        if k in data:
            set_setting(k, data[k])

    # apply volume immediately
    if "volume_percent" in data:
        set_system_volume(int(get_setting("volume_percent")))

    return jsonify({"ok": True})

@app.route("/api/alarms", methods=["GET", "POST"])
def api_alarms():
    if request.method == "GET":
        return jsonify(list_alarms())

    data = request.get_json(force=True) or {}
    time_hhmm = data.get("time_hhmm", "07:30")
    days_mask = int(data.get("days_mask", 0b0111110))  # default Mon-Fri
    enabled = bool(data.get("enabled", True))
    create_alarm(time_hhmm, days_mask, enabled)
    return jsonify({"ok": True})

@app.route("/api/alarms/<int:alarm_id>", methods=["PATCH", "DELETE"])
def api_alarm_one(alarm_id):
    if request.method == "DELETE":
        delete_alarm(alarm_id)
        return jsonify({"ok": True})

    data = request.get_json(force=True) or {}
    ok = update_alarm(
        alarm_id,
        time_hhmm=data.get("time_hhmm"),
        days_mask=data.get("days_mask"),
        enabled=data.get("enabled"),
    )
    return jsonify({"ok": ok})

@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.get_json(force=True) or {}
    action = data.get("action")

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

    # Start scheduler
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()

    # Run web server
    app.run(host="0.0.0.0", port=8080, debug=False)

if __name__ == "__main__":
    main()
