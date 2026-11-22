"""Attendance rules: sessions, marking, percentages, leave and student check-in
(SRS 3.1.2, 4.1, 4.4, 4.5)."""
import math
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import pandas as pd

from . import clock, db

STATUSES = ("Present", "Late", "Absent", "Excused")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CHECKIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no look-alike characters


# ----------------------------------------------------------------- percentages
def percentage(present: int, late: int, absent: int) -> float | None:
    """Attendance % = attended / counted classes.

    Present and Late both count as attended. Excused classes are left out of the
    calculation entirely, so approved leave never lowers a student's percentage.
    """
    counted = present + late + absent
    if counted == 0:
        return None
    return round(100.0 * (present + late) / counted, 1)


def allowance(attended: int, counted: int, threshold: float) -> tuple[int, int]:
    """(classes that can still be missed, classes needed in a row to recover)."""
    p = threshold / 100.0
    if counted == 0 or p <= 0:
        return 0, 0
    if attended / counted >= p:
        return max(0, math.floor(attended / p - counted + 1e-9)), 0
    if p >= 1:
        return 0, 0
    return 0, max(0, math.ceil((p * counted - attended) / (1 - p) - 1e-9))


# -------------------------------------------------------------------- sessions
def term_bounds() -> tuple[date, date]:
    today = clock.today()
    start = db.get_setting("term_start")
    end = db.get_setting("term_end")
    return (
        clock.parse_date(start) if start else today - timedelta(days=120),
        clock.parse_date(end) if end else today + timedelta(days=120),
    )


def sessions(start: date, end: date, class_ids=None, teacher_id=None, student_id=None) -> list[dict]:
    """Class meetings between two dates, built from the weekly timetable with
    one-off changes (cancellation, room or time change) applied."""
    term_start, term_end = term_bounds()
    start, end = max(start, term_start), min(end, term_end)
    if start > end:
        return []
    sql = """
        SELECT s.id AS schedule_id, s.class_id, s.weekday, s.start_time, s.end_time, s.room,
               c.code, c.name, c.section, c.teacher_id, u.full_name AS teacher_name
        FROM schedules s
        JOIN classes c ON c.id = s.class_id
        LEFT JOIN teachers t ON t.id = c.teacher_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE s.active = 1"""
    params: list = []
    if class_ids is not None:
        class_ids = list(class_ids)
        if not class_ids:
            return []
        sql += f" AND s.class_id IN ({','.join('?' * len(class_ids))})"
        params += class_ids
    if teacher_id is not None:
        sql += " AND c.teacher_id = ?"
        params.append(teacher_id)
    if student_id is not None:
        sql += " AND s.class_id IN (SELECT class_id FROM enrollments WHERE student_id = ?)"
        params.append(student_id)
    by_weekday = defaultdict(list)
    for slot in db.query(sql, params):
        by_weekday[slot["weekday"]].append(dict(slot))
    changes = {
        (r["schedule_id"], r["date"]): r
        for r in db.query(
            "SELECT * FROM schedule_changes WHERE date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        )
    }
    out = []
    for day in clock.daterange(start, end):
        for slot in by_weekday[day.weekday()]:
            item = dict(slot, date=day, state="scheduled", change_note="")
            change = changes.get((slot["schedule_id"], day.isoformat()))
            if change:
                item["change_note"] = change["note"] or ""
                if change["change_type"] == "cancelled":
                    item["state"] = "cancelled"
                else:
                    item["state"] = "changed"
                    item["start_time"] = change["new_start"] or item["start_time"]
                    item["end_time"] = change["new_end"] or item["end_time"]
                    item["room"] = change["new_room"] or item["room"]
            out.append(item)
    out.sort(key=lambda s: (s["date"], s["start_time"], s["code"]))
    return out


def session_end(session: dict) -> datetime:
    return datetime.combine(session["date"], time.fromisoformat(session["end_time"]))


def session_start(session: dict) -> datetime:
    return datetime.combine(session["date"], time.fromisoformat(session["start_time"]))


def marked_keys(start: date, end: date) -> set[tuple[int, str]]:
    rows = db.query(
        "SELECT DISTINCT schedule_id, date FROM attendance WHERE date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )
    return {(r["schedule_id"], r["date"]) for r in rows}


def unmarked_sessions(start: date, end: date, teacher_id=None) -> list[dict]:
    """Finished, non-cancelled classes that have no attendance yet."""
    now = clock.now()
    done = marked_keys(start, end)
    return [
        s for s in sessions(start, end, teacher_id=teacher_id)
        if s["state"] != "cancelled"
        and session_end(s) <= now
        and (s["schedule_id"], s["date"].isoformat()) not in done
    ]


# ---------------------------------------------------------------------- roster
def roster(class_id: int) -> pd.DataFrame:
    return db.frame(
        """SELECT st.id AS student_id, st.roll_no, u.full_name, st.user_id, st.parent_user_id
           FROM enrollments e
           JOIN students st ON st.id = e.student_id
           JOIN users u ON u.id = st.user_id
           WHERE e.class_id = ? AND u.is_active = 1
           ORDER BY st.roll_no""",
        (class_id,),
    )


def leave_map(day: date, student_ids=None) -> dict[int, str]:
    """Students on approved leave on a date -> reason."""
    rows = db.query(
        "SELECT student_id, reason FROM leaves WHERE status = 'approved' AND ? BETWEEN start_date AND end_date",
        (day.isoformat(),),
    )
    wanted = set(student_ids) if student_ids is not None else None
    return {r["student_id"]: r["reason"] or "Approved leave" for r in rows if wanted is None or r["student_id"] in wanted}


def existing(schedule_id: int, day: date) -> dict[int, dict]:
    rows = db.query("SELECT * FROM attendance WHERE schedule_id = ? AND date = ?", (schedule_id, day.isoformat()))
    return {r["student_id"]: dict(r) for r in rows}


def get_slot(schedule_id: int) -> dict | None:
    row = db.one(
        """SELECT s.*, c.code, c.name, c.section, c.teacher_id
           FROM schedules s JOIN classes c ON c.id = s.class_id WHERE s.id = ?""",
        (schedule_id,),
    )
    return dict(row) if row else None


# --------------------------------------------------------------------- marking
def save_attendance(schedule_id: int, day, records: list[dict], actor: dict, method: str = "manual",
                    notify: bool = True) -> dict:
    """Insert or update attendance for one class meeting.

    records: [{"student_id", "status", "note"}]. Unchanged rows are skipped, so
    re-submitting a roster is safe and keeps the original method and timestamp.
    """
    day = clock.parse_date(day)
    slot = get_slot(schedule_id)
    if slot is None:
        raise ValueError("Unknown class slot.")
    if actor.get("role") == "teacher" and actor.get("teacher_id") != slot["teacher_id"]:
        raise PermissionError("You can only mark attendance for your own classes.")
    if day > clock.today():
        raise ValueError("Attendance cannot be marked for a future date.")
    if day.weekday() != slot["weekday"]:
        raise ValueError("That class does not meet on this date.")
    enrolled = {r["student_id"] for r in db.query(
        "SELECT student_id FROM enrollments WHERE class_id = ?", (slot["class_id"],))}
    before = existing(schedule_id, day)
    changes = []
    with db.tx() as conn:
        for rec in records:
            student_id, status = int(rec["student_id"]), rec["status"]
            if status not in STATUSES:
                raise ValueError(f"Invalid status: {status}")
            if student_id not in enrolled:
                raise ValueError("A student in this list is not enrolled in the class.")
            note = (rec.get("note") or "").strip() or None
            old = before.get(student_id)
            if old and old["status"] == status and (old["note"] or None) == note:
                continue
            conn.execute(
                """INSERT INTO attendance(student_id, schedule_id, class_id, date, status, note, method, marked_by, marked_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(student_id, schedule_id, date) DO UPDATE SET
                     status = excluded.status, note = excluded.note, method = excluded.method,
                     marked_by = excluded.marked_by, marked_at = excluded.marked_at""",
                (student_id, schedule_id, slot["class_id"], day.isoformat(), status, note, method,
                 actor["id"], clock.stamp()),
            )
            changes.append((student_id, old["status"] if old else None, status))
        if changes:
            db.audit(
                "attendance_saved",
                f"{slot['code']} {day.isoformat()} {slot['start_time']}: {len(changes)} record(s) via {method}",
                actor, conn,
            )
    if notify and changes:
        from . import notifications
        notifications.after_marking(slot, day, changes)
    return {"submitted": len(records), "changed": len(changes)}


# --------------------------------------------------------------------- student
def student_stats(student_id: int, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Per-class counts and percentage for one student."""
    sql = """
        SELECT c.id AS class_id, c.code, c.name,
               COALESCE(SUM(a.status = 'Present'), 0) AS present,
               COALESCE(SUM(a.status = 'Late'), 0) AS late,
               COALESCE(SUM(a.status = 'Absent'), 0) AS absent,
               COALESCE(SUM(a.status = 'Excused'), 0) AS excused
        FROM enrollments e
        JOIN classes c ON c.id = e.class_id
        LEFT JOIN attendance a ON a.class_id = c.id AND a.student_id = e.student_id {dates}
        WHERE e.student_id = ?
        GROUP BY c.id ORDER BY c.code"""
    dates, params = "", []
    if start:
        dates += " AND a.date >= ?"
        params.append(start.isoformat())
    if end:
        dates += " AND a.date <= ?"
        params.append(end.isoformat())
    df = db.frame(sql.format(dates=dates), params + [student_id])
    df["counted"] = df["present"] + df["late"] + df["absent"]
    df["attended"] = df["present"] + df["late"]
    df["pct"] = [percentage(r.present, r.late, r.absent) for r in df.itertuples()]
    return df


def overall(stats: pd.DataFrame) -> dict:
    present, late, absent, excused = (int(stats[c].sum()) for c in ("present", "late", "absent", "excused"))
    return {
        "present": present, "late": late, "absent": absent, "excused": excused,
        "counted": present + late + absent, "attended": present + late,
        "pct": percentage(present, late, absent),
    }


def history(student_id: int, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    sql = """
        SELECT a.id AS attendance_id, a.date, s.start_time, s.end_time, c.code, c.name,
               a.status, a.note, a.method, a.schedule_id, a.class_id
        FROM attendance a
        JOIN schedules s ON s.id = a.schedule_id
        JOIN classes c ON c.id = a.class_id
        WHERE a.student_id = ?"""
    params: list = [student_id]
    if start:
        sql += " AND a.date >= ?"
        params.append(start.isoformat())
    if end:
        sql += " AND a.date <= ?"
        params.append(end.isoformat())
    return db.frame(sql + " ORDER BY a.date DESC, s.start_time DESC", params)


# -------------------------------------------------------------------- check-in
def open_checkin(schedule_id: int, day: date, actor: dict) -> dict:
    """Open a short-lived code students use to mark themselves present."""
    current = active_window(schedule_id, day)
    if current:
        return current
    now = clock.now()
    minutes = db.get_setting("checkin_window_min", int)
    code = "".join(secrets.choice(CHECKIN_ALPHABET) for _ in range(6))
    window_id = db.execute(
        "INSERT INTO checkin_windows(schedule_id, date, code, opened_by, opened_at, expires_at) VALUES(?,?,?,?,?,?)",
        (schedule_id, day.isoformat(), code, actor["id"], now.strftime(clock.DATETIME_FMT),
         (now + timedelta(minutes=minutes)).strftime(clock.DATETIME_FMT)),
    )
    slot = get_slot(schedule_id)
    db.audit("checkin_opened", f"{slot['code']} {day.isoformat()} {slot['start_time']}", actor)
    return dict(db.one("SELECT * FROM checkin_windows WHERE id = ?", (window_id,)))


def active_window(schedule_id: int, day: date) -> dict | None:
    row = db.one(
        """SELECT * FROM checkin_windows
           WHERE schedule_id = ? AND date = ? AND closed = 0 AND expires_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (schedule_id, day.isoformat(), clock.stamp()),
    )
    return dict(row) if row else None


def close_checkin(window_id: int, actor: dict) -> None:
    db.execute("UPDATE checkin_windows SET closed = 1 WHERE id = ?", (window_id,))
    db.audit("checkin_closed", f"window {window_id}", actor)


def check_in(student_id: int, code: str, actor: dict, fingerprint: str | None = None) -> dict:
    """A student marks their own attendance with the teacher's code.

    Check-ins that look like proxy attendance are still recorded but flagged for
    the teacher: the same device used for more than one student in the same
    window, or a check-in by a student who is on approved leave.
    """
    code = (code or "").strip().upper()
    now = clock.now()
    window = db.one(
        "SELECT * FROM checkin_windows WHERE code = ? AND closed = 0 AND expires_at >= ? ORDER BY id DESC LIMIT 1",
        (code, now.strftime(clock.DATETIME_FMT)),
    )
    if window is None:
        raise ValueError("That code is not valid or has expired. Ask your teacher for the current code.")
    slot = get_slot(window["schedule_id"])
    day = clock.parse_date(window["date"])
    if not db.one("SELECT 1 FROM enrollments WHERE class_id = ? AND student_id = ?", (slot["class_id"], student_id)):
        raise ValueError(f"You are not enrolled in {slot['code']}.")
    if db.one("SELECT 1 FROM checkins WHERE window_id = ? AND student_id = ?", (window["id"], student_id)):
        raise ValueError("You have already checked in for this class.")

    reasons = []
    if fingerprint:
        same_device = db.query(
            """SELECT ci.id, st.roll_no FROM checkins ci JOIN students st ON st.id = ci.student_id
               WHERE ci.window_id = ? AND ci.fingerprint = ? AND ci.student_id != ?""",
            (window["id"], fingerprint, student_id),
        )
        if same_device:
            reasons.append("Same device as " + ", ".join(r["roll_no"] or "another student" for r in same_device))
            me = db.one("SELECT roll_no FROM students WHERE id = ?", (student_id,))
            for other in same_device:
                db.execute(
                    "UPDATE checkins SET flagged = 1, reason = COALESCE(reason || '; ', '') || ? WHERE id = ?",
                    (f"Same device as {me['roll_no']}", other["id"]),
                )
    if student_id in leave_map(day, [student_id]):
        reasons.append("Student is on approved leave today")

    grace = db.get_setting("late_grace_min", int)
    started = datetime.combine(day, time.fromisoformat(slot["start_time"]))
    status = "Late" if day == now.date() and now > started + timedelta(minutes=grace) else "Present"
    db.execute(
        "INSERT INTO checkins(window_id, student_id, at, fingerprint, flagged, reason) VALUES(?,?,?,?,?,?)",
        (window["id"], student_id, now.strftime(clock.DATETIME_FMT), fingerprint, 1 if reasons else 0,
         "; ".join(reasons) or None),
    )
    opener = {"id": window["opened_by"], "username": None}
    note = "Flagged check-in: " + "; ".join(reasons) if reasons else None
    save_attendance(window["schedule_id"], day, [{"student_id": student_id, "status": status, "note": note}],
                    opener, method="check-in", notify=False)
    db.audit("checkin", f"{slot['code']} {day.isoformat()}" + (" FLAGGED" if reasons else ""), actor)
    return {"status": status, "flagged": bool(reasons), "code": slot["code"], "name": slot["name"]}


def checkin_list(schedule_id: int, day: date) -> pd.DataFrame:
    return db.frame(
        """SELECT st.roll_no, u.full_name, ci.at, ci.flagged, ci.reason
           FROM checkins ci
           JOIN checkin_windows w ON w.id = ci.window_id
           JOIN students st ON st.id = ci.student_id
           JOIN users u ON u.id = st.user_id
           WHERE w.schedule_id = ? AND w.date = ?
           ORDER BY ci.at""",
        (schedule_id, day.isoformat()),
    )
