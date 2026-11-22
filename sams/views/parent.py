"""Parent portal: read-only view of a child's attendance (SRS 2.3.4)."""
import streamlit as st

from .. import db
from . import common


def _child(user: dict) -> int | None:
    children = user.get("children") or []
    if not children:
        st.info("No student is linked to this account. Please contact the administrator.")
        return None
    names = {r["id"]: f"{r['full_name']} ({r['roll_no']})" for r in db.query(
        f"""SELECT st.id, st.roll_no, u.full_name FROM students st JOIN users u ON u.id = st.user_id
            WHERE st.id IN ({','.join('?' * len(children))})""", children)}
    if len(children) == 1:
        st.caption(f"Viewing: {names[children[0]]}")
        return children[0]
    return st.selectbox("Student", children, format_func=names.get)


def dashboard(user: dict) -> None:
    st.title("Attendance overview")
    child = _child(user)
    if child is not None:
        common.student_overview(child)


def history(user: dict) -> None:
    st.title("Attendance history")
    child = _child(user)
    if child is not None:
        common.attendance_calendar(child, "parent_cal")


def schedule(user: dict) -> None:
    child = _child(user)
    if child is not None:
        common.schedule_page(user, student_id=child)
