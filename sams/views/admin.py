"""Administrator screens: analytics, alerts, users, timetable, settings, audit."""
from datetime import time, timedelta

import pandas as pd
import streamlit as st

from .. import analytics, auth, clock, db, notifications, seed, ui, workflow
from .. import attendance as att
from . import common, teacher

RANGES = {"Last 14 days": 14, "Last 30 days": 30, "Term to date": None}


def dashboard(user: dict) -> None:
    st.title("Institute overview")
    today = clock.today()
    term_start, _ = att.term_bounds()
    threshold = db.get_setting("threshold_low", float)
    choice = st.segmented_control("Period", list(RANGES), default="Last 30 days", label_visibility="collapsed")
    days = RANGES.get(choice or "Last 30 days")
    start = term_start if days is None else max(term_start, today - timedelta(days=days - 1))
    records = analytics.records(start, today)
    term = records if days is None else analytics.records(term_start, today)
    counted = records[records["status"] != "Excused"]
    overall = round(100 * counted["status"].isin(["Present", "Late"]).mean(), 1) if len(counted) else None
    risk = analytics.at_risk(term, threshold, db.get_setting("min_sessions_for_alert", int))
    pending = att.unmarked_sessions(today - timedelta(days=7), today)
    patterns = analytics.anomalies()
    students = db.one("SELECT COUNT(*) AS n FROM students st JOIN users u ON u.id = st.user_id WHERE u.is_active = 1")

    m = st.columns(5)
    m[0].metric("Attendance", ui.pct_text(overall), help=f"All classes, {(choice or 'Last 30 days').lower()}",
                border=True)
    m[1].metric("Active students", students["n"], border=True)
    m[2].metric(f"Below {threshold:.0f}%", risk["student_id"].nunique(),
                help="Students below the threshold in at least one class, term to date", border=True)
    m[3].metric("Not marked", len(pending), help="Classes finished in the last 7 days with no attendance",
                border=True)
    m[4].metric("Unusual patterns", len(patterns), border=True)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Attendance by day")
        ui.trend_chart(analytics.daily(records), threshold)
    with right:
        st.subheader("Attendance by class")
        by_class = analytics.by_class(records)
        ui.percent_bars(by_class.assign(label=by_class["code"] + " · " + by_class["section"]), "label", threshold,
                        row_height=30)
    st.subheader("Status mix by week")
    ui.status_mix_chart(analytics.weekly_mix(records))

    st.subheader(f"Students below {threshold:.0f}% (term to date)")
    if risk.empty:
        st.success("No student is below the threshold.")
    else:
        critical = db.get_setting("threshold_critical", float)
        st.dataframe(pd.DataFrame({
            "Roll no": risk["roll_no"], "Student": risk["student"], "Class": risk["code"],
            "Classes counted": risk["counted"], "Absent": risk["absent"], "Attendance %": risk["pct"],
            "Level": ["Critical" if p < critical else "Low" for p in risk["pct"]],
        }), width="stretch", hide_index=True, height=320,
            column_config={"Attendance %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100)})


def alerts(user: dict) -> None:
    st.title("Alerts")
    st.caption("Patterns that need a person to look at them. Each one is also sent to administrators as a notification.")
    today = clock.today()
    patterns = analytics.anomalies()
    st.subheader(f"Unusual attendance patterns ({len(patterns)})")
    if not patterns:
        st.success("Nothing unusual in the last 14 days.")
    for a in patterns:
        icon = ":material/error:" if a["severity"] == "high" else ":material/warning:"
        box = st.error if a["severity"] == "high" else st.warning
        box(f"**{a['title']}**  \n{a['detail']}  \n*{a['kind']} · {a['severity']} priority*", icon=icon)
    with st.expander("How these are detected"):
        st.markdown(
            "- **Sudden drop**: attendance over the last 14 days is at least 30 points below the student's earlier record.\n"
            "- **Absence streak**: the student's 4 or more most recent classes are all absent.\n"
            "- **Class outlier**: one class meeting is more than 2.5 standard deviations (and 25 points) below that class's usual turnout.\n"
            "- **Flagged check-in**: a self check-in from a device already used by another student in the same class, "
            "or by a student on approved leave. The device signature is coarse, so these are reviewed, not rejected.")

    pending = att.unmarked_sessions(today - timedelta(days=7), today)
    st.subheader(f"Classes without attendance ({len(pending)})")
    if not pending:
        st.success("Every finished class in the last 7 days has been marked.")
    else:
        st.dataframe(pd.DataFrame({
            "Date": [s["date"].strftime("%a %d %b") for s in pending], "Time": [s["start_time"] for s in pending],
            "Class": [f"{s['code']} {s['name']} ({s['section']})" for s in pending],
            "Teacher": [s["teacher_name"] for s in pending],
        }), width="stretch", hide_index=True)
        st.caption("Each teacher has been sent a reminder.")


# ----------------------------------------------------------------------- users
def users(user: dict) -> None:
    st.title("Users")
    flash = st.session_state.pop("users_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    list_tab, add_tab = st.tabs(["All users", "Add user"])
    with list_tab:
        everyone = db.frame(
            """SELECT u.id, u.username, u.full_name, u.role, u.is_active, u.last_login, st.roll_no, st.section
               FROM users u LEFT JOIN students st ON st.user_id = u.id ORDER BY u.role, u.full_name""")
        c1, c2 = st.columns([1, 2])
        role = c1.selectbox("Role", ["All"] + list(auth.ROLES), format_func=lambda r: auth.ROLE_LABELS.get(r, r))
        search = c2.text_input("Search", placeholder="Name, username or roll number")
        view = everyone if role == "All" else everyone[everyone["role"] == role]
        if search:
            text = (view["full_name"] + " " + view["username"] + " " + view["roll_no"].fillna("")).str.lower()
            view = view[text.str.contains(search.strip().lower(), regex=False)]
        st.caption(f"{len(view)} of {len(everyone)} users")
        st.dataframe(pd.DataFrame({
            "Name": view["full_name"], "Username": view["username"],
            "Role": view["role"].map(auth.ROLE_LABELS), "Roll no": view["roll_no"].fillna(""),
            "Active": view["is_active"].astype(bool), "Last sign-in": view["last_login"].fillna("Never"),
        }), width="stretch", hide_index=True, height=360)

        st.subheader("Manage a user")
        if view.empty:
            st.info("No users match.")
        else:
            labels = {int(r.id): f"{r.full_name} ({r.username})" for r in view.itertuples()}
            chosen = st.selectbox("User", list(labels), format_func=labels.get)
            target = everyone[everyone["id"] == chosen].iloc[0]
            protected = auth.is_protected(target["username"])
            if protected:
                st.caption("This is a shared demo account, so it cannot be deactivated or have its password changed.")
            c1, c2 = st.columns(2)
            with c1:
                label = "Deactivate account" if target["is_active"] else "Reactivate account"
                if st.button(label, disabled=protected, width="stretch"):
                    try:
                        auth.set_active(int(chosen), not target["is_active"], user)
                        st.session_state["users_flash"] = f"{target['full_name']}: account " + (
                            "deactivated." if target["is_active"] else "reactivated.")
                        st.rerun()
                    except auth.AuthError as e:
                        st.error(str(e))
            with c2:
                with st.form("reset_password", clear_on_submit=True):
                    new = st.text_input("New password", type="password")
                    if st.form_submit_button("Reset password", disabled=protected, width="stretch"):
                        try:
                            auth.set_password(int(chosen), new, user)
                            st.success("Password reset.")
                        except auth.AuthError as e:
                            st.error(str(e))
    with add_tab:
        role = st.selectbox("Account type", list(auth.ROLES), index=2, format_func=auth.ROLE_LABELS.get, key="new_role")
        with st.form("add_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            full_name = c1.text_input("Full name")
            username = c2.text_input("Username", help="Lowercase letters, digits, dot, dash or underscore")
            email = c1.text_input("Email (optional)")
            password = c2.text_input("Temporary password", type="password",
                                     help="At least 8 characters with upper and lower case, a digit and a special character.")
            profile, class_ids = {}, []
            if role == "student":
                classes = db.frame("SELECT id, code, name, section FROM classes ORDER BY code")
                parents = db.frame("SELECT id, full_name, username FROM users WHERE role = 'parent' ORDER BY full_name")
                profile["roll_no"] = c1.text_input("Roll number")
                profile["section"] = c2.selectbox("Section", sorted(classes["section"].unique()))
                profile["program"] = c1.text_input("Programme", "B.Tech Computer Science")
                profile["semester"] = c2.number_input("Semester", 1, 12, 3)
                parent_labels = {int(r.id): f"{r.full_name} ({r.username})" for r in parents.itertuples()}
                profile["parent_user_id"] = c1.selectbox("Parent account (optional)", [None] + list(parent_labels),
                                                         format_func=lambda i: parent_labels.get(i, "None"))
                class_labels = {int(r.id): f"{r.code} {r.name} ({r.section})" for r in classes.itertuples()}
                class_ids = c2.multiselect("Enrol in classes", list(class_labels), format_func=class_labels.get)
            elif role == "teacher":
                profile["employee_code"] = c1.text_input("Employee code")
                profile["department"] = c2.text_input("Department")
            if st.form_submit_button("Create account", type="primary"):
                try:
                    if role == "student" and profile["roll_no"] and db.one(
                            "SELECT 1 FROM students WHERE roll_no = ?", (profile["roll_no"],)):
                        raise auth.AuthError("That roll number is already in use.")
                    new_id = auth.create_user(username, full_name, role, password, email, actor=user, **profile)
                    if class_ids:
                        student = db.one("SELECT id FROM students WHERE user_id = ?", (new_id,))
                        for class_id in class_ids:
                            db.execute("INSERT OR IGNORE INTO enrollments(class_id, student_id) VALUES(?,?)",
                                       (class_id, student["id"]))
                    st.session_state["users_flash"] = f"Account created for {full_name.strip()}."
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))


# -------------------------------------------------------- classes & timetable
def classes(user: dict) -> None:
    st.title("Classes and timetable")
    flash = st.session_state.pop("classes_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    table_tab, class_tab, change_tab, enrol_tab = st.tabs(["Timetable", "Classes", "Schedule changes", "Enrolment"])
    all_classes = db.frame(
        """SELECT c.id, c.code, c.name, c.section, u.full_name AS teacher,
                  (SELECT COUNT(*) FROM enrollments e WHERE e.class_id = c.id) AS students,
                  (SELECT COUNT(*) FROM schedules s WHERE s.class_id = c.id AND s.active = 1) AS meetings
           FROM classes c LEFT JOIN teachers t ON t.id = c.teacher_id LEFT JOIN users u ON u.id = t.user_id
           ORDER BY c.code""")
    class_labels = {int(r.id): f"{r.code} {r.name} ({r.section})" for r in all_classes.itertuples()}
    with table_tab:
        sections = sorted(all_classes["section"].dropna().unique())
        section = st.segmented_control("Section", sections, default=sections[0] if sections else None)
        grid = common.timetable_grid(section=section) if section else pd.DataFrame()
        if grid.empty:
            st.info("No classes are scheduled for this section.")
        else:
            st.dataframe(grid, width="stretch")
    with class_tab:
        st.dataframe(pd.DataFrame({
            "Code": all_classes["code"], "Class": all_classes["name"], "Section": all_classes["section"],
            "Teacher": all_classes["teacher"].fillna("Unassigned"), "Students": all_classes["students"],
            "Meetings per week": all_classes["meetings"],
        }), width="stretch", hide_index=True)
    with change_tab:
        st.caption("Cancel or move a single upcoming class. Enrolled students and the teacher are notified.")
        today = clock.today()
        class_id = st.selectbox("Class", list(class_labels), format_func=class_labels.get, key="change_class")
        upcoming = [s for s in att.sessions(today, today + timedelta(days=28), class_ids=[class_id])]
        if not upcoming:
            st.info("This class has no meetings in the next four weeks.")
        else:
            keys = [(s["schedule_id"], s["date"]) for s in upcoming]
            lookup = dict(zip(keys, upcoming))
            with st.form("schedule_change", clear_on_submit=True):
                meeting = st.selectbox(
                    "Class meeting", keys,
                    format_func=lambda k: f"{k[1].strftime('%a %d %b')} at {lookup[k]['start_time']} in {lookup[k]['room']}"
                                          + ("" if lookup[k]["state"] == "scheduled" else f"  ({lookup[k]['state']})"))
                kind = st.radio("Change", list(workflow.CHANGE_TYPES), format_func=workflow.CHANGE_TYPES.get,
                                horizontal=True)
                c1, c2, c3 = st.columns(3)
                room = c1.text_input("New room", help="For a room change")
                start = c2.time_input("New start", time(9, 0), help="For a time change")
                end = c3.time_input("New end", time(9, 50), help="For a time change")
                note = st.text_input("Reason shown to students", max_chars=200)
                if st.form_submit_button("Save change and notify", type="primary"):
                    try:
                        sent = workflow.change_schedule(meeting[0], meeting[1], kind, user, new_room=room,
                                                        new_start=start.strftime("%H:%M"),
                                                        new_end=end.strftime("%H:%M"), note=note)
                        st.session_state["classes_flash"] = f"Change saved. {sent} people were notified."
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        changes = db.frame(
            """SELECT sc.date, c.code, s.start_time, sc.change_type, sc.new_room, sc.new_start, sc.new_end, sc.note
               FROM schedule_changes sc JOIN schedules s ON s.id = sc.schedule_id JOIN classes c ON c.id = s.class_id
               WHERE sc.date >= ? ORDER BY sc.date""", (today.isoformat(),))
        if not changes.empty:
            st.subheader("Upcoming changes")
            st.dataframe(pd.DataFrame({
                "Date": changes["date"], "Class": changes["code"], "Usual time": changes["start_time"],
                "Change": changes["change_type"].str.capitalize(),
                "Details": [r.new_room or (f"{r.new_start}-{r.new_end}" if r.new_start else "") for r in changes.itertuples()],
                "Reason": changes["note"].fillna(""),
            }), width="stretch", hide_index=True)
    with enrol_tab:
        class_id = st.selectbox("Class", list(class_labels), format_func=class_labels.get, key="enrol_class")
        roster = att.roster(class_id)
        st.caption(f"{len(roster)} students enrolled")
        st.dataframe(roster.rename(columns={"roll_no": "Roll no", "full_name": "Student"})[["Roll no", "Student"]],
                     width="stretch", hide_index=True, height=260)
        others = db.frame(
            """SELECT st.id, st.roll_no, u.full_name FROM students st JOIN users u ON u.id = st.user_id
               WHERE u.is_active = 1 AND st.id NOT IN (SELECT student_id FROM enrollments WHERE class_id = ?)
               ORDER BY st.roll_no""", (class_id,))
        c1, c2 = st.columns(2)
        with c1.form("enrol_add", clear_on_submit=True):
            labels = {int(r.id): f"{r.roll_no} {r.full_name}" for r in others.itertuples()}
            add = st.multiselect("Add students", list(labels), format_func=labels.get)
            if st.form_submit_button("Enrol") and add:
                for student_id in add:
                    db.execute("INSERT OR IGNORE INTO enrollments(class_id, student_id) VALUES(?,?)", (class_id, student_id))
                db.audit("enrolment_added", f"{len(add)} student(s) to {class_labels[class_id]}", user)
                st.session_state["classes_flash"] = f"{len(add)} student(s) enrolled."
                st.rerun()
        with c2.form("enrol_remove", clear_on_submit=True):
            labels = {int(r.student_id): f"{r.roll_no} {r.full_name}" for r in roster.itertuples()}
            drop = st.multiselect("Remove students", list(labels), format_func=labels.get,
                                  help="Past attendance records are kept.")
            if st.form_submit_button("Remove") and drop:
                for student_id in drop:
                    db.execute("DELETE FROM enrollments WHERE class_id = ? AND student_id = ?", (class_id, student_id))
                db.audit("enrolment_removed", f"{len(drop)} student(s) from {class_labels[class_id]}", user)
                st.session_state["classes_flash"] = f"{len(drop)} student(s) removed."
                st.rerun()


def requests(user: dict) -> None:
    st.title("Requests")
    st.caption("Flagged records and leave requests across all classes.")
    flash = st.session_state.pop("requests_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    teacher.requests_panel(user)


def reports(user: dict) -> None:
    common.report_page(user)


# -------------------------------------------------------------------- settings
def settings(user: dict) -> None:
    st.title("Settings")
    flash = st.session_state.pop("settings_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    with st.form("settings"):
        st.subheader("Attendance thresholds")
        c1, c2, c3 = st.columns(3)
        low = c1.number_input("Low attendance alert below (%)", 1, 100, db.get_setting("threshold_low", int),
                              help="Students and parents are alerted.")
        critical = c2.number_input("Escalate to staff below (%)", 0, 99, db.get_setting("threshold_critical", int),
                                   help="The class teacher and administrators are alerted.")
        min_sessions = c3.number_input("Minimum classes before alerting", 1, 30,
                                       db.get_setting("min_sessions_for_alert", int))
        st.subheader("Check-in and sessions")
        c1, c2, c3 = st.columns(3)
        window = c1.number_input("Check-in code valid for (minutes)", 1, 60, db.get_setting("checkin_window_min", int))
        grace = c2.number_input("Count as Late after (minutes)", 0, 60, db.get_setting("late_grace_min", int))
        timeout = c3.number_input("Sign out after inactivity (minutes)", 5, 240, db.get_setting("session_timeout_min", int))
        if st.form_submit_button("Save settings", type="primary"):
            if critical >= low:
                st.error("The escalation threshold must be lower than the low attendance threshold.")
            else:
                for key, value in (("threshold_low", low), ("threshold_critical", critical),
                                   ("min_sessions_for_alert", min_sessions), ("checkin_window_min", window),
                                   ("late_grace_min", grace), ("session_timeout_min", timeout)):
                    db.set_setting(key, int(value))
                db.audit("settings_changed", f"low={low} critical={critical} timeout={timeout}", user)
                st.session_state["settings_flash"] = "Settings saved."
                st.rerun()

    st.subheader("Maintenance")
    c1, c2, c3 = st.columns(3)
    if c1.button("Run alert checks now", icon=":material/notifications_active:", width="stretch",
                 help="Reminders, threshold alerts and pattern alerts. Safe to run repeatedly."):
        result = notifications.run_housekeeping()
        st.session_state["settings_flash"] = (
            f"Checks complete: {result['reminders']} reminders, {result['low_attendance']} threshold alerts, "
            f"{result['patterns']} pattern alerts sent.")
        st.rerun()
    c2.download_button("Download database backup", db.get_path().read_bytes(),
                       f"sams_backup_{clock.today().isoformat()}.db", "application/octet-stream",
                       icon=":material/database:", width="stretch",
                       on_click=lambda: db.audit("backup_downloaded", "", user))
    if seed.DEMO_MODE:
        with c3.popover("Reset demo data", icon=":material/restart_alt:", width="stretch"):
            st.write("Delete everything and rebuild the demo data relative to today. This cannot be undone.")
            if st.button("Reset now", type="primary"):
                seed.reset_demo()
                st.session_state.clear()
                st.rerun()
        st.caption("Demo deployment: the data is fictional and rebuilds itself each day.")


def audit(user: dict) -> None:
    st.title("Audit log")
    st.caption("Sign-ins, attendance changes and administrative actions, newest first.")
    c1, c2 = st.columns([1, 2])
    actions = [r["action"] for r in db.query("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    action = c1.selectbox("Action", ["All"] + actions)
    search = c2.text_input("Search user or detail")
    sql, params = "SELECT at, username, action, detail FROM audit_log WHERE 1 = 1", []
    if action != "All":
        sql += " AND action = ?"
        params.append(action)
    if search:
        sql += " AND (username LIKE ? OR detail LIKE ?)"
        params += [f"%{search.strip()}%"] * 2
    log = db.frame(sql + " ORDER BY id DESC LIMIT 500", params)
    st.dataframe(log.rename(columns={"at": "When", "username": "User", "action": "Action", "detail": "Detail"}),
                 width="stretch", hide_index=True, height=520)
    st.download_button("Export as CSV", log.to_csv(index=False).encode(), "sams_audit_log.csv", "text/csv",
                       icon=":material/download:")
