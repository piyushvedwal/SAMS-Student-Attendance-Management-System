"""Reporting aggregates and unusual-pattern detection (SRS 3.1.4, 3.1.5, 4.2)."""
from datetime import date, timedelta

import pandas as pd

from . import clock, db
from .attendance import percentage

STATUS_COLUMNS = ["present", "late", "absent", "excused"]


def records(start: date | None = None, end: date | None = None, class_ids=None, student_id=None,
            teacher_id=None) -> pd.DataFrame:
    """One row per attendance record with class and student details."""
    sql = """
        SELECT a.id AS attendance_id, a.date, s.start_time, c.id AS class_id, c.code, c.name AS class_name,
               c.section, st.id AS student_id, st.roll_no, u.full_name AS student, a.status, a.note, a.method
        FROM attendance a
        JOIN schedules s ON s.id = a.schedule_id
        JOIN classes c ON c.id = a.class_id
        JOIN students st ON st.id = a.student_id
        JOIN users u ON u.id = st.user_id
        WHERE 1 = 1"""
    params: list = []
    if start:
        sql += " AND a.date >= ?"
        params.append(start.isoformat())
    if end:
        sql += " AND a.date <= ?"
        params.append(end.isoformat())
    if class_ids is not None:
        ids = list(class_ids)
        if not ids:
            ids = [-1]
        sql += f" AND a.class_id IN ({','.join('?' * len(ids))})"
        params += ids
    if student_id is not None:
        sql += " AND a.student_id = ?"
        params.append(student_id)
    if teacher_id is not None:
        sql += " AND c.teacher_id = ?"
        params.append(teacher_id)
    df = db.frame(sql + " ORDER BY a.date, s.start_time, st.roll_no", params)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _counts(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Status counts and percentage per group."""
    if df.empty:
        return pd.DataFrame(columns=by + STATUS_COLUMNS + ["counted", "pct"])
    wide = (
        df.assign(n=1)
        .pivot_table(index=by, columns="status", values="n", aggfunc="sum", fill_value=0)
        .rename(columns=str.lower)
        .reindex(columns=STATUS_COLUMNS, fill_value=0)
        .reset_index()
    )
    wide.columns.name = None
    wide["counted"] = wide["present"] + wide["late"] + wide["absent"]
    wide["pct"] = [percentage(r.present, r.late, r.absent) for r in wide.itertuples()]
    return wide


def by_student(df: pd.DataFrame) -> pd.DataFrame:
    return _counts(df, ["student_id", "roll_no", "student"]).sort_values("roll_no", ignore_index=True)


def by_class(df: pd.DataFrame) -> pd.DataFrame:
    return _counts(df, ["class_id", "code", "class_name", "section"]).sort_values("code", ignore_index=True)


def by_student_class(df: pd.DataFrame) -> pd.DataFrame:
    return _counts(df, ["student_id", "roll_no", "student", "class_id", "code"])


def daily(df: pd.DataFrame) -> pd.DataFrame:
    out = _counts(df, ["date"])
    return out.sort_values("date", ignore_index=True)


def weekly_mix(df: pd.DataFrame) -> pd.DataFrame:
    """Long table: week start, status, count (for the status-mix chart)."""
    if df.empty:
        return pd.DataFrame(columns=["week", "status", "count"])
    week = df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")
    return (
        df.assign(week=week).groupby(["week", "status"]).size().reset_index(name="count")
        .sort_values(["week", "status"], ignore_index=True)
    )


def at_risk(df: pd.DataFrame, threshold: float, min_sessions: int = 4) -> pd.DataFrame:
    """Students whose attendance in any class is below the threshold."""
    per = by_student_class(df)
    per = per[(per["counted"] >= min_sessions) & (per["pct"] < threshold)]
    return per.sort_values("pct", ignore_index=True)


# ------------------------------------------------------------ unusual patterns
def anomalies(today: date | None = None, recent_days: int = 14) -> list[dict]:
    """Statistical checks that surface patterns an administrator should look at.

    1. Sudden drop: a student's attendance over the last two weeks is at least
       30 points below their own earlier record.
    2. Absence streak: a student's 4 or more most recent classes are all absent.
    3. Class outlier: one meeting of a class has attendance more than 2.5
       standard deviations (and 25 points) below that class's usual level.
    4. Flagged check-ins: self check-ins that looked like proxy attendance.

    Each finding carries a stable key so it is only reported once.
    """
    today = today or clock.today()
    cutoff = pd.Timestamp(today - timedelta(days=recent_days))
    df = records(end=today)
    found: list[dict] = []
    if df.empty:
        return found
    counted = df[df["status"] != "Excused"].assign(attended=lambda d: d["status"].isin(["Present", "Late"]))

    # 1. sudden drop
    recent = counted[counted["date"] > cutoff].groupby("student_id")["attended"].agg(["mean", "size"])
    earlier = counted[counted["date"] <= cutoff].groupby("student_id")["attended"].agg(["mean", "size"])
    both = recent.join(earlier, lsuffix="_recent", rsuffix="_earlier", how="inner")
    both = both[(both["size_recent"] >= 5) & (both["size_earlier"] >= 8)]
    both["drop"] = (both["mean_earlier"] - both["mean_recent"]) * 100
    names = df.drop_duplicates("student_id").set_index("student_id")[["roll_no", "student"]]
    for student_id, r in both[both["drop"] >= 30].iterrows():
        who = names.loc[student_id]
        found.append({
            "kind": "Sudden drop", "severity": "high" if r["drop"] >= 45 else "medium",
            "title": f"Sudden drop: {who['student']} ({who['roll_no']})",
            "detail": f"Attendance fell from {r['mean_earlier'] * 100:.0f}% to {r['mean_recent'] * 100:.0f}% "
                      f"over the last {recent_days} days ({int(r['size_recent'])} classes).",
            "key": f"drop:{student_id}:{today.isocalendar()[0]}-W{today.isocalendar()[1]}",
        })

    # 2. absence streak
    ordered = counted.sort_values(["date", "start_time"], ascending=False)
    for student_id, group in ordered.groupby("student_id", sort=False):
        streak = 0
        for attended in group["attended"]:
            if attended:
                break
            streak += 1
        if streak >= 4:
            who = names.loc[student_id]
            last_seen = group[group["attended"]]["date"].max()
            since = f"Last attended on {last_seen.strftime('%d %b')}." if pd.notna(last_seen) else "No attended class on record."
            found.append({
                "kind": "Absence streak", "severity": "high" if streak >= 8 else "medium",
                "title": f"Absence streak: {who['student']} ({who['roll_no']})",
                "detail": f"Absent for the last {streak} classes in a row. {since}",
                "key": f"streak:{student_id}:{group['date'].max().date().isoformat()}",
            })

    # 3. class outlier
    per_meeting = counted.groupby(["class_id", "code", "date"])["attended"].mean().reset_index(name="rate")
    for (class_id, code), group in per_meeting.groupby(["class_id", "code"]):
        if len(group) < 6:
            continue
        for row in group[group["date"] > cutoff].itertuples():
            others = group[group["date"] != row.date]["rate"]
            mean, std = others.mean(), others.std(ddof=0)
            if std > 0 and (row.rate - mean) / std < -2.5 and mean - row.rate >= 0.25:
                found.append({
                    "kind": "Class outlier", "severity": "medium",
                    "title": f"Unusually low turnout: {code} on {row.date.strftime('%a %d %b')}",
                    "detail": f"Only {row.rate * 100:.0f}% attended, against a usual {mean * 100:.0f}% for this class.",
                    "key": f"class:{class_id}:{row.date.date().isoformat()}",
                })

    # 4. flagged check-ins
    flagged = db.query(
        """SELECT ci.id, ci.reason, w.date, c.code, st.roll_no, u.full_name
           FROM checkins ci
           JOIN checkin_windows w ON w.id = ci.window_id
           JOIN schedules s ON s.id = w.schedule_id
           JOIN classes c ON c.id = s.class_id
           JOIN students st ON st.id = ci.student_id
           JOIN users u ON u.id = st.user_id
           WHERE ci.flagged = 1 AND w.date >= ?""",
        ((today - timedelta(days=recent_days)).isoformat(),),
    )
    for r in flagged:
        found.append({
            "kind": "Flagged check-in", "severity": "high",
            "title": f"Possible proxy check-in: {r['full_name']} ({r['roll_no']}) in {r['code']}",
            "detail": f"{r['reason']} on {r['date']}.",
            "key": f"checkin:{r['id']}",
        })
    order = {"high": 0, "medium": 1}
    return sorted(found, key=lambda a: (order[a["severity"]], a["kind"], a["title"]))
