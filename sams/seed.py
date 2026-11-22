"""Demo data: a small institute with eight weeks of attendance history.

Everything is fictional and generated relative to today's date, so the app
always opens with recent history and an upcoming schedule. The data includes
deliberate cases for the alerting features to find (low attendance, a sudden
drop, an absence streak, an unusually empty class, pending requests).
"""
import random
from datetime import datetime, time, timedelta

from . import auth, clock, db, notifications
from . import attendance as att
from .config import DEMO_ACCOUNTS, DEMO_MODE, DEMO_PASSWORD

TEACHERS = [
    ("kavita.rao", "Dr. Kavita Rao", "Computer Science", "EMP-1041"),
    ("arjun.menon", "Prof. Arjun Menon", "Computer Science", "EMP-1057"),
    ("sana.qureshi", "Dr. Sana Qureshi", "Information Technology", "EMP-1063"),
    ("rohit.bansal", "Prof. Rohit Bansal", "Mathematics", "EMP-1078"),
]

# (code, name, section, teacher index)
CLASSES = [
    ("CS201", "Data Structures & Algorithms", "Section A", 0),
    ("CS202", "Database Management Systems", "Section A", 1),
    ("CS203", "Object-Oriented Programming", "Section A", 2),
    ("CS204", "Discrete Mathematics", "Section A", 3),
    ("CS211", "Data Structures & Algorithms", "Section B", 0),
    ("CS212", "Database Management Systems", "Section B", 1),
    ("CS213", "Python Programming", "Section B", 2),
    ("CS214", "Probability & Statistics", "Section B", 3),
]

SLOTS = {"1": ("09:00", "09:50"), "2": ("10:00", "10:50"), "3": ("11:10", "12:00"), "4": ("12:10", "13:00")}

# class code -> [(weekday, slot, room)]; no teacher is double-booked
TIMETABLE = {
    "CS201": [(0, "1", "A-201"), (1, "2", "A-201"), (3, "2", "A-201")],
    "CS202": [(0, "2", "A-201"), (2, "1", "Lab-1"), (3, "3", "A-201")],
    "CS203": [(1, "1", "Lab-2"), (2, "3", "A-201"), (4, "2", "Lab-2")],
    "CS204": [(0, "3", "A-201"), (2, "4", "A-201"), (4, "1", "A-201")],
    "CS211": [(0, "2", "B-105"), (1, "1", "B-105"), (3, "4", "B-105")],
    "CS212": [(0, "3", "B-105"), (2, "2", "Lab-1"), (4, "3", "B-105")],
    "CS213": [(0, "4", "Lab-2"), (1, "2", "Lab-2"), (3, "1", "B-105")],
    "CS214": [(1, "3", "B-105"), (2, "1", "B-105"), (4, "4", "B-105")],
}

SECTION_A = [
    "Aarav Sharma", "Diya Patel", "Kabir Singh", "Ananya Iyer", "Vihaan Gupta", "Isha Reddy",
    "Arnav Joshi", "Meera Pillai", "Reyansh Verma", "Saanvi Desai", "Aditya Kulkarni", "Riya Chatterjee",
    "Dhruv Malhotra", "Kavya Nambiar", "Ishaan Bhatt", "Tara Menon", "Yash Agarwal", "Nisha Rao",
    "Rohan Kapoor", "Pooja Shetty", "Karan Mehta", "Sneha Ghosh", "Manav Saxena", "Aditi Banerjee",
]
SECTION_B = [
    "Arjun Nair", "Zara Khan", "Dev Chauhan", "Mira Fernandes", "Neil Dsouza", "Anika Bose",
    "Samar Gill", "Lavanya Krishnan", "Harsh Tiwari", "Simran Kaur", "Parth Trivedi", "Tanvi Deshpande",
    "Kunal Sinha", "Aisha Siddiqui", "Varun Hegde", "Naina Sethi", "Om Pandey", "Ritika Jain",
    "Jay Thakur", "Shreya Mishra", "Tejas Gowda", "Pallavi Das", "Nikhil Rana", "Esha Dutta",
]
PARENT_FIRST = ["Sunita", "Rajesh", "Anita", "Suresh", "Kiran", "Vijay", "Geeta", "Mahesh", "Rekha", "Ashok",
                "Lata", "Deepak"]

# Students the alerting features should pick up.
LOW = {"Isha Reddy": 0.70, "Riya Chatterjee": 0.71, "Samar Gill": 0.69}
CRITICAL = {"Yash Agarwal": 0.52, "Varun Hegde": 0.55}
SUDDEN_DROP = "Reyansh Verma"
ABSENCE_STREAK = "Mira Fernandes"
ON_LEAVE = "Kavya Nambiar"
PENDING_LEAVE = "Diya Patel"
DEMO_STUDENT = "Aarav Sharma"
OUTLIER_CLASS = "CS212"


def _username(name: str) -> str:
    return name.lower().replace(" ", ".")


def ensure_seeded() -> bool:
    """Create the schema and, in demo mode, the demo data.

    Demo data is rebuilt when it is from an earlier day, so the public demo
    always shows recent history instead of a growing backlog. Outside demo
    mode an empty database gets a single administrator account instead.
    """
    db.init_db()
    has_users = db.one("SELECT 1 FROM users LIMIT 1") is not None
    if not DEMO_MODE:
        if not has_users:
            import os
            auth.create_user("admin", "Administrator", "admin", os.environ.get("SAMS_ADMIN_PASSWORD", DEMO_PASSWORD))
        return False
    if has_users and db.get_setting("seeded_on") == clock.today().isoformat():
        return False
    if has_users:
        _wipe()
    _seed()
    return True


def reset_demo() -> None:
    _wipe()
    _seed()


def _wipe() -> None:
    """Start from an empty database file."""
    from pathlib import Path

    for suffix in ("", "-wal", "-shm"):
        Path(str(db.get_path()) + suffix).unlink(missing_ok=True)
    db.init_db()


def _seed() -> None:
    rng = random.Random(42)
    today = clock.today()
    now = clock.stamp()
    term_start = clock.week_start(today) - timedelta(weeks=8)
    db.set_setting("term_start", term_start.isoformat())
    db.set_setting("term_end", (today + timedelta(weeks=12)).isoformat())
    db.set_setting("seeded_on", today.isoformat())
    # Hashing is deliberately slow, so the shared demo password is hashed once.
    salt, digest = auth.hash_password(DEMO_PASSWORD)

    with db.tx() as conn:
        def add_user(username, full_name, role, email=None):
            return conn.execute(
                "INSERT INTO users(username, full_name, email, role, salt, password_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (username, full_name, email or f"{username}@sams-demo.edu", role, salt, digest, now),
            ).lastrowid

        add_user("admin", "Meera Nair", "admin")
        teacher_ids = []
        for username, name, dept, code in TEACHERS:
            uid = add_user(username, name, "teacher")
            teacher_ids.append(conn.execute(
                "INSERT INTO teachers(user_id, employee_code, department) VALUES(?,?,?)", (uid, code, dept)
            ).lastrowid)

        class_ids, schedule_ids = {}, {}
        for code, name, section, t in CLASSES:
            class_ids[code] = conn.execute(
                "INSERT INTO classes(code, name, section, teacher_id) VALUES(?,?,?,?)",
                (code, name, section, teacher_ids[t]),
            ).lastrowid
            for weekday, slot, room in TIMETABLE[code]:
                start, end = SLOTS[slot]
                schedule_ids[(code, weekday)] = conn.execute(
                    "INSERT INTO schedules(class_id, weekday, start_time, end_time, room) VALUES(?,?,?,?,?)",
                    (class_ids[code], weekday, start, end, room),
                ).lastrowid

        students = {}  # name -> student id
        for section, names in (("Section A", SECTION_A), ("Section B", SECTION_B)):
            for i, name in enumerate(names):
                n = len(students) + 1
                uid = add_user(_username(name), name, "student")
                parent = add_user("parent." + _username(name),
                                  f"{PARENT_FIRST[n % len(PARENT_FIRST)]} {name.split()[-1]}", "parent")
                sid = conn.execute(
                    "INSERT INTO students(user_id, roll_no, program, section, semester, parent_user_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (uid, f"23CS{n:03d}", "B.Tech Computer Science", section, 3, parent),
                ).lastrowid
                students[name] = sid
                for code, _, class_section, _ in CLASSES:
                    if class_section == section:
                        conn.execute("INSERT INTO enrollments(class_id, student_id) VALUES(?,?)",
                                     (class_ids[code], sid))

    # ---- leave: one approved in the past, one pending for next week
    leave_start = clock.week_start(today) - timedelta(weeks=2)
    admin = dict(db.one("SELECT id, username FROM users WHERE username = 'admin'"))
    db.execute(
        "INSERT INTO leaves(student_id, start_date, end_date, reason, status, decided_by, decided_at, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (students[ON_LEAVE], leave_start.isoformat(), (leave_start + timedelta(days=2)).isoformat(),
         "Medical: viral fever (certificate submitted)", "approved", admin["id"], now, now),
    )
    next_monday = clock.week_start(today) + timedelta(weeks=1)
    db.execute(
        "INSERT INTO leaves(student_id, start_date, end_date, reason, created_at) VALUES(?,?,?,?,?)",
        (students[PENDING_LEAVE], next_monday.isoformat(), (next_monday + timedelta(days=1)).isoformat(),
         "Family function out of town", now),
    )

    # ---- attendance history for every finished class before today
    past = [s for s in att.sessions(term_start, today - timedelta(days=1))]
    demo_teacher = dict(db.one("SELECT t.id FROM teachers t JOIN users u ON u.id = t.user_id WHERE u.username = ?",
                               (DEMO_ACCOUNTS["teacher"],)))["id"]
    # Leave the demo teacher's most recent class unmarked, to show the reminder flow.
    kavita_past = [s for s in past if s["teacher_id"] == demo_teacher]
    skip = (kavita_past[-1]["schedule_id"], kavita_past[-1]["date"]) if kavita_past else None
    last_cs201 = [s for s in kavita_past[:-1] if s["code"] == "CS201"][-1]
    outlier_day = max((s["date"] for s in past
                       if s["code"] == OUTLIER_CLASS and s["date"] <= today - timedelta(days=6)), default=None)
    recent_cutoff = today - timedelta(days=14)
    class_days = sorted({s["date"] for s in past})
    streak_days = set(class_days[-4:])
    leave_days = {leave_start + timedelta(days=d) for d in range(3)}
    base = {name: rng.uniform(0.86, 0.98) for name in SECTION_A + SECTION_B}
    base.update(LOW)
    base.update(CRITICAL)
    base[DEMO_STUDENT] = 0.88
    late_prone = {"Kabir Singh": 0.22, "Zara Khan": 0.2}
    rosters = {code: [n for n in (SECTION_A if section == "Section A" else SECTION_B)]
               for code, _, section, _ in CLASSES}
    teacher_user = {r["id"]: r["user_id"] for r in db.query("SELECT id, user_id FROM teachers")}

    rows = []
    for s in past:
        if skip and (s["schedule_id"], s["date"]) == skip:
            continue
        marked_at = (datetime.combine(s["date"], time.fromisoformat(s["end_time"]))
                     + timedelta(minutes=rng.randint(1, 25))).strftime(clock.DATETIME_FMT)
        method = "bulk" if rng.random() < 0.35 else "manual"
        for name in rosters[s["code"]]:
            p = base[name]
            if name == DEMO_STUDENT and s["code"] == "CS204":
                p = 0.62
            if name == SUDDEN_DROP:
                p = 0.96 if s["date"] <= recent_cutoff else 0.2
            if name == ABSENCE_STREAK:
                p = 0.0 if s["date"] in streak_days else 0.94
            if s["code"] == OUTLIER_CLASS and s["date"] == outlier_day:
                p = min(p, 0.38)
            note = None
            if name == ON_LEAVE and s["date"] in leave_days:
                status, note = "Excused", "Approved leave"
            elif name == DEMO_STUDENT and s["schedule_id"] == last_cs201["schedule_id"] and s["date"] == last_cs201["date"]:
                status = "Absent"
            elif rng.random() < p:
                if rng.random() < late_prone.get(name, 0.06):
                    status, note = "Late", rng.choice(["Arrived 10 min late", "Late: bus delay", None, None])
                else:
                    status = "Present"
            else:
                status, note = "Absent", rng.choice([None, None, None, None, "Informed: unwell"])
            rows.append((students[name], s["schedule_id"], s["class_id"], s["date"].isoformat(), status, note,
                         method, teacher_user[s["teacher_id"]], marked_at))
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO attendance(student_id, schedule_id, class_id, date, status, note, method, marked_by, marked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)

    # ---- a discrepancy flag from the demo student, waiting for the teacher
    flagged = db.one("SELECT id FROM attendance WHERE student_id = ? AND schedule_id = ? AND date = ?",
                     (students[DEMO_STUDENT], last_cs201["schedule_id"], last_cs201["date"].isoformat()))
    db.execute("INSERT INTO flags(attendance_id, student_id, message, created_at) VALUES(?,?,?,?)",
               (flagged["id"], students[DEMO_STUDENT],
                "I was in class. I came in just after the roll call and sat in the last row.", now))

    # ---- one-off schedule changes next week
    upcoming = att.sessions(today + timedelta(days=1), today + timedelta(days=10))
    cancel = next((s for s in upcoming if s["code"] == "CS203"), None)
    move = next((s for s in upcoming if s["code"] == "CS201"), None)
    for s, kind, room, note, text in (
        (cancel, "cancelled", None, "Faculty at a conference", "class cancelled"),
        (move, "room", "Lab-2", "Lab session for the trees assignment", "moved to Lab-2"),
    ):
        if s is None:
            continue
        db.execute(
            "INSERT INTO schedule_changes(schedule_id, date, change_type, new_room, note, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (s["schedule_id"], s["date"].isoformat(), kind, room, note, admin["id"], now),
        )
        notifications.notify_schedule_change(s["schedule_id"], s["date"], text)

    # ---- notifications a real system would already have sent
    recent_absences = db.query(
        """SELECT a.student_id, a.schedule_id, a.date FROM attendance a
           WHERE a.status = 'Absent' AND a.date >= ?""",
        ((today - timedelta(days=4)).isoformat(),),
    )
    for r in recent_absences:
        slot = att.get_slot(r["schedule_id"])
        day = clock.parse_date(r["date"])
        label = f"{slot['code']} on {day.strftime('%a %d %b')} at {slot['start_time']}"
        st = db.one("SELECT st.user_id, st.parent_user_id, st.roll_no, u.full_name FROM students st "
                    "JOIN users u ON u.id = st.user_id WHERE st.id = ?", (r["student_id"],))
        key = f"absence:{r['student_id']}:{r['schedule_id']}:{r['date']}"
        notifications.notify(st["user_id"], "absence", f"Marked absent: {label}",
                             f"You were marked absent in {slot['name']}. If this is wrong, flag it from "
                             "Attendance history.", key)
        notifications.notify(st["parent_user_id"], "absence", f"{st['full_name']} was absent: {label}",
                             f"{st['full_name']} ({st['roll_no']}) was marked absent in {slot['name']}.", key)
    kavita_user = teacher_user[demo_teacher]
    notifications.notify(kavita_user, "flag", "Aarav Sharma flagged an attendance record",
                         f"CS201 on {last_cs201['date'].strftime('%a %d %b')}: marked Absent, student says present.",
                         "seed-flag")
    for user_id in (kavita_user, admin["id"]):
        notifications.notify(user_id, "leave", "Leave request from Diya Patel",
                             f"{next_monday.strftime('%d %b')} to {(next_monday + timedelta(days=1)).strftime('%d %b')}: "
                             "family function out of town.", "seed-leave")
    notifications.run_housekeeping()
    db.audit("demo_data_created", f"{len(rows)} attendance records", admin)
