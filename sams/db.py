"""SQLite storage. Tables follow the ER diagram in the SRS (section 8.2):
USER, TEACHER, STUDENT, CLASS_ENTITY, SCHEDULE, ATTENDANCE, NOTIFICATION,
plus supporting tables for leave, discrepancy flags, check-in and audit."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "sams.db"
_path = Path(os.environ.get("SAMS_DB", _DEFAULT))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  email TEXT,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student','parent')),
  salt TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  last_login TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teachers(
  id INTEGER PRIMARY KEY,
  user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
  employee_code TEXT,
  department TEXT
);
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY,
  user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
  roll_no TEXT UNIQUE,
  program TEXT,
  section TEXT,
  semester INTEGER,
  parent_user_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS classes(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  section TEXT,
  teacher_id INTEGER REFERENCES teachers(id)
);
CREATE TABLE IF NOT EXISTS enrollments(
  class_id INTEGER NOT NULL REFERENCES classes(id),
  student_id INTEGER NOT NULL REFERENCES students(id),
  PRIMARY KEY(class_id, student_id)
);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY,
  class_id INTEGER NOT NULL REFERENCES classes(id),
  weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  room TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedule_changes(
  id INTEGER PRIMARY KEY,
  schedule_id INTEGER NOT NULL REFERENCES schedules(id),
  date TEXT NOT NULL,
  change_type TEXT NOT NULL CHECK(change_type IN ('cancelled','room','time')),
  new_start TEXT,
  new_end TEXT,
  new_room TEXT,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  UNIQUE(schedule_id, date)
);
CREATE TABLE IF NOT EXISTS attendance(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  schedule_id INTEGER NOT NULL REFERENCES schedules(id),
  class_id INTEGER NOT NULL REFERENCES classes(id),
  date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('Present','Late','Absent','Excused')),
  note TEXT,
  method TEXT NOT NULL DEFAULT 'manual',
  marked_by INTEGER REFERENCES users(id),
  marked_at TEXT NOT NULL,
  UNIQUE(student_id, schedule_id, date)
);
CREATE INDEX IF NOT EXISTS idx_att_class_date ON attendance(class_id, date);
CREATE INDEX IF NOT EXISTS idx_att_student_date ON attendance(student_id, date);
CREATE TABLE IF NOT EXISTS leaves(
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  decided_by INTEGER REFERENCES users(id),
  decided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flags(
  id INTEGER PRIMARY KEY,
  attendance_id INTEGER NOT NULL REFERENCES attendance(id),
  student_id INTEGER NOT NULL REFERENCES students(id),
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','rejected')),
  response TEXT,
  resolved_by INTEGER REFERENCES users(id),
  resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkin_windows(
  id INTEGER PRIMARY KEY,
  schedule_id INTEGER NOT NULL REFERENCES schedules(id),
  date TEXT NOT NULL,
  code TEXT NOT NULL,
  opened_by INTEGER NOT NULL REFERENCES users(id),
  opened_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checkins(
  id INTEGER PRIMARY KEY,
  window_id INTEGER NOT NULL REFERENCES checkin_windows(id),
  student_id INTEGER NOT NULL REFERENCES students(id),
  at TEXT NOT NULL,
  fingerprint TEXT,
  flagged INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  UNIQUE(window_id, student_id)
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  channel TEXT NOT NULL DEFAULT 'in-app',
  dedupe_key TEXT,
  created_at TEXT NOT NULL,
  read_at TEXT,
  UNIQUE(user_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS notification_prefs(
  user_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(user_id, type)
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user_id INTEGER,
  username TEXT,
  action TEXT NOT NULL,
  detail TEXT
);
"""

DEFAULT_SETTINGS = {
    "threshold_low": "75",          # % below which students and parents are alerted
    "threshold_critical": "60",     # % below which the case is escalated to staff
    "session_timeout_min": "20",    # inactivity logout
    "checkin_window_min": "5",      # how long a student check-in code stays valid
    "late_grace_min": "10",         # check-ins after this many minutes count as Late
    "min_sessions_for_alert": "4",  # do not alert on tiny samples
}


def set_path(path) -> None:
    """Point the app at a different database file (used by tests)."""
    global _path
    _path = Path(path)


def get_path() -> Path:
    return _path


def connect() -> sqlite3.Connection:
    _path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    """One short-lived connection per unit of work; commits on success."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params=()) -> list:
    with tx() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def frame(sql: str, params=()) -> pd.DataFrame:
    with tx() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def execute(sql: str, params=()) -> int:
    with tx() as conn:
        return conn.execute(sql, params).lastrowid


def init_db() -> None:
    with tx() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))


def get_setting(key: str, cast=str):
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    value = row["value"] if row else DEFAULT_SETTINGS.get(key)
    return cast(value) if value is not None else None


def set_setting(key: str, value) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def audit(action: str, detail: str = "", user=None, conn=None) -> None:
    """Append to the audit log (SRS 6: audit logging of access and changes)."""
    from . import clock

    row = (
        clock.stamp(),
        user["id"] if user else None,
        user["username"] if user else None,
        action,
        detail,
    )
    sql = "INSERT INTO audit_log(at, user_id, username, action, detail) VALUES(?,?,?,?,?)"
    if conn is not None:
        conn.execute(sql, row)
    else:
        execute(sql, row)
