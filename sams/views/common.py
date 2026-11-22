"""Screens shared by more than one role."""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from .. import analytics, auth, clock, db, notifications, reports, ui, workflow
from .. import attendance as att

STATE_LABEL = {"scheduled": "Scheduled", "cancelled": "Cancelled", "changed": "Changed"}


# --------------------------------------------------------------- notifications
def notifications_page(user: dict) -> None:
    st.title("Notifications")
    rows = notifications.inbox(user["id"])
    unread = sum(1 for r in rows if r["read_at"] is None)
    left, right = st.columns([3, 1], vertical_alignment="center")
    left.caption(f"{unread} unread of {len(rows)}. Alerts are delivered in the app; email and SMS are not connected "
                 "in this deployment.")
    if unread and right.button("Mark all as read", width="stretch"):
        notifications.mark_read(user["id"])
        st.rerun()
    if not rows:
        st.info("You have no notifications yet.")
        return
    kinds = sorted({r["type"] for r in rows})
    chosen = st.pills("Filter", kinds, selection_mode="multi", format_func=lambda k: notifications.TYPES.get(k, k),
                      label_visibility="collapsed")
    for r in rows:
        if chosen and r["type"] not in chosen:
            continue
        with st.container(border=True):
            head, when = st.columns([5, 1])
            marker = ":material/mark_email_unread:" if r["read_at"] is None else ":material/drafts:"
            head.markdown(f"{marker} **{r['title']}**")
            when.caption(r["created_at"][5:16])
            if r["body"]:
                st.write(r["body"])
            st.caption(notifications.TYPES.get(r["type"], r["type"]))


# --------------------------------------------------------------------- account
def account_page(user: dict) -> None:
    st.title("My account")
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Profile")
        st.write(f"**{user['full_name']}**")
        st.caption(f"{auth.ROLE_LABELS[user['role']]} · username `{user['username']}`")
        if user.get("email"):
            st.caption(user["email"])
        st.subheader("Change password")
        with st.form("change_password", clear_on_submit=True):
            current = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password",
                                help="At least 8 characters with upper and lower case, a digit and a special character.")
            again = st.text_input("Repeat new password", type="password")
            if st.form_submit_button("Update password", type="primary"):
                if new != again:
                    st.error("The two new passwords do not match.")
                else:
                    try:
                        auth.change_password(user, current, new)
                        st.success("Password updated.")
                    except auth.AuthError as e:
                        st.error(str(e))
    with right:
        st.subheader("Notification preferences")
        st.caption("Choose which alerts you receive.")
        current_prefs = notifications.prefs(user["id"], user["role"])
        for kind, enabled in current_prefs.items():
            value = st.toggle(notifications.TYPES[kind], value=enabled, key=f"pref_{kind}")
            if value != enabled:
                notifications.set_pref(user["id"], kind, value)
                st.toast(f"{notifications.TYPES[kind]}: {'on' if value else 'off'}")


# -------------------------------------------------------------------- schedule
def timetable_grid(student_id=None, teacher_id=None, section=None) -> pd.DataFrame:
    sql = """SELECT s.weekday, s.start_time, s.end_time, s.room, c.code, c.section
             FROM schedules s JOIN classes c ON c.id = s.class_id WHERE s.active = 1"""
    params: list = []
    if student_id is not None:
        sql += " AND c.id IN (SELECT class_id FROM enrollments WHERE student_id = ?)"
        params.append(student_id)
    if teacher_id is not None:
        sql += " AND c.teacher_id = ?"
        params.append(teacher_id)
    if section is not None:
        sql += " AND c.section = ?"
        params.append(section)
    slots = db.frame(sql, params)
    if slots.empty:
        return pd.DataFrame()
    slots["Time"] = slots["start_time"] + " - " + slots["end_time"]
    slots["cell"] = slots["code"] + " · " + slots["room"].fillna("")
    grid = slots.pivot_table(index="Time", columns="weekday", values="cell", aggfunc=" / ".join).sort_index()
    days = sorted(grid.columns)
    grid = grid.reindex(columns=range(0, max(5, max(days) + 1)))
    grid.columns = [att.WEEKDAYS[d] for d in grid.columns]
    return grid.fillna("")


def upcoming_table(sessions: list[dict], stats: pd.DataFrame | None = None, threshold: float | None = None):
    rows = []
    by_class = stats.set_index("class_id") if stats is not None and not stats.empty else None
    for s in sessions:
        row = {
            "Date": s["date"].strftime("%a %d %b"), "Time": f"{s['start_time']} - {s['end_time']}",
            "Class": f"{s['code']} {s['name']}", "Room": s["room"] or "", "Teacher": s["teacher_name"] or "",
            "Status": STATE_LABEL[s["state"]] + (f": {s['change_note']}" if s["change_note"] else ""),
        }
        if by_class is not None and s["class_id"] in by_class.index:
            c = by_class.loc[s["class_id"]]
            row["Your attendance"] = c["pct"]
            can_miss, need = att.allowance(int(c["attended"]), int(c["counted"]), threshold)
            if c["counted"] == 0:
                row["Requirement"] = "No classes counted yet"
            elif need:
                row["Requirement"] = f"Attend the next {need} to reach {threshold:.0f}%"
            else:
                row["Requirement"] = f"Can miss {can_miss} more" if can_miss else "Cannot miss any"
        rows.append(row)
    return pd.DataFrame(rows)


def schedule_page(user: dict, student_id=None, teacher_id=None) -> None:
    st.title("Class schedule")
    today = clock.today()
    threshold = db.get_setting("threshold_low", float)
    grid = timetable_grid(student_id=student_id, teacher_id=teacher_id)
    st.subheader("Weekly timetable")
    if grid.empty:
        st.info("No classes are scheduled.")
        return
    st.dataframe(grid, width="stretch")
    days = st.segmented_control("Show", [7, 14, 28], default=14, format_func=lambda d: f"Next {d} days")
    upcoming = att.sessions(today, today + timedelta(days=(days or 14) - 1), student_id=student_id,
                            teacher_id=teacher_id)
    upcoming = [s for s in upcoming if att.session_end(s) >= clock.now() or s["date"] > today]
    st.subheader("Upcoming classes")
    changed = [s for s in upcoming if s["state"] != "scheduled"]
    if changed:
        for s in changed:
            what = "cancelled" if s["state"] == "cancelled" else f"changed ({s['start_time']}, {s['room']})"
            st.warning(f"**{s['code']}** on {s['date'].strftime('%a %d %b')} is {what}"
                       + (f": {s['change_note']}" if s["change_note"] else "."), icon=":material/event_busy:")
    stats = att.student_stats(student_id) if student_id is not None else None
    table = upcoming_table(upcoming, stats, threshold)
    if table.empty:
        st.info("No classes in this period.")
        return
    config = {}
    if "Your attendance" in table:
        config["Your attendance"] = st.column_config.NumberColumn(format="%.1f%%")
    st.dataframe(table, width="stretch", hide_index=True, column_config=config)
    if student_id is not None:
        st.caption(f"Requirement is measured against the {threshold:.0f}% attendance threshold for each class.")


# --------------------------------------------------------------------- reports
PERIODS = ["Today", "This week", "This month", "Last 30 days", "Term to date", "Custom"]


def _period(choice: str, key: str) -> tuple[date, date]:
    today = clock.today()
    term_start, _ = att.term_bounds()
    if choice == "Today":
        return today, today
    if choice == "This week":
        return clock.week_start(today), today
    if choice == "This month":
        return today.replace(day=1), today
    if choice == "Last 30 days":
        return today - timedelta(days=29), today
    if choice == "Term to date":
        return term_start, today
    picked = st.date_input("Date range", (today - timedelta(days=13), today), max_value=today, key=key)
    if isinstance(picked, (tuple, list)) and len(picked) == 2:
        return picked[0], picked[1]
    return today - timedelta(days=13), today


def report_page(user: dict, teacher_id=None) -> None:
    st.title("Reports")
    st.caption("Daily, weekly, monthly or custom reports by class and student, with export to CSV, Excel and PDF.")
    classes = db.frame(
        "SELECT id, code, name, section FROM classes" + (" WHERE teacher_id = ?" if teacher_id else "") + " ORDER BY code",
        (teacher_id,) if teacher_id else ())
    c1, c2, c3 = st.columns([2, 2, 2])
    choice = c1.selectbox("Period", PERIODS, index=3)
    class_label = {int(r.id): f"{r.code} {r.name} ({r.section})" for r in classes.itertuples()}
    all_label = "All my classes" if teacher_id else "All classes"
    class_id = c2.selectbox("Class", [None] + list(class_label), format_func=lambda i: class_label.get(i, all_label))
    students = db.frame(
        """SELECT DISTINCT st.id, st.roll_no, u.full_name FROM students st JOIN users u ON u.id = st.user_id
           JOIN enrollments e ON e.student_id = st.id JOIN classes c ON c.id = e.class_id
           WHERE 1 = 1""" + (" AND c.id = ?" if class_id else "") + (" AND c.teacher_id = ?" if teacher_id else "")
        + " ORDER BY st.roll_no",
        tuple(v for v in (class_id, teacher_id) if v))
    student_label = {int(r.id): f"{r.roll_no} {r.full_name}" for r in students.itertuples()}
    student_id = c3.selectbox("Student", [None] + list(student_label),
                              format_func=lambda i: student_label.get(i, "All students"))
    start, end = _period(choice, "report_range")

    report = reports.build(start, end, class_id=class_id, student_id=student_id, teacher_id=teacher_id)
    totals = report["totals"]
    st.subheader(report["scope"])
    st.caption(f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}")
    if report["records"].empty:
        st.info("No attendance was recorded for this selection.")
        return
    m = st.columns(5)
    m[0].metric("Attendance", ui.pct_text(totals["pct"]), border=True)
    m[1].metric("Present", totals["present"], border=True)
    m[2].metric("Late", totals["late"], border=True)
    m[3].metric("Absent", totals["absent"], border=True)
    m[4].metric("Excused", totals["excused"], border=True)

    if len(report["daily"]) > 1:
        st.subheader("Attendance by day")
        ui.trend_chart(report["daily"], report["threshold"])

    st.subheader("By class" if report["by"] == "class" else "By student")
    table = reports.summary_table(report)
    st.dataframe(table, width="stretch", hide_index=True, column_config={
        "Attendance %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100)})

    risk = report["at_risk"]
    if not risk.empty and not student_id:
        st.subheader(f"Below {report['threshold']:.0f}% in this period")
        st.dataframe(
            risk.rename(columns={"roll_no": "Roll no", "student": "Student", "code": "Class",
                                 "counted": "Classes counted", "absent": "Absent", "pct": "Attendance %"})[
                ["Roll no", "Student", "Class", "Classes counted", "Absent", "Attendance %"]],
            width="stretch", hide_index=True,
            column_config={"Attendance %": st.column_config.NumberColumn(format="%.1f%%")})

    st.subheader("Export")
    stem = f"sams_attendance_{start.isoformat()}_{end.isoformat()}"
    d1, d2, d3, _ = st.columns([1, 1, 1, 3])
    d1.download_button("CSV", reports.to_csv(report), f"{stem}.csv", "text/csv", width="stretch",
                       icon=":material/download:")
    d2.download_button("Excel", reports.to_excel(report), f"{stem}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch", icon=":material/download:")
    d3.download_button("PDF", reports.to_pdf(report), f"{stem}.pdf", "application/pdf", width="stretch",
                       icon=":material/download:")
    with st.expander(f"All {len(report['records'])} records"):
        st.dataframe(reports.records_table(report), width="stretch", hide_index=True)
    signature = (report["scope"], start, end)
    if st.session_state.get("_last_report") != signature:
        db.audit("report_viewed", f"{report['scope']} {start.isoformat()}..{end.isoformat()}", user)
        st.session_state["_last_report"] = signature


# ------------------------------------------------------------ student overview
def requirement_text(row, threshold: float) -> str:
    if row.counted == 0:
        return "No classes counted yet"
    can_miss, need = att.allowance(int(row.attended), int(row.counted), threshold)
    if need:
        return f"Below {threshold:.0f}%: attend the next {need} classes to recover"
    return f"On track: can miss {can_miss} more" if can_miss else "On track: cannot miss any"


def student_overview(student_id: int) -> pd.DataFrame:
    """Headline numbers and per-class standing for one student."""
    threshold = db.get_setting("threshold_low", float)
    stats = att.student_stats(student_id)
    total = att.overall(stats)
    m = st.columns(4)
    gap = None if total["pct"] is None else round(total["pct"] - threshold, 1)
    m[0].metric("Overall attendance", ui.pct_text(total["pct"]),
                delta=None if gap is None else f"{gap:+.1f} pts vs required {threshold:.0f}%", border=True)
    m[1].metric("Classes attended", f"{total['attended']} of {total['counted']}", border=True)
    m[2].metric("Late", total["late"], border=True)
    m[3].metric("Absent", total["absent"], help=f"{total['excused']} excused classes are not counted.", border=True)

    below = stats[(stats["counted"] > 0) & (stats["pct"] < threshold)]
    for r in below.itertuples():
        _, need = att.allowance(int(r.attended), int(r.counted), threshold)
        st.warning(f"**{r.code} {r.name}** is at {r.pct:.1f}%, below the required {threshold:.0f}%. "
                   f"Attend the next {need} classes to recover.", icon=":material/warning:")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Attendance by class")
        ui.percent_bars(stats.assign(course=stats["code"] + " " + stats["name"]), "course", threshold)
    with right:
        st.subheader("Where you stand")
        table = pd.DataFrame({
            "Class": stats["code"],
            "Attended": [f"{a} of {c}" for a, c in zip(stats["attended"], stats["counted"])],
            "Requirement": [requirement_text(r, threshold) for r in stats.itertuples()],
        })
        st.dataframe(table, width="stretch", hide_index=True)
    return stats


def attendance_calendar(student_id: int, prefix: str, actor: dict | None = None, can_flag: bool = False) -> None:
    """Calendar view with filters, a period summary and the record list (SRS 4.4)."""
    today = clock.today()
    term_start, _ = att.term_bounds()
    key = f"{prefix}_month"
    if key not in st.session_state:
        st.session_state[key] = today.replace(day=1)
    month = st.session_state[key]
    all_records = att.history(student_id)
    courses = sorted(all_records["code"].unique()) if not all_records.empty else []

    nav = st.columns([1, 1, 8], vertical_alignment="center")
    if nav[0].button(":material/chevron_left:", key=f"{prefix}_prev", help="Previous month",
                     disabled=month <= term_start.replace(day=1), width="stretch"):
        st.session_state[key] = (month - timedelta(days=1)).replace(day=1)
        st.rerun()
    if nav[1].button(":material/chevron_right:", key=f"{prefix}_next", help="Next month",
                     disabled=month >= today.replace(day=1), width="stretch"):
        st.session_state[key] = (month + timedelta(days=32)).replace(day=1)
        st.rerun()
    nav[2].subheader(month.strftime("%B %Y"))
    filters = st.columns(2)
    chosen_courses = filters[0].multiselect("Class", courses, placeholder="All classes", key=f"{prefix}_courses")
    chosen_status = filters[1].multiselect("Status", list(att.STATUSES), placeholder="All statuses",
                                           key=f"{prefix}_status")

    view = all_records.copy()
    if chosen_courses:
        view = view[view["code"].isin(chosen_courses)]
    if chosen_status:
        view = view[view["status"].isin(chosen_status)]
    in_month = view[pd.to_datetime(view["date"]).dt.strftime("%Y-%m") == month.strftime("%Y-%m")]

    st.html(ui.calendar_html(month.year, month.month, in_month, today))

    counts = in_month["status"].value_counts()
    present, late, absent, excused = (int(counts.get(s, 0)) for s in ("Present", "Late", "Absent", "Excused"))
    st.subheader(f"Summary for {month.strftime('%B')}")
    m = st.columns(5)
    m[0].metric("Attendance", ui.pct_text(att.percentage(present, late, absent)), border=True)
    m[1].metric("Present", present, border=True)
    m[2].metric("Late", late, border=True)
    m[3].metric("Absent", absent, border=True)
    m[4].metric("Excused", excused, border=True)

    st.subheader("Records")
    search = st.text_input("Search by date, class or note", placeholder="e.g. CS201, 2026-11-16, unwell",
                           key=f"{prefix}_search", label_visibility="collapsed")
    listing = in_month
    if search:
        text = (listing["date"] + " " + listing["code"] + " " + listing["name"] + " "
                + listing["note"].fillna("") + " " + listing["status"]).str.lower()
        listing = listing[text.str.contains(search.strip().lower(), regex=False)]
    if listing.empty:
        st.info("No records match these filters.")
    else:
        st.dataframe(pd.DataFrame({
            "Date": pd.to_datetime(listing["date"]).dt.strftime("%a %d %b"), "Time": listing["start_time"],
            "Class": listing["code"] + " " + listing["name"], "Status": listing["status"],
            "Note": listing["note"].fillna(""), "Marked by": listing["method"].str.replace("-", " ").str.capitalize(),
        }), width="stretch", hide_index=True)

    if can_flag and actor is not None and not in_month.empty:
        with st.expander("Something wrong? Flag a record for review", icon=":material/flag:"):
            labels = {int(r.attendance_id): f"{pd.Timestamp(r.date).strftime('%a %d %b')} · {r.code} {r.start_time} · {r.status}"
                      for r in in_month.itertuples()}
            with st.form(f"{prefix}_flag", clear_on_submit=True):
                record = st.selectbox("Record", list(labels), format_func=labels.get)
                message = st.text_area("What is wrong?", placeholder="e.g. I was present and answered the roll call.",
                                       max_chars=500)
                if st.form_submit_button("Send to teacher", type="primary"):
                    try:
                        workflow.create_flag(record, student_id, message, actor)
                        st.success("Sent. Your teacher will review this record.")
                    except ValueError as e:
                        st.error(str(e))
