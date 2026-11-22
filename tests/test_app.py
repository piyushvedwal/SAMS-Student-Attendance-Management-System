"""End-to-end checks that drive the Streamlit app itself."""
from datetime import timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sams import clock, db, workflow
from sams import attendance as att

from conftest import user

APP = str(Path(__file__).resolve().parent.parent / "app.py")
EXPECTED_PAGES = {
    "Admin": ["Dashboard", "Alerts", "Reports", "Requests", "Users", "Classes and timetable", "Settings",
              "Audit log", "Notifications", "My account"],
    "Teacher": ["Dashboard", "Mark attendance", "Requests", "Reports", "Schedule", "Notifications", "My account"],
    "Student": ["Dashboard", "Attendance history", "Schedule", "Check in", "Leave and flags", "Notifications",
                "My account"],
    "Parent": ["Overview", "Attendance history", "Schedule", "Notifications", "My account"],
}


def start() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert not at.exception
    return at


def demo_login(role: str) -> AppTest:
    at = start()
    next(b for b in at.button if b.label == role).click().run()
    assert not at.exception, at.exception
    return at


def pages(at: AppTest) -> list[str]:
    """Sidebar entries without their icon and unread counter."""
    return [o.split(": ", 1)[-1].split(" (")[0] for o in at.sidebar.radio[0].options]


def go(at: AppTest, page: str) -> AppTest:
    nav = at.sidebar.radio[0]
    nav.set_value(nav.options[pages(at).index(page)]).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def test_wrong_password_is_rejected(app_db):
    at = start()
    at.text_input[0].set_value("admin")
    at.text_input[1].set_value("not-the-password")
    button(at, "Sign in").click().run()
    assert [e.value for e in at.error] == ["Incorrect username or password."]
    assert len(at.sidebar.radio) == 0


def test_typed_credentials_sign_in(app_db):
    at = start()
    at.text_input[0].set_value("Kavita.Rao")
    at.text_input[1].set_value("Demo@1234")
    button(at, "Sign in").click().run()
    assert at.title[0].value == "Welcome, Dr. Kavita Rao"


@pytest.mark.parametrize("role", list(EXPECTED_PAGES))
def test_each_role_sees_only_its_own_pages_and_all_render(app_db, role):
    at = demo_login(role)
    assert pages(at) == EXPECTED_PAGES[role]
    for page in EXPECTED_PAGES[role]:
        go(at, page)
        assert at.title, f"{role}: {page} rendered nothing"


def test_sign_out_returns_to_login(app_db):
    at = demo_login("Student")
    button(at, "Sign out").click().run()
    assert len(at.sidebar.radio) == 0
    assert db.one("SELECT 1 FROM audit_log WHERE action = 'logout' AND username = 'aarav.sharma'")


def test_inactive_session_is_signed_out(app_db):
    at = demo_login("Teacher")
    at.session_state["last_seen"] = clock.now() - timedelta(hours=3)
    at.run()
    assert len(at.sidebar.radio) == 0
    assert any("inactivity" in i.value for i in at.info)


def test_teacher_marks_the_pending_class(app_db):
    teacher = user("kavita.rao")
    today = clock.today()
    pending = att.unmarked_sessions(today - timedelta(days=14), today, teacher_id=teacher["teacher_id"])
    assert len(pending) == 1
    s = pending[0]
    at = demo_login("Teacher")
    button(at, "Mark now").click().run()
    assert at.title[0].value == "Mark attendance"
    assert any("Not marked yet" in w.value for w in at.warning)
    button(at, "All absent").click().run()
    button(at, "Save attendance").click().run()
    assert not at.exception
    assert any(f"Attendance saved for {s['code']}" in m.value for m in at.success)
    saved = att.existing(s["schedule_id"], s["date"])
    assert len(saved) == len(att.roster(s["class_id"]))
    assert {r["status"] for r in saved.values()} == {"Absent"}
    assert {r["method"] for r in saved.values()} == {"bulk"}
    assert not att.unmarked_sessions(today - timedelta(days=14), today, teacher_id=teacher["teacher_id"])
    # every newly absent student was told
    told = db.one("SELECT COUNT(*) AS n FROM notifications WHERE dedupe_key LIKE ?",
                  (f"absence:%:{s['schedule_id']}:{s['date'].isoformat()}",))["n"]
    assert told == 2 * len(saved)


def test_student_checks_in_with_the_teachers_code(app_db, monkeypatch):
    from datetime import datetime, time

    teacher, student = user("kavita.rao"), user("aarav.sharma")
    day = clock.today()
    while not att.sessions(day, day, teacher_id=teacher["teacher_id"], student_id=student["student_id"]):
        day -= timedelta(days=1)
    s = att.sessions(day, day, teacher_id=teacher["teacher_id"], student_id=student["student_id"])[0]
    fake_now = datetime.combine(day, time.fromisoformat(s["start_time"])) + timedelta(minutes=3)
    monkeypatch.setattr(clock, "now", lambda: fake_now)
    monkeypatch.setattr(clock, "today", lambda: day)
    db.set_setting("seeded_on", day.isoformat())     # otherwise the demo data would rebuild for the "new" day
    db.execute("DELETE FROM flags")
    db.execute("DELETE FROM attendance WHERE schedule_id = ? AND date = ?", (s["schedule_id"], day.isoformat()))
    window = att.open_checkin(s["schedule_id"], day, teacher)

    at = go(demo_login("Student"), "Check in")
    at.text_input[0].set_value("WRONG1")
    button(at, "Check in").click().run()
    assert any("not valid or has expired" in e.value for e in at.error)
    assert student["student_id"] not in att.existing(s["schedule_id"], day)

    at = go(demo_login("Student"), "Check in")
    at.text_input[0].set_value(window["code"])
    button(at, "Check in").click().run()
    assert any(f"Checked in to {s['code']}" in m.value for m in at.success), [e.value for e in at.error]
    record = att.existing(s["schedule_id"], day)[student["student_id"]]
    assert (record["status"], record["method"]) == ("Present", "check-in")


def test_admin_approves_a_leave_request(app_db):
    pending = workflow.leaves(status="pending")
    assert len(pending) == 1
    at = demo_login("Admin")
    go(at, "Requests")
    button(at, "Approve").click().run()
    assert not at.exception
    assert any("Leave approved" in m.value for m in at.success)
    assert workflow.leaves(status="pending").empty
    student_user = db.one("SELECT user_id FROM students WHERE id = ?", (int(pending["student_id"].iloc[0]),))
    assert db.one("SELECT 1 FROM notifications WHERE user_id = ? AND title LIKE 'Leave approved%'",
                  (student_user["user_id"],))


def test_admin_changes_thresholds(app_db):
    at = demo_login("Admin")
    go(at, "Settings")
    low = next(n for n in at.number_input if n.label.startswith("Low attendance"))
    low.set_value(80)
    button(at, "Save settings").click().run()
    assert not at.exception
    assert db.get_setting("threshold_low", int) == 80
    assert db.one("SELECT 1 FROM audit_log WHERE action = 'settings_changed'")
