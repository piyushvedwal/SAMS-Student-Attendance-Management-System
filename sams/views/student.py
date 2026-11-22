"""Student console (SRS 3.1.3, 4.4, 4.5)."""
from datetime import timedelta

import pandas as pd
import streamlit as st

from .. import clock, db, workflow
from .. import attendance as att
from . import common


def dashboard(user: dict) -> None:
    student_id = user["student_id"]
    profile = db.one("SELECT roll_no, program, section, semester FROM students WHERE id = ?", (student_id,))
    st.title(f"Hello, {user['full_name'].split()[0]}")
    st.caption(f"{profile['roll_no']} · {profile['program']} · {profile['section']} · Semester {profile['semester']}")
    stats = common.student_overview(student_id)

    today = clock.today()
    upcoming = [s for s in att.sessions(today, today + timedelta(days=6), student_id=student_id)
                if att.session_end(s) >= clock.now()]
    st.subheader("Next classes")
    if not upcoming:
        st.info("No classes in the next 7 days.")
    else:
        threshold = db.get_setting("threshold_low", float)
        st.dataframe(common.upcoming_table(upcoming[:6], stats, threshold).drop(columns=["Teacher"]),
                     width="stretch", hide_index=True,
                     column_config={"Your attendance": st.column_config.NumberColumn(format="%.1f%%")})


def history(user: dict) -> None:
    st.title("Attendance history")
    st.caption("Every class you were marked for. Use the filters to narrow by class or status.")
    common.attendance_calendar(user["student_id"], "student_cal", actor=user, can_flag=True)


def schedule(user: dict) -> None:
    common.schedule_page(user, student_id=user["student_id"])


def _fingerprint() -> str | None:
    """A coarse device signature (network address + browser) used only to spot
    several students checking in from one device."""
    import hashlib

    try:
        headers = st.context.headers
        agent = headers.get("User-Agent", "")
        address = headers.get("X-Forwarded-For", "") or (st.context.ip_address or "")
    except Exception:
        return None
    if not agent and not address:
        return None
    return hashlib.sha256(f"{address}|{agent}".encode()).hexdigest()[:32]


def check_in(user: dict) -> None:
    st.title("Check in")
    st.caption("When your teacher opens check-in, enter the 6-character code shown in class.")
    with st.form("checkin", clear_on_submit=True):
        code = st.text_input("Check-in code", max_chars=6, placeholder="e.g. K7M2QX")
        if st.form_submit_button("Check in", type="primary"):
            try:
                result = att.check_in(user["student_id"], code, user, _fingerprint())
                st.success(f"Checked in to {result['code']} {result['name']} as {result['status']}.")
                if result["flagged"]:
                    st.warning("This check-in was flagged for your teacher to review.")
            except ValueError as e:
                st.error(str(e))
    today = clock.today()
    mine = att.history(user["student_id"], today, today)
    st.subheader("Today")
    if mine.empty:
        st.info("No attendance recorded for you today yet.")
    else:
        st.dataframe(pd.DataFrame({"Time": mine["start_time"], "Class": mine["code"] + " " + mine["name"],
                                   "Status": mine["status"],
                                   "Marked by": mine["method"].str.replace("-", " ").str.capitalize()}),
                     width="stretch", hide_index=True)


def requests(user: dict) -> None:
    st.title("Leave and flags")
    student_id = user["student_id"]
    leave_tab, flag_tab = st.tabs(["Leave requests", "Flagged records"])
    with leave_tab:
        today = clock.today()
        with st.form("leave", clear_on_submit=True):
            st.markdown("**Request leave**")
            c1, c2 = st.columns(2)
            start = c1.date_input("From", today + timedelta(days=1), min_value=today - timedelta(days=14))
            end = c2.date_input("To", today + timedelta(days=1), min_value=today - timedelta(days=14))
            reason = st.text_input("Reason", max_chars=300, placeholder="e.g. Medical appointment")
            if st.form_submit_button("Submit request", type="primary"):
                try:
                    workflow.request_leave(student_id, start, end, reason, user)
                    st.success("Request sent to your teachers and the administrator.")
                except ValueError as e:
                    st.error(str(e))
        mine = workflow.leaves(student_id=student_id)
        if mine.empty:
            st.info("You have not requested any leave.")
        else:
            st.dataframe(pd.DataFrame({
                "From": mine["start_date"], "To": mine["end_date"], "Reason": mine["reason"],
                "Status": mine["status"].str.capitalize(), "Decided by": mine["decided_by"].fillna(""),
            }), width="stretch", hide_index=True)
            st.caption("Approved leave is recorded as Excused and does not count against your attendance.")
    with flag_tab:
        mine = workflow.flags(student_id=student_id)
        if mine.empty:
            st.info("You have not flagged any records. To flag one, open Attendance history.")
        else:
            st.dataframe(pd.DataFrame({
                "Date": mine["date"], "Class": mine["code"], "Recorded as": mine["status"],
                "Your message": mine["message"], "Review": mine["flag_status"].str.capitalize(),
                "Teacher's response": mine["response"].fillna(""),
            }), width="stretch", hide_index=True)
