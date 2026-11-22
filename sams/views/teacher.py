"""Teacher screens: dashboard, marking, check-in, requests (SRS 4.1, use case 8.6.1)."""
from datetime import timedelta

import pandas as pd
import streamlit as st

from .. import analytics, clock, db, ui, workflow
from .. import attendance as att
from . import common


def _go(page: str, **state) -> None:
    st.session_state["nav_target"] = page
    st.session_state.update(state)
    st.rerun()


def dashboard(user: dict) -> None:
    teacher_id = user["teacher_id"]
    today, now = clock.today(), clock.now()
    term_start, _ = att.term_bounds()
    st.title(f"Welcome, {user['full_name']}")
    classes = db.frame(
        """SELECT c.id, c.code, c.name, c.section, COUNT(e.student_id) AS students
           FROM classes c LEFT JOIN enrollments e ON e.class_id = c.id
           WHERE c.teacher_id = ? GROUP BY c.id ORDER BY c.code""", (teacher_id,))
    records = analytics.records(term_start, today, teacher_id=teacher_id)
    by_class = analytics.by_class(records)
    pending = att.unmarked_sessions(today - timedelta(days=14), today, teacher_id=teacher_id)
    open_flags = workflow.flags(teacher_id=teacher_id, status="open")
    pending_leave = workflow.leaves(teacher_id=teacher_id, status="pending")
    counted = records[records["status"] != "Excused"]
    overall = round(100 * counted["status"].isin(["Present", "Late"]).mean(), 1) if len(counted) else None

    m = st.columns(4)
    m[0].metric("My classes", len(classes), help=f"{int(classes['students'].sum())} enrolments", border=True)
    m[1].metric("Average attendance", ui.pct_text(overall), border=True)
    m[2].metric("Attendance pending", len(pending), border=True)
    m[3].metric("Requests to review", len(open_flags) + len(pending_leave), border=True)

    if pending:
        st.subheader("Attendance pending")
        for s in pending[-5:][::-1]:
            row = st.columns([5, 1], vertical_alignment="center")
            row[0].warning(f"**{s['code']} {s['name']}** ({s['section']}) on {s['date'].strftime('%a %d %b')} "
                           f"at {s['start_time']} has no attendance yet.", icon=":material/pending_actions:")
            if row[1].button("Mark now", key=f"mark_{s['schedule_id']}_{s['date']}", width="stretch"):
                _go("Mark attendance", mark_target=(s["schedule_id"], s["date"]))

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Attendance by class")
        threshold = db.get_setting("threshold_low", float)
        ui.percent_bars(by_class.assign(label=by_class["code"] + " " + by_class["class_name"]), "label", threshold)
    with right:
        st.subheader("Today" if any(s["date"] == today for s in att.sessions(today, today, teacher_id=teacher_id))
                     else "Next classes")
        upcoming = [s for s in att.sessions(today, today + timedelta(days=6), teacher_id=teacher_id)
                    if att.session_end(s) >= now][:5]
        if not upcoming:
            st.info("No classes in the next 7 days.")
        else:
            st.dataframe(common.upcoming_table(upcoming)[["Date", "Time", "Class", "Room", "Status"]],
                         width="stretch", hide_index=True)


# ------------------------------------------------------------------- marking
def _roster_frame(session: dict, bulk: str | None) -> tuple[pd.DataFrame, bool]:
    """Roster with the status to show for each student; True if already saved."""
    day = session["date"]
    roster = att.roster(session["class_id"])
    saved = att.existing(session["schedule_id"], day)
    on_leave = att.leave_map(day, roster["student_id"].tolist())
    checkins = {r.roll_no: r for r in att.checkin_list(session["schedule_id"], day).itertuples()}
    statuses, notes, info = [], [], []
    for r in roster.itertuples():
        record = saved.get(r.student_id)
        remark = []
        if r.student_id in on_leave:
            remark.append(f"On approved leave: {on_leave[r.student_id]}")
        if r.roll_no in checkins:
            c = checkins[r.roll_no]
            remark.append(f"Flagged check-in: {c.reason}" if c.flagged else f"Checked in at {c.at[11:16]}")
        if bulk and not (record and record["method"] == "check-in"):
            status = "Excused" if r.student_id in on_leave else bulk
        elif record:
            status = record["status"]
        else:
            status = "Excused" if r.student_id in on_leave else "Present"
        statuses.append(status)
        notes.append((record or {}).get("note") or "")
        info.append("; ".join(remark))
    frame = pd.DataFrame({"student_id": roster["student_id"], "Roll no": roster["roll_no"],
                          "Student": roster["full_name"], "Status": statuses, "Note": notes, "Info": info})
    return frame, bool(saved)


def _checkin_panel(user: dict, session: dict) -> None:
    schedule_id, day = session["schedule_id"], session["date"]
    window = att.active_window(schedule_id, day)
    with st.expander("Student check-in", expanded=window is not None, icon=":material/qr_code_2:"):
        if day != clock.today():
            st.caption("Check-in can only be opened for today's classes.")
        elif window is None:
            minutes = db.get_setting("checkin_window_min", int)
            st.write(f"Let students mark themselves present with a code that is valid for {minutes} minutes.")
            if st.button("Open check-in", icon=":material/lock_open:"):
                att.open_checkin(schedule_id, day, user)
                st.rerun()
        else:
            c1, c2 = st.columns([2, 1], vertical_alignment="center")
            c1.markdown(f"## `{window['code']}`")
            c1.caption(f"Show this code in class. Valid until {window['expires_at'][11:16]}.")
            if c2.button("Close check-in", icon=":material/lock:", width="stretch"):
                att.close_checkin(window["id"], user)
                st.rerun()
            c2.button("Refresh list", icon=":material/refresh:", width="stretch")
        done = att.checkin_list(schedule_id, day)
        if not done.empty:
            flagged = int(done["flagged"].sum())
            st.caption(f"{len(done)} checked in" + (f", {flagged} flagged for review" if flagged else ""))
            st.dataframe(pd.DataFrame({
                "Roll no": done["roll_no"], "Student": done["full_name"], "Time": done["at"].str[11:19],
                "Review": ["Flagged: " + (r or "") if f else "OK" for f, r in zip(done["flagged"], done["reason"])],
            }), width="stretch", hide_index=True)


def mark_attendance(user: dict) -> None:
    st.title("Mark attendance")
    teacher_id = user["teacher_id"]
    today, now = clock.today(), clock.now()
    term_start, _ = att.term_bounds()
    held = [s for s in att.sessions(term_start, today, teacher_id=teacher_id)
            if s["state"] != "cancelled" and att.session_start(s) <= now]
    if not held:
        st.info("None of your classes has started yet this term.")
        return
    marked = att.marked_keys(term_start, today)

    def is_marked(s):
        return (s["schedule_id"], s["date"].isoformat()) in marked

    target = st.session_state.pop("mark_target", None)
    pending = [s for s in held if not is_marked(s)]
    default = next((s for s in held if target and (s["schedule_id"], s["date"]) == tuple(target)), None) \
        or (pending[-1] if pending else held[-1])

    class_labels = {}
    for s in held:
        class_labels.setdefault(s["class_id"], f"{s['code']} {s['name']} ({s['section']})")
    class_ids = list(class_labels)
    if target or st.session_state.get("mark_class") not in class_ids:
        st.session_state["mark_class"] = default["class_id"]
    c1, c2 = st.columns(2)
    class_id = c1.selectbox("Class", class_ids, format_func=class_labels.get, key="mark_class")
    meetings = [s for s in held if s["class_id"] == class_id][::-1]
    keys = [(s["schedule_id"], s["date"]) for s in meetings]
    if target or st.session_state.get("mark_meeting") not in keys:
        wanted = (default["schedule_id"], default["date"])
        first_pending = next((k for k, s in zip(keys, meetings) if not is_marked(s)), keys[0])
        st.session_state["mark_meeting"] = wanted if wanted in keys else first_pending
    lookup = dict(zip(keys, meetings))
    # Labels stay fixed: the saved/pending state is shown below, where it can change.
    meeting = c2.selectbox(
        "Class meeting", keys, key="mark_meeting",
        format_func=lambda k: f"{k[1].strftime('%a %d %b %Y')} at {lookup[k]['start_time']}")
    session = lookup[meeting]
    waiting = [s for s in pending if (s["schedule_id"], s["date"]) != meeting]
    if waiting:
        st.caption("Also pending: " + ", ".join(
            f"{s['code']} on {s['date'].strftime('%a %d %b')} at {s['start_time']}" for s in waiting[-4:]))
    state_key = f"{session['schedule_id']}_{session['date'].isoformat()}"

    flash = st.session_state.pop("mark_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")

    _checkin_panel(user, session)

    bulk = st.session_state.get(f"bulk_{state_key}")
    version = st.session_state.get(f"version_{state_key}", 0)
    frame, already = _roster_frame(session, bulk)
    if already:
        st.info(f"Attendance for this class is already saved (room {session['room']}). "
                "Change any row and save again to correct it.", icon=":material/task_alt:")
    else:
        st.warning(f"Not marked yet (room {session['room']}). Everyone starts as Present; "
                   "change the exceptions and save.", icon=":material/pending_actions:")

    b = st.columns([1, 1, 1, 3])
    if b[0].button("All present", icon=":material/done_all:", width="stretch"):
        st.session_state[f"bulk_{state_key}"] = "Present"
        st.session_state[f"version_{state_key}"] = version + 1
        st.rerun()
    if b[1].button("All absent", icon=":material/remove_done:", width="stretch"):
        st.session_state[f"bulk_{state_key}"] = "Absent"
        st.session_state[f"version_{state_key}"] = version + 1
        st.rerun()
    if b[2].button("Reset", icon=":material/undo:", width="stretch", help="Discard unsaved changes"):
        st.session_state.pop(f"bulk_{state_key}", None)
        st.session_state[f"version_{state_key}"] = version + 1
        st.rerun()

    edited = st.data_editor(
        frame, key=f"roster_{state_key}_{version}", hide_index=True, width="stretch",
        height=min(38 * len(frame) + 40, 620),
        column_order=["Roll no", "Student", "Status", "Note", "Info"],
        column_config={
            "Roll no": st.column_config.TextColumn(disabled=True, width="small"),
            "Student": st.column_config.TextColumn(disabled=True),
            "Status": st.column_config.SelectboxColumn(options=list(att.STATUSES), required=True, width="small"),
            "Note": st.column_config.TextColumn(max_chars=120, help="Optional comment, e.g. reason for absence"),
            "Info": st.column_config.TextColumn(disabled=True, help="Leave and check-in information"),
        })
    counts = edited["Status"].value_counts()
    st.markdown(" ".join(f"{ui.chip(s)} **{int(counts.get(s, 0))}**&nbsp;&nbsp;" for s in ui.STATUS_ORDER),
                unsafe_allow_html=True)

    if st.button("Save attendance", type="primary", icon=":material/save:"):
        records = [{"student_id": int(r.student_id), "status": r.Status, "note": r.Note} for r in edited.itertuples()]
        try:
            result = att.save_attendance(session["schedule_id"], session["date"], records, user,
                                         method="bulk" if bulk else "manual")
        except Exception as e:  # keep what the teacher entered so they can retry
            st.error(f"Could not save attendance: {e}. Your entries are still on screen; please try again.")
        else:
            st.session_state.pop(f"bulk_{state_key}", None)
            st.session_state[f"version_{state_key}"] = version + 1
            st.session_state["mark_flash"] = (
                f"Attendance saved for {session['code']} on {session['date'].strftime('%a %d %b')}: "
                f"{result['submitted']} students, {result['changed']} record(s) written.")
            st.rerun()


# ------------------------------------------------------------------- requests
def requests_panel(user: dict, teacher_id=None) -> None:
    """Review discrepancy flags and leave requests (teacher: own classes; admin: all)."""
    flag_tab, leave_tab = st.tabs(["Flagged records", "Leave requests"])
    with flag_tab:
        open_flags = workflow.flags(teacher_id=teacher_id, status="open")
        if open_flags.empty:
            st.info("No flagged records are waiting for review.")
        for f in open_flags.itertuples():
            with st.container(border=True):
                st.markdown(f"**{f.student}** ({f.roll_no}) · {f.code} · "
                            f"{pd.Timestamp(f.date).strftime('%a %d %b')} at {f.start_time}")
                st.markdown(f"Recorded as {ui.chip(f.status)}", unsafe_allow_html=True)
                st.write(f"“{f.message}”")
                with st.form(f"flag_{f.flag_id}"):
                    c1, c2 = st.columns(2)
                    decision = c1.radio("Decision", ["Correct the record", "Keep as recorded"], horizontal=True)
                    options = [s for s in att.STATUSES if s != f.status]
                    new_status = c2.selectbox("Corrected status", options)
                    response = st.text_input("Note to the student (optional)", max_chars=300)
                    if st.form_submit_button("Submit decision", type="primary"):
                        try:
                            accept = decision == "Correct the record"
                            workflow.resolve_flag(int(f.flag_id), accept, new_status if accept else None, response, user)
                            st.session_state["requests_flash"] = "Flag resolved and the student was notified."
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
        done = workflow.flags(teacher_id=teacher_id)
        done = done[done["flag_status"] != "open"]
        if not done.empty:
            with st.expander(f"Resolved flags ({len(done)})"):
                st.dataframe(pd.DataFrame({
                    "Date": done["date"], "Class": done["code"], "Student": done["student"],
                    "Message": done["message"], "Outcome": done["flag_status"].str.capitalize(),
                    "Response": done["response"].fillna(""),
                }), width="stretch", hide_index=True)
    with leave_tab:
        pending = workflow.leaves(teacher_id=teacher_id, status="pending")
        if pending.empty:
            st.info("No leave requests are waiting for a decision.")
        for l in pending.itertuples():
            with st.container(border=True):
                c1, c2, c3 = st.columns([5, 1, 1], vertical_alignment="center")
                c1.markdown(f"**{l.student}** ({l.roll_no}, {l.section}) · {l.start_date} to {l.end_date}")
                c1.caption(l.reason)
                if c2.button("Approve", key=f"approve_{l.leave_id}", type="primary", width="stretch"):
                    excused = workflow.decide_leave(int(l.leave_id), True, user)
                    st.session_state["requests_flash"] = (
                        f"Leave approved. {excused} recorded absence(s) in that period were changed to Excused.")
                    st.rerun()
                if c3.button("Reject", key=f"reject_{l.leave_id}", width="stretch"):
                    workflow.decide_leave(int(l.leave_id), False, user)
                    st.session_state["requests_flash"] = "Leave request rejected and the student was notified."
                    st.rerun()
        decided = workflow.leaves(teacher_id=teacher_id)
        decided = decided[decided["status"] != "pending"]
        if not decided.empty:
            with st.expander(f"Decided requests ({len(decided)})"):
                st.dataframe(pd.DataFrame({
                    "Student": decided["student"], "From": decided["start_date"], "To": decided["end_date"],
                    "Reason": decided["reason"], "Decision": decided["status"].str.capitalize(),
                    "Decided by": decided["decided_by"].fillna(""),
                }), width="stretch", hide_index=True)


def requests(user: dict) -> None:
    st.title("Requests")
    st.caption("Records students have flagged as wrong, and leave requests from students in your classes.")
    flash = st.session_state.pop("requests_flash", None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    requests_panel(user, teacher_id=user["teacher_id"])


def reports(user: dict) -> None:
    common.report_page(user, teacher_id=user["teacher_id"])


def schedule(user: dict) -> None:
    common.schedule_page(user, teacher_id=user["teacher_id"])
