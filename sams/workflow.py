"""Requests that move between people: discrepancy flags, leave, schedule changes
(SRS 4.5 and the alternative flows of use cases 8.6.1 and 8.6.2)."""
from datetime import date

import pandas as pd

from . import attendance as att
from . import clock, db, notifications

CHANGE_TYPES = {"cancelled": "Cancel this class", "room": "Change room", "time": "Change time"}


# ----------------------------------------------------------- discrepancy flags
def create_flag(attendance_id: int, student_id: int, message: str, actor: dict) -> int:
    message = (message or "").strip()
    if len(message) < 10:
        raise ValueError("Please describe the problem in at least 10 characters.")
    record = db.one(
        """SELECT a.*, c.code, c.name, tu.id AS teacher_user_id
           FROM attendance a JOIN classes c ON c.id = a.class_id
           LEFT JOIN teachers t ON t.id = c.teacher_id LEFT JOIN users tu ON tu.id = t.user_id
           WHERE a.id = ?""",
        (attendance_id,),
    )
    if record is None or record["student_id"] != student_id:
        raise ValueError("You can only flag your own attendance records.")
    if db.one("SELECT 1 FROM flags WHERE attendance_id = ? AND status = 'open'", (attendance_id,)):
        raise ValueError("This record is already flagged and waiting for review.")
    flag_id = db.execute(
        "INSERT INTO flags(attendance_id, student_id, message, created_at) VALUES(?,?,?,?)",
        (attendance_id, student_id, message[:500], clock.stamp()),
    )
    db.audit("flag_created", f"{record['code']} {record['date']}", actor)
    notifications.notify(
        record["teacher_user_id"], "flag", f"{actor['full_name']} flagged an attendance record",
        f"{record['code']} on {record['date']}: marked {record['status']}. \"{message[:140]}\"", f"flag:{flag_id}")
    return flag_id


def flags(teacher_id=None, student_id=None, status=None) -> pd.DataFrame:
    sql = """
        SELECT f.id AS flag_id, f.message, f.status AS flag_status, f.response, f.created_at, f.resolved_at,
               a.id AS attendance_id, a.date, a.status, a.schedule_id, a.note, s.start_time,
               c.code, c.name, st.id AS student_id, st.roll_no, u.full_name AS student, st.user_id
        FROM flags f
        JOIN attendance a ON a.id = f.attendance_id
        JOIN schedules s ON s.id = a.schedule_id
        JOIN classes c ON c.id = a.class_id
        JOIN students st ON st.id = f.student_id
        JOIN users u ON u.id = st.user_id
        WHERE 1 = 1"""
    params: list = []
    if teacher_id is not None:
        sql += " AND c.teacher_id = ?"
        params.append(teacher_id)
    if student_id is not None:
        sql += " AND f.student_id = ?"
        params.append(student_id)
    if status is not None:
        sql += " AND f.status = ?"
        params.append(status)
    return db.frame(sql + " ORDER BY f.created_at DESC, f.id DESC", params)


def resolve_flag(flag_id: int, accept: bool, new_status: str | None, response: str, actor: dict) -> None:
    flag = db.one(
        """SELECT f.*, a.schedule_id, a.date, a.status AS current, c.code, c.teacher_id, st.user_id
           FROM flags f JOIN attendance a ON a.id = f.attendance_id
           JOIN classes c ON c.id = a.class_id JOIN students st ON st.id = f.student_id
           WHERE f.id = ?""",
        (flag_id,),
    )
    if flag is None or flag["status"] != "open":
        raise ValueError("This flag has already been resolved.")
    if actor.get("role") == "teacher" and actor.get("teacher_id") != flag["teacher_id"]:
        raise PermissionError("You can only review flags for your own classes.")
    if accept:
        if new_status not in att.STATUSES:
            raise ValueError("Choose the corrected status.")
        att.save_attendance(
            flag["schedule_id"], flag["date"],
            [{"student_id": flag["student_id"], "status": new_status, "note": "Corrected after student flag"}],
            actor, method="correction")
    db.execute(
        "UPDATE flags SET status = ?, response = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
        ("accepted" if accept else "rejected", (response or "").strip()[:500] or None, actor["id"],
         clock.stamp(), flag_id),
    )
    db.audit("flag_resolved", f"flag {flag_id} {'accepted' if accept else 'rejected'}", actor)
    outcome = f"corrected to {new_status}" if accept else "kept as " + flag["current"]
    notifications.notify(
        flag["user_id"], "flag", f"Your flag for {flag['code']} on {flag['date']} was reviewed",
        f"The record was {outcome}." + (f" Teacher's note: {response.strip()}" if (response or '').strip() else ""),
        f"flag-resolved:{flag_id}")


# ----------------------------------------------------------------------- leave
def request_leave(student_id: int, start: date, end: date, reason: str, actor: dict) -> int:
    reason = (reason or "").strip()
    if end < start:
        raise ValueError("The end date cannot be before the start date.")
    if (end - start).days > 30:
        raise ValueError("A single leave request can cover at most 31 days.")
    if len(reason) < 5:
        raise ValueError("Please give a reason for the leave.")
    overlap = db.one(
        "SELECT 1 FROM leaves WHERE student_id = ? AND status != 'rejected' AND start_date <= ? AND end_date >= ?",
        (student_id, end.isoformat(), start.isoformat()),
    )
    if overlap:
        raise ValueError("You already have a leave request covering some of these dates.")
    leave_id = db.execute(
        "INSERT INTO leaves(student_id, start_date, end_date, reason, created_at) VALUES(?,?,?,?,?)",
        (student_id, start.isoformat(), end.isoformat(), reason[:300], clock.stamp()),
    )
    db.audit("leave_requested", f"{start.isoformat()} to {end.isoformat()}", actor)
    staff = [r["id"] for r in db.query(
        """SELECT DISTINCT tu.id FROM enrollments e JOIN classes c ON c.id = e.class_id
           JOIN teachers t ON t.id = c.teacher_id JOIN users tu ON tu.id = t.user_id WHERE e.student_id = ?""",
        (student_id,))]
    staff += [r["id"] for r in db.query("SELECT id FROM users WHERE role = 'admin' AND is_active = 1")]
    for user_id in staff:
        notifications.notify(user_id, "leave", f"Leave request from {actor['full_name']}",
                             f"{start.strftime('%d %b')} to {end.strftime('%d %b')}: {reason[:140]}",
                             f"leave:{leave_id}")
    return leave_id


def leaves(student_id=None, teacher_id=None, status=None) -> pd.DataFrame:
    sql = """
        SELECT l.id AS leave_id, l.start_date, l.end_date, l.reason, l.status, l.created_at, l.decided_at,
               st.id AS student_id, st.roll_no, st.section, u.full_name AS student, du.full_name AS decided_by
        FROM leaves l
        JOIN students st ON st.id = l.student_id
        JOIN users u ON u.id = st.user_id
        LEFT JOIN users du ON du.id = l.decided_by
        WHERE 1 = 1"""
    params: list = []
    if student_id is not None:
        sql += " AND l.student_id = ?"
        params.append(student_id)
    if teacher_id is not None:
        sql += """ AND l.student_id IN (SELECT e.student_id FROM enrollments e
                    JOIN classes c ON c.id = e.class_id WHERE c.teacher_id = ?)"""
        params.append(teacher_id)
    if status is not None:
        sql += " AND l.status = ?"
        params.append(status)
    return db.frame(sql + " ORDER BY l.created_at DESC, l.id DESC", params)


def decide_leave(leave_id: int, approve: bool, actor: dict) -> int:
    """Approve or reject. Approval turns absences already recorded in the leave
    period into Excused, and later marking defaults those days to Excused."""
    leave = db.one(
        "SELECT l.*, st.user_id FROM leaves l JOIN students st ON st.id = l.student_id WHERE l.id = ?", (leave_id,))
    if leave is None or leave["status"] != "pending":
        raise ValueError("This request has already been decided.")
    converted = 0
    with db.tx() as conn:
        conn.execute("UPDATE leaves SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                     ("approved" if approve else "rejected", actor["id"], clock.stamp(), leave_id))
        if approve:
            converted = conn.execute(
                """UPDATE attendance SET status = 'Excused', note = 'Approved leave', marked_by = ?, marked_at = ?
                   WHERE student_id = ? AND status = 'Absent' AND date BETWEEN ? AND ?""",
                (actor["id"], clock.stamp(), leave["student_id"], leave["start_date"], leave["end_date"]),
            ).rowcount
        db.audit("leave_decided", f"leave {leave_id} {'approved' if approve else 'rejected'}; "
                                  f"{converted} absence(s) excused", actor, conn)
    notifications.notify(
        leave["user_id"], "leave",
        f"Leave {'approved' if approve else 'rejected'}: {leave['start_date']} to {leave['end_date']}",
        "Classes in this period will be recorded as Excused." if approve else "Contact your class teacher for details.",
        f"leave-decided:{leave_id}")
    return converted


# ------------------------------------------------------------ schedule changes
def change_schedule(schedule_id: int, day: date, change_type: str, actor: dict, new_room: str | None = None,
                    new_start: str | None = None, new_end: str | None = None, note: str = "") -> int:
    """Record a one-off change to a class meeting and notify everyone affected."""
    slot = att.get_slot(schedule_id)
    if slot is None or change_type not in CHANGE_TYPES:
        raise ValueError("Unknown class slot or change type.")
    if day.weekday() != slot["weekday"]:
        raise ValueError(f"{slot['code']} does not meet on {day.strftime('%A')}s in this slot.")
    if day < clock.today():
        raise ValueError("Only upcoming classes can be changed.")
    if change_type == "room" and not (new_room or "").strip():
        raise ValueError("Enter the new room.")
    if change_type == "time":
        if not new_start or not new_end or new_end <= new_start:
            raise ValueError("Enter a valid new start and end time.")
    db.execute(
        """INSERT INTO schedule_changes(schedule_id, date, change_type, new_start, new_end, new_room, note,
                                        created_by, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(schedule_id, date) DO UPDATE SET change_type = excluded.change_type,
             new_start = excluded.new_start, new_end = excluded.new_end, new_room = excluded.new_room,
             note = excluded.note, created_by = excluded.created_by, created_at = excluded.created_at""",
        (schedule_id, day.isoformat(), change_type, new_start if change_type == "time" else None,
         new_end if change_type == "time" else None, (new_room or "").strip() if change_type == "room" else None,
         (note or "").strip()[:200] or None, actor["id"], clock.stamp()),
    )
    text = {"cancelled": "class cancelled", "room": f"moved to {(new_room or '').strip()}",
            "time": f"moved to {new_start}-{new_end}"}[change_type]
    db.audit("schedule_changed", f"{slot['code']} {day.isoformat()}: {text}", actor)
    return notifications.notify_schedule_change(schedule_id, day, text)
