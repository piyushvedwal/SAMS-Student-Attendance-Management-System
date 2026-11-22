"""In-app notifications and alerts (SRS 3.1.5 and 4.3).

Every alert is stored per user with a de-duplication key, so the same event
never notifies the same person twice. Email, SMS and push delivery are not
wired up; this module is the single place where they would be added.
"""
from datetime import timedelta

from . import clock, db

TYPES = {
    "absence": "Absence recorded",
    "low_attendance": "Low attendance warning",
    "escalation": "Escalation: critical attendance",
    "schedule_change": "Schedule change",
    "reminder": "Reminder to mark attendance",
    "pattern_alert": "Unusual attendance pattern",
    "flag": "Discrepancy flag",
    "leave": "Leave request",
}
ROLE_TYPES = {
    "student": ("absence", "low_attendance", "schedule_change", "flag", "leave"),
    "parent": ("absence", "low_attendance"),
    "teacher": ("reminder", "escalation", "flag", "leave", "schedule_change"),
    "admin": ("escalation", "pattern_alert", "leave"),
}


def notify(user_id, type_: str, title: str, body: str = "", dedupe_key: str | None = None) -> bool:
    """Store one notification. Returns False if muted or already sent."""
    if user_id is None:
        return False
    with db.tx() as conn:
        pref = conn.execute(
            "SELECT enabled FROM notification_prefs WHERE user_id = ? AND type = ?", (user_id, type_)
        ).fetchone()
        if pref is not None and not pref["enabled"]:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications(user_id, type, title, body, dedupe_key, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, type_, title, body, dedupe_key, clock.stamp()),
        )
        return cur.rowcount > 0


def unread_count(user_id: int) -> int:
    return db.one("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,))["n"]


def inbox(user_id: int, limit: int = 100) -> list:
    return db.query(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (user_id, limit)
    )


def mark_read(user_id: int, notification_id: int | None = None) -> None:
    sql = "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL"
    params = [clock.stamp(), user_id]
    if notification_id is not None:
        sql += " AND id = ?"
        params.append(notification_id)
    db.execute(sql, params)


def prefs(user_id: int, role: str) -> dict[str, bool]:
    saved = {r["type"]: bool(r["enabled"]) for r in db.query(
        "SELECT type, enabled FROM notification_prefs WHERE user_id = ?", (user_id,))}
    return {t: saved.get(t, True) for t in ROLE_TYPES.get(role, ())}


def set_pref(user_id: int, type_: str, enabled: bool) -> None:
    db.execute(
        "INSERT INTO notification_prefs(user_id, type, enabled) VALUES(?,?,?) "
        "ON CONFLICT(user_id, type) DO UPDATE SET enabled = excluded.enabled",
        (user_id, type_, 1 if enabled else 0),
    )


def _admins() -> list[int]:
    return [r["id"] for r in db.query("SELECT id FROM users WHERE role = 'admin' AND is_active = 1")]


def _student(student_id: int):
    return db.one(
        """SELECT st.id, st.roll_no, st.user_id, st.parent_user_id, u.full_name
           FROM students st JOIN users u ON u.id = st.user_id WHERE st.id = ?""",
        (student_id,),
    )


def after_marking(slot: dict, day, changes: list[tuple]) -> None:
    """Absence notices for newly absent students, then threshold checks."""
    label = f"{slot['code']} on {day.strftime('%a %d %b')} at {slot['start_time']}"
    for student_id, old, new in changes:
        if new == "Absent" and old != "Absent":
            st = _student(student_id)
            key = f"absence:{student_id}:{slot['id']}:{day.isoformat()}"
            notify(st["user_id"], "absence", f"Marked absent: {label}",
                   f"You were marked absent in {slot['name']}. If this is wrong, flag it from Attendance history.", key)
            notify(st["parent_user_id"], "absence", f"{st['full_name']} was absent: {label}",
                   f"{st['full_name']} ({st['roll_no']}) was marked absent in {slot['name']}.", key)
    low_attendance_sweep(student_ids=[c[0] for c in changes], class_id=slot["class_id"])


def low_attendance_sweep(student_ids=None, class_id=None) -> int:
    """Alert on students below the configured thresholds.

    Level 1 (below the low threshold): the student and their parent.
    Level 2 (below the critical threshold): escalated to the class teacher and
    administrators. Each alert repeats at most once per week per class.
    """
    from .attendance import percentage

    low = db.get_setting("threshold_low", float)
    critical = db.get_setting("threshold_critical", float)
    min_sessions = db.get_setting("min_sessions_for_alert", int)
    sql = """
        SELECT a.student_id, a.class_id, c.code, c.name, tu.id AS teacher_user_id,
               SUM(a.status = 'Present') AS present, SUM(a.status = 'Late') AS late,
               SUM(a.status = 'Absent') AS absent
        FROM attendance a
        JOIN classes c ON c.id = a.class_id
        LEFT JOIN teachers t ON t.id = c.teacher_id
        LEFT JOIN users tu ON tu.id = t.user_id
        WHERE 1 = 1"""
    params: list = []
    if class_id is not None:
        sql += " AND a.class_id = ?"
        params.append(class_id)
    if student_ids is not None:
        ids = list(student_ids)
        if not ids:
            return 0
        sql += f" AND a.student_id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " GROUP BY a.student_id, a.class_id"
    year, week, _ = clock.today().isocalendar()
    created = 0
    for r in db.query(sql, params):
        counted = r["present"] + r["late"] + r["absent"]
        if counted < min_sessions:
            continue
        pct = percentage(r["present"], r["late"], r["absent"])
        if pct >= low:
            continue
        st = _student(r["student_id"])
        detail = f"{pct:.1f}% ({r['present'] + r['late']} of {counted} classes attended). Required: {low:.0f}%."
        key = f"low:{r['student_id']}:{r['class_id']}:{year}-W{week}"
        created += notify(st["user_id"], "low_attendance", f"Attendance below {low:.0f}% in {r['code']}",
                          f"Your attendance in {r['name']} is {detail}", key)
        created += notify(st["parent_user_id"], "low_attendance",
                          f"{st['full_name']}: attendance below {low:.0f}% in {r['code']}",
                          f"{st['full_name']}'s attendance in {r['name']} is {detail}", key)
        if pct < critical:
            key = f"critical:{r['student_id']}:{r['class_id']}:{year}-W{week}"
            title = f"{st['full_name']} ({st['roll_no']}) at {pct:.1f}% in {r['code']}"
            body = f"Below the critical threshold of {critical:.0f}%. {detail}"
            for user_id in [r["teacher_user_id"], *_admins()]:
                created += notify(user_id, "escalation", title, body, key)
    return created


def notify_schedule_change(schedule_id: int, day, text: str) -> int:
    """Tell every enrolled student (and the teacher) about a one-off change."""
    slot = db.one(
        """SELECT s.id, s.class_id, s.start_time, c.code, c.name, tu.id AS teacher_user_id
           FROM schedules s JOIN classes c ON c.id = s.class_id
           LEFT JOIN teachers t ON t.id = c.teacher_id LEFT JOIN users tu ON tu.id = t.user_id
           WHERE s.id = ?""",
        (schedule_id,),
    )
    title = f"{slot['code']} on {day.strftime('%a %d %b')}: {text}"
    key = f"schedule:{schedule_id}:{day.isoformat()}:{text}"
    recipients = [r["user_id"] for r in db.query(
        "SELECT st.user_id FROM enrollments e JOIN students st ON st.id = e.student_id WHERE e.class_id = ?",
        (slot["class_id"],))]
    recipients.append(slot["teacher_user_id"])
    return sum(notify(u, "schedule_change", title, f"{slot['name']}, usually at {slot['start_time']}.", key)
               for u in recipients)


def run_housekeeping() -> dict:
    """Idempotent periodic checks: reminders, threshold sweep, pattern alerts."""
    from . import analytics, attendance

    today = clock.today()
    counts = {"reminders": 0, "low_attendance": 0, "patterns": 0}
    for s in attendance.unmarked_sessions(today - timedelta(days=7), today):
        teacher = db.one("SELECT user_id FROM teachers WHERE id = ?", (s["teacher_id"],))
        if teacher:
            counts["reminders"] += notify(
                teacher["user_id"], "reminder",
                f"Attendance pending: {s['code']} on {s['date'].strftime('%a %d %b')} at {s['start_time']}",
                f"No attendance has been recorded for {s['name']} ({s['section']}).",
                f"remind:{s['schedule_id']}:{s['date'].isoformat()}",
            )
    counts["low_attendance"] = low_attendance_sweep()
    for a in analytics.anomalies():
        for admin_id in _admins():
            counts["patterns"] += notify(admin_id, "pattern_alert", a["title"], a["detail"], f"pattern:{a['key']}")
    return counts
