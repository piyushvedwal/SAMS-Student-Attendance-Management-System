"""Rules of the system, checked against the seeded demo data."""
from datetime import timedelta

import pytest

from sams import analytics, auth, clock, db, notifications, reports, workflow
from sams import attendance as att

from conftest import user


# ------------------------------------------------------------------- auth
def test_password_policy():
    assert auth.password_problems("Str0ng!pass") == []
    assert "an uppercase letter" in auth.password_problems("weak1!aa")
    assert "at least 8 characters" in auth.password_problems("Ab1!")
    assert "a special character" in auth.password_problems("Password1")


def test_password_hash_is_salted_and_verifiable():
    salt1, hash1 = auth.hash_password("Secret#123")
    salt2, hash2 = auth.hash_password("Secret#123")
    assert hash1 != hash2 and salt1 != salt2
    assert auth.verify_password("Secret#123", salt1, hash1)
    assert not auth.verify_password("secret#123", salt1, hash1)


def test_login_and_roles(app_db):
    assert auth.authenticate("admin", "Demo@1234")["role"] == "admin"
    assert auth.authenticate("KAVITA.RAO ", "Demo@1234")["teacher_id"]
    assert auth.authenticate("aarav.sharma", "Demo@1234")["student_id"]
    assert auth.authenticate("parent.aarav.sharma", "Demo@1234")["children"]
    with pytest.raises(auth.AuthError):
        auth.authenticate("admin", "wrong")
    with pytest.raises(auth.AuthError):
        auth.authenticate("nobody", "Demo@1234")


def test_account_locks_after_repeated_failures(app_db):
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        with pytest.raises(auth.AuthError):
            auth.authenticate("diya.patel", "wrong")
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.authenticate("diya.patel", "Demo@1234")


def test_shared_demo_accounts_cannot_be_locked_out(app_db, admin):
    for _ in range(auth.MAX_FAILED_ATTEMPTS + 2):
        with pytest.raises(auth.AuthError):
            auth.authenticate("admin", "wrong")
    assert auth.authenticate("admin", "Demo@1234")
    with pytest.raises(auth.AuthError):
        auth.set_password(admin["id"], "Another#123", admin)
    with pytest.raises(auth.AuthError):
        auth.set_active(user("kavita.rao")["id"], False, admin)


def test_create_user_and_deactivate(app_db, admin):
    new_id = auth.create_user("new.student", "New Student", "student", "Welcome#2026", actor=admin,
                              roll_no="23CS900", section="Section A")
    assert auth.authenticate("new.student", "Welcome#2026")["student_id"]
    with pytest.raises(auth.AuthError, match="already taken"):
        auth.create_user("new.student", "Again", "student", "Welcome#2026")
    with pytest.raises(auth.AuthError, match="Password needs"):
        auth.create_user("weak.user", "Weak", "teacher", "password")
    auth.set_active(new_id, False, admin)
    with pytest.raises(auth.AuthError, match="deactivated"):
        auth.authenticate("new.student", "Welcome#2026")
    actions = [r["action"] for r in db.query("SELECT action FROM audit_log")]
    assert "user_created" in actions and "user_deactivated" in actions


# ------------------------------------------------------------- attendance
def test_percentage_rules():
    assert att.percentage(8, 0, 2) == 80.0
    assert att.percentage(6, 2, 2) == 80.0          # late counts as attended
    assert att.percentage(8, 0, 2) == att.percentage(8, 0, 2)
    assert att.percentage(3, 0, 1) == 75.0
    assert att.percentage(0, 0, 0) is None           # only excused classes: nothing to measure


def test_allowance():
    assert att.allowance(30, 30, 75) == (10, 0)      # 30/40 = 75%
    assert att.allowance(15, 20, 75) == (0, 0)       # exactly on the line
    assert att.allowance(14, 20, 75) == (0, 4)       # 18/24 = 75%


def _clear_meeting(s):
    """Make a class meeting unmarked again (flags point at attendance rows)."""
    key = (s["schedule_id"], s["date"].isoformat())
    db.execute("DELETE FROM flags WHERE attendance_id IN "
               "(SELECT id FROM attendance WHERE schedule_id = ? AND date = ?)", key)
    db.execute("DELETE FROM attendance WHERE schedule_id = ? AND date = ?", key)


def _last_meeting(teacher):
    """A finished class of this teacher from the last three weeks."""
    today = clock.today()
    held = [s for s in att.sessions(today - timedelta(days=21), today - timedelta(days=1),
                                    teacher_id=teacher["teacher_id"]) if s["state"] != "cancelled"]
    return held[0]


def test_save_attendance_upserts_and_skips_unchanged(app_db, teacher):
    s = _last_meeting(teacher)
    roster = att.roster(s["class_id"])
    records = [{"student_id": int(i), "status": "Present", "note": ""} for i in roster["student_id"]]
    att.save_attendance(s["schedule_id"], s["date"], records, teacher)
    assert att.save_attendance(s["schedule_id"], s["date"], records, teacher)["changed"] == 0
    records[0]["status"], records[0]["note"] = "Late", "Arrived 12 min late"
    assert att.save_attendance(s["schedule_id"], s["date"], records, teacher)["changed"] == 1
    saved = att.existing(s["schedule_id"], s["date"])[records[0]["student_id"]]
    assert (saved["status"], saved["note"]) == ("Late", "Arrived 12 min late")
    total = db.one("SELECT COUNT(*) AS n FROM attendance WHERE schedule_id = ? AND date = ?",
                   (s["schedule_id"], s["date"].isoformat()))["n"]
    assert total == len(roster)


def test_save_attendance_rejects_bad_input(app_db, teacher):
    s = _last_meeting(teacher)
    student_id = int(att.roster(s["class_id"])["student_id"].iloc[0])
    ok = [{"student_id": student_id, "status": "Present"}]
    with pytest.raises(ValueError, match="future"):
        att.save_attendance(s["schedule_id"], clock.today() + timedelta(days=7), ok, teacher)
    with pytest.raises(ValueError, match="does not meet"):
        att.save_attendance(s["schedule_id"], s["date"] - timedelta(days=1), ok, teacher)
    with pytest.raises(ValueError, match="Invalid status"):
        att.save_attendance(s["schedule_id"], s["date"], [{"student_id": student_id, "status": "Maybe"}], teacher)
    outsider = db.one("SELECT id FROM students WHERE id NOT IN (SELECT student_id FROM enrollments WHERE class_id = ?)",
                      (s["class_id"],))["id"]
    with pytest.raises(ValueError, match="not enrolled"):
        att.save_attendance(s["schedule_id"], s["date"], [{"student_id": outsider, "status": "Present"}], teacher)


def test_teacher_cannot_mark_another_teachers_class(app_db, teacher):
    other = user("arjun.menon")
    s = _last_meeting(other)
    student_id = int(att.roster(s["class_id"])["student_id"].iloc[0])
    with pytest.raises(PermissionError):
        att.save_attendance(s["schedule_id"], s["date"], [{"student_id": student_id, "status": "Absent"}], teacher)
    assert att.save_attendance(s["schedule_id"], s["date"],
                               [{"student_id": student_id, "status": "Absent"}], user("admin"))["submitted"] == 1


def test_excused_classes_do_not_lower_percentage(app_db, teacher):
    student = user("kavya.nambiar")           # had three days of approved leave
    stats = att.overall(att.student_stats(student["student_id"]))
    assert stats["excused"] > 0
    assert stats["pct"] == att.percentage(stats["present"], stats["late"], stats["absent"])
    assert stats["counted"] == stats["present"] + stats["late"] + stats["absent"]


def test_sessions_apply_cancellations_and_room_changes(app_db, student):
    today = clock.today()
    upcoming = att.sessions(today, today + timedelta(days=10), student_id=student["student_id"])
    states = {s["state"] for s in upcoming}
    assert {"scheduled", "cancelled", "changed"} <= states
    moved = next(s for s in upcoming if s["state"] == "changed")
    assert moved["room"] == "Lab-2"
    assert all(s["date"].weekday() == s["weekday"] for s in upcoming)


# ---------------------------------------------------------------- check-in
def _todays_window(teacher, monkeypatch):
    """Pretend 'now' is five minutes into one of the teacher's classes."""
    from datetime import datetime, time

    day = clock.today()
    while not att.sessions(day, day, teacher_id=teacher["teacher_id"]):
        day -= timedelta(days=1)
    s = att.sessions(day, day, teacher_id=teacher["teacher_id"])[0]
    fake_now = datetime.combine(day, time.fromisoformat(s["start_time"])) + timedelta(minutes=5)
    monkeypatch.setattr(clock, "now", lambda: fake_now)
    monkeypatch.setattr(clock, "today", lambda: day)
    _clear_meeting(s)
    return s, att.open_checkin(s["schedule_id"], day, teacher)


def test_student_check_in(app_db, teacher, monkeypatch):
    s, window = _todays_window(teacher, monkeypatch)
    roster = att.roster(s["class_id"])
    first = int(roster["student_id"].iloc[0])
    actor = {"id": int(roster["user_id"].iloc[0]), "username": "x", "full_name": "X"}
    result = att.check_in(first, window["code"].lower(), actor, "device-1")
    assert result == {"status": "Present", "flagged": False, "code": s["code"], "name": s["name"]}
    assert att.existing(s["schedule_id"], s["date"])[first]["method"] == "check-in"
    with pytest.raises(ValueError, match="already checked in"):
        att.check_in(first, window["code"], actor, "device-1")
    with pytest.raises(ValueError, match="not valid"):
        att.check_in(first, "ZZZZZZ", actor, "device-1")
    outsider = db.one("SELECT id FROM students WHERE id NOT IN (SELECT student_id FROM enrollments WHERE class_id = ?)",
                      (s["class_id"],))["id"]
    with pytest.raises(ValueError, match="not enrolled"):
        att.check_in(outsider, window["code"], actor, "device-9")


def test_same_device_check_ins_are_flagged_as_possible_proxy(app_db, teacher, monkeypatch):
    s, window = _todays_window(teacher, monkeypatch)
    roster = att.roster(s["class_id"])
    a, b, c = (int(i) for i in roster["student_id"].iloc[:3])
    actor = {"id": int(roster["user_id"].iloc[0]), "username": "x", "full_name": "X"}
    assert not att.check_in(a, window["code"], actor, "same-phone")["flagged"]
    assert att.check_in(b, window["code"], actor, "same-phone")["flagged"]
    assert not att.check_in(c, window["code"], actor, "other-phone")["flagged"]
    listing = att.checkin_list(s["schedule_id"], s["date"])
    assert listing["flagged"].tolist() == [1, 1, 0]          # both sides of the shared device
    assert any(x["kind"] == "Flagged check-in" for x in analytics.anomalies(s["date"]))


def test_check_in_code_expires_and_late_is_recorded(app_db, teacher, monkeypatch):
    s, window = _todays_window(teacher, monkeypatch)
    roster = att.roster(s["class_id"])
    actor = {"id": int(roster["user_id"].iloc[0]), "username": "x", "full_name": "X"}
    opened = clock.now()
    monkeypatch.setattr(clock, "now", lambda: opened + timedelta(minutes=4))     # 9 min into class: still on time
    assert att.check_in(int(roster["student_id"].iloc[0]), window["code"], actor)["status"] == "Present"
    monkeypatch.setattr(clock, "now", lambda: opened + timedelta(minutes=30))    # code valid for 5 minutes
    with pytest.raises(ValueError, match="expired"):
        att.check_in(int(roster["student_id"].iloc[1]), window["code"], actor)
    late_window = att.open_checkin(s["schedule_id"], s["date"], teacher)          # reopened 35 min into class
    assert att.check_in(int(roster["student_id"].iloc[1]), late_window["code"], actor)["status"] == "Late"


# ----------------------------------------------------------- notifications
def test_absence_notifies_student_and_parent_once(app_db, teacher):
    s = _last_meeting(teacher)
    row = att.roster(s["class_id"]).iloc[1]
    db.execute("DELETE FROM notifications")
    for status in ("Present", "Absent", "Present", "Absent"):
        att.save_attendance(s["schedule_id"], s["date"],
                            [{"student_id": int(row["student_id"]), "status": status}], teacher)
    sent = db.query("SELECT user_id FROM notifications WHERE type = 'absence'")
    assert sorted(r["user_id"] for r in sent) == sorted([int(row["user_id"]), int(row["parent_user_id"])])


def test_muted_notification_type_is_not_delivered(app_db, student):
    notifications.set_pref(student["id"], "absence", False)
    assert not notifications.notify(student["id"], "absence", "Marked absent", "", "k1")
    assert notifications.notify(student["id"], "low_attendance", "Low", "", "k2")
    assert not notifications.notify(student["id"], "low_attendance", "Low", "", "k2")      # duplicate key


def test_low_attendance_alerts_escalate_below_critical(app_db):
    db.execute("DELETE FROM notifications")
    notifications.low_attendance_sweep()
    low = user("isha.reddy")           # around 70%: warned, not escalated in every class
    critical = user("yash.agarwal")    # around 52%: escalated to staff
    assert db.one("SELECT 1 FROM notifications WHERE user_id = ? AND type = 'low_attendance'", (low["id"],))
    assert db.one("SELECT 1 FROM notifications WHERE user_id = ? AND type = 'low_attendance'", (critical["id"],))
    escalations = db.query("SELECT title FROM notifications WHERE user_id = ? AND type = 'escalation'",
                           (user("admin")["id"],))
    assert any("Yash Agarwal" in r["title"] for r in escalations)
    assert notifications.low_attendance_sweep() == 0                                          # once per week


def test_housekeeping_is_idempotent(app_db):
    notifications.run_housekeeping()
    assert notifications.run_housekeeping() == {"reminders": 0, "low_attendance": 0, "patterns": 0}


def test_unmarked_class_sends_reminder_to_its_teacher(app_db, teacher):
    today = clock.today()
    s = att.sessions(today - timedelta(days=6), today - timedelta(days=1), teacher_id=teacher["teacher_id"])[0]
    _clear_meeting(s)
    db.execute("DELETE FROM notifications")
    notifications.run_housekeeping()
    titles = [r["title"] for r in db.query(
        "SELECT title FROM notifications WHERE user_id = ? AND type = 'reminder'", (teacher["id"],))]
    assert any(s["code"] in t and s["date"].strftime("%d %b") in t for t in titles)


# ---------------------------------------------------------------- workflow
def test_flag_is_reviewed_and_record_corrected(app_db, student, teacher):
    absent = db.one(
        """SELECT a.id, a.schedule_id, a.date FROM attendance a JOIN classes c ON c.id = a.class_id
           WHERE a.student_id = ? AND a.status = 'Absent' AND c.teacher_id = ?
             AND a.id NOT IN (SELECT attendance_id FROM flags) LIMIT 1""",
        (student["student_id"], teacher["teacher_id"]))
    with pytest.raises(ValueError, match="at least 10"):
        workflow.create_flag(absent["id"], student["student_id"], "wrong", student)
    other = user("diya.patel")
    with pytest.raises(ValueError, match="your own"):
        workflow.create_flag(absent["id"], other["student_id"], "This is not my record at all", other)
    flag_id = workflow.create_flag(absent["id"], student["student_id"], "I was present for the whole class.", student)
    with pytest.raises(ValueError, match="already flagged"):
        workflow.create_flag(absent["id"], student["student_id"], "I was present for the whole class.", student)
    workflow.resolve_flag(flag_id, True, "Present", "Checked the sign-in sheet.", teacher)
    assert db.one("SELECT status FROM attendance WHERE id = ?", (absent["id"],))["status"] == "Present"
    assert db.one("SELECT status FROM flags WHERE id = ?", (flag_id,))["status"] == "accepted"
    assert db.one("SELECT 1 FROM notifications WHERE user_id = ? AND dedupe_key = ?",
                  (student["id"], f"flag-resolved:{flag_id}"))
    with pytest.raises(ValueError, match="already been resolved"):
        workflow.resolve_flag(flag_id, False, None, "", teacher)


def test_approved_leave_excuses_recorded_absences(app_db, admin):
    student = user("yash.agarwal")
    last_absence = clock.parse_date(db.one(
        "SELECT MAX(date) AS d FROM attendance WHERE student_id = ? AND status = 'Absent'",
        (student["student_id"],))["d"])
    before = att.overall(att.student_stats(student["student_id"]))
    leave_id = workflow.request_leave(student["student_id"], last_absence, last_absence, "Hospital visit", student)
    with pytest.raises(ValueError, match="already have a leave request"):
        workflow.request_leave(student["student_id"], last_absence, last_absence, "Hospital visit", student)
    converted = workflow.decide_leave(leave_id, True, admin)
    after = att.overall(att.student_stats(student["student_id"]))
    assert converted >= 1
    assert after["excused"] == before["excused"] + converted
    assert after["absent"] == before["absent"] - converted
    assert after["pct"] > before["pct"]
    assert student["student_id"] in att.leave_map(last_absence)


def test_schedule_change_validates_and_notifies(app_db, admin, student):
    today = clock.today()
    s = next(x for x in att.sessions(today + timedelta(days=1), today + timedelta(days=14),
                                     student_id=student["student_id"]) if x["state"] == "scheduled")
    with pytest.raises(ValueError, match="does not meet"):
        workflow.change_schedule(s["schedule_id"], s["date"] + timedelta(days=1), "cancelled", admin)
    with pytest.raises(ValueError, match="new room"):
        workflow.change_schedule(s["schedule_id"], s["date"], "room", admin, new_room=" ")
    sent = workflow.change_schedule(s["schedule_id"], s["date"], "cancelled", admin, note="Holiday")
    assert sent >= 24                                   # every enrolled student plus the teacher
    after = next(x for x in att.sessions(s["date"], s["date"], student_id=student["student_id"])
                 if x["schedule_id"] == s["schedule_id"])
    assert after["state"] == "cancelled" and after["change_note"] == "Holiday"
    assert s["schedule_id"] not in {u["schedule_id"] for u in att.unmarked_sessions(s["date"], s["date"])}


# ------------------------------------------------------ analytics & reports
def test_unusual_patterns_are_detected(app_db):
    found = analytics.anomalies()
    titles = " | ".join(a["title"] for a in found)
    assert "Sudden drop: Reyansh Verma" in titles
    assert "Absence streak: Mira Fernandes" in titles
    assert "Unusually low turnout: CS212" in titles
    assert len({a["key"] for a in found}) == len(found)


def test_at_risk_lists_only_students_below_threshold(app_db):
    df = analytics.records(end=clock.today())
    risk = analytics.at_risk(df, 75)
    assert (risk["pct"] < 75).all() and (risk["counted"] >= 4).all()
    assert {"Yash Agarwal", "Varun Hegde"} <= set(risk["student"])
    assert "Diya Patel" not in set(risk["student"])


def test_report_totals_and_exports(app_db):
    today = clock.today()
    report = reports.build(today - timedelta(days=29), today, class_id=1)
    table = reports.summary_table(report)
    totals = report["totals"]
    assert len(table) == 24
    assert table["Present"].sum() == totals["present"] and table["Absent"].sum() == totals["absent"]
    assert totals["counted"] == totals["present"] + totals["late"] + totals["absent"]
    assert reports.to_csv(report).decode().splitlines()[0] == "# Attendance report"
    assert reports.to_excel(report)[:2] == b"PK"
    assert reports.to_pdf(report)[:5] == b"%PDF-"
    one_student = reports.build(today - timedelta(days=29), today, student_id=1)
    assert one_student["by"] == "class" and len(reports.summary_table(one_student)) == 4
    empty = reports.build(today + timedelta(days=1), today + timedelta(days=2))
    assert empty["records"].empty and reports.to_pdf(empty)[:5] == b"%PDF-"
