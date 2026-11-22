"""SAMS - Student Attendance Management System (Streamlit entry point).

Implements the SRS v3.0 by Piyush Vedwal: role-based sign-in, attendance
marking, student history and schedule, reports, notifications and alerts.
"""
from datetime import timedelta

import streamlit as st

from sams import __version__, auth, clock, config, db, notifications, seed, ui
from sams.views import admin, common, parent, student, teacher

st.set_page_config(page_title="SAMS · Student Attendance", page_icon=":material/how_to_reg:", layout="wide")

PAGES = {
    "admin": [
        ("Dashboard", ":material/dashboard:", admin.dashboard),
        ("Alerts", ":material/crisis_alert:", admin.alerts),
        ("Reports", ":material/bar_chart:", admin.reports),
        ("Requests", ":material/inbox:", admin.requests),
        ("Users", ":material/group:", admin.users),
        ("Classes and timetable", ":material/calendar_month:", admin.classes),
        ("Settings", ":material/settings:", admin.settings),
        ("Audit log", ":material/receipt_long:", admin.audit),
    ],
    "teacher": [
        ("Dashboard", ":material/dashboard:", teacher.dashboard),
        ("Mark attendance", ":material/how_to_reg:", teacher.mark_attendance),
        ("Requests", ":material/inbox:", teacher.requests),
        ("Reports", ":material/bar_chart:", teacher.reports),
        ("Schedule", ":material/calendar_month:", teacher.schedule),
    ],
    "student": [
        ("Dashboard", ":material/dashboard:", student.dashboard),
        ("Attendance history", ":material/event_available:", student.history),
        ("Schedule", ":material/calendar_month:", student.schedule),
        ("Check in", ":material/pin:", student.check_in),
        ("Leave and flags", ":material/inbox:", student.requests),
    ],
    "parent": [
        ("Overview", ":material/dashboard:", parent.dashboard),
        ("Attendance history", ":material/event_available:", parent.history),
        ("Schedule", ":material/calendar_month:", parent.schedule),
    ],
}
SHARED = [
    ("Notifications", ":material/notifications:", common.notifications_page),
    ("My account", ":material/account_circle:", common.account_page),
]


@st.cache_resource(show_spinner="Preparing SAMS...")
def bootstrap(day: str) -> bool:
    """Create the database (and demo data) once per process and day."""
    seed.ensure_seeded()
    return True


@st.cache_data(ttl=900, show_spinner=False)
def housekeeping(day: str) -> dict:
    """Reminders and alert sweeps, at most every 15 minutes."""
    return notifications.run_housekeeping()


def sign_in(username: str, password: str) -> None:
    try:
        user = auth.authenticate(username, password)
    except auth.AuthError as e:
        st.session_state["login_error"] = str(e)
        return
    st.session_state.clear()
    st.session_state["user_id"] = user["id"]
    st.session_state["last_seen"] = clock.now()


def sign_out(message: str | None = None) -> None:
    user = current_user()
    if user:
        db.audit("logout" if message is None else "session_timeout", "", user)
    st.session_state.clear()
    if message:
        st.session_state["login_notice"] = message


def current_user() -> dict | None:
    user_id = st.session_state.get("user_id")
    if user_id is None:
        return None
    user = auth.get_user(user_id)
    return user if user and user["is_active"] else None


def login_screen() -> None:
    left, right = st.columns([5, 4], gap="large")
    with left:
        st.title("SAMS")
        st.subheader("Student Attendance Management System")
        st.write(
            "One place for an institute's attendance: teachers mark it, students and parents follow it, "
            "and administrators see the patterns that need attention.")
        st.markdown(
            "- **Teachers** mark a class in seconds, by roster, in bulk, or with a student check-in code\n"
            "- **Students** see a calendar of every class, their percentage and their upcoming schedule\n"
            "- **Parents** are told about absences and low attendance\n"
            "- **Administrators** get reports, exports and alerts on unusual patterns")
        st.caption(f"Version {__version__} · Built from the SAMS Software Requirements Specification by Piyush Vedwal")
    with right:
        with st.container(border=True):
            st.subheader("Sign in")
            notice = st.session_state.pop("login_notice", None)
            if notice:
                st.info(notice)
            error = st.session_state.pop("login_error", None)
            if error:
                st.error(error)
            with st.form("login"):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                if st.form_submit_button("Sign in", type="primary", width="stretch"):
                    sign_in(username, password)
                    st.rerun()
        if config.DEMO_MODE:
            with st.container(border=True):
                st.markdown("**Try the demo**")
                st.caption("Sign in as any role. The data is fictional and rebuilds itself every day.")
                cols = st.columns(4)
                for col, (role, username) in zip(cols, config.DEMO_ACCOUNTS.items()):
                    if col.button(role.capitalize(), key=f"demo_{role}", width="stretch"):
                        sign_in(username, config.DEMO_PASSWORD)
                        st.rerun()
                st.caption(f"Or type a username (for example `{config.DEMO_ACCOUNTS['teacher']}`) with the password "
                           f"`{config.DEMO_PASSWORD}`.")


def main() -> None:
    bootstrap(clock.today().isoformat())
    ui.inject_css()
    user = current_user()
    if user is None:
        if "user_id" in st.session_state:
            st.session_state.clear()
        login_screen()
        return

    # Automatic sign-out after inactivity (SRS 6).
    timeout = timedelta(minutes=db.get_setting("session_timeout_min", int))
    if clock.now() - st.session_state.get("last_seen", clock.now()) > timeout:
        sign_out("You were signed out after a period of inactivity.")
        st.rerun()
    st.session_state["last_seen"] = clock.now()
    housekeeping(clock.today().isoformat())

    pages = PAGES[user["role"]] + SHARED
    names = [name for name, _, _ in pages]
    icons = {name: icon for name, icon, _ in pages}
    target = st.session_state.pop("nav_target", None)
    if target in names:
        st.session_state["nav"] = target
    if st.session_state.get("nav") not in names:
        st.session_state["nav"] = names[0]
    unread = notifications.unread_count(user["id"])

    def label(name: str) -> str:
        suffix = f" ({unread})" if name == "Notifications" and unread else ""
        return f"{icons[name]} {name}{suffix}"

    with st.sidebar:
        st.markdown("## SAMS")
        st.markdown(f"**{user['full_name']}**")
        st.caption(auth.ROLE_LABELS[user["role"]])
        choice = st.radio("Go to", names, format_func=label, key="nav", label_visibility="collapsed")
        st.divider()
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            sign_out()
            st.rerun()
        st.caption(f"{clock.now().strftime('%a %d %b %Y, %H:%M')} IST")

    dict((name, view) for name, _, view in pages)[choice](user)


main()
