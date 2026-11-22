"""Attendance reports and exports to CSV, Excel and PDF (SRS 3.1.4, 4.2)."""
import io
from datetime import date

import pandas as pd

from . import analytics, clock, db

SUMMARY_COLUMNS = {
    "roll_no": "Roll no", "student": "Student", "present": "Present", "late": "Late",
    "absent": "Absent", "excused": "Excused", "counted": "Classes counted", "pct": "Attendance %",
}


def build(start: date, end: date, class_id: int | None = None, student_id: int | None = None,
          teacher_id: int | None = None) -> dict:
    """Collect everything one report needs for a date range and scope."""
    df = analytics.records(start, end, class_ids=[class_id] if class_id else None,
                           student_id=student_id, teacher_id=teacher_id)
    threshold = db.get_setting("threshold_low", float)
    scope = "All classes"
    if class_id:
        c = db.one("SELECT code, name, section FROM classes WHERE id = ?", (class_id,))
        scope = f"{c['code']} {c['name']} ({c['section']})"
    if student_id:
        s = db.one("SELECT st.roll_no, u.full_name FROM students st JOIN users u ON u.id = st.user_id WHERE st.id = ?",
                   (student_id,))
        scope = f"{s['full_name']} ({s['roll_no']})" + (f" in {scope}" if class_id else "")
    summary = analytics.by_class(df) if student_id else analytics.by_student(df)
    totals = {s: int((df["status"] == s.capitalize()).sum()) for s in analytics.STATUS_COLUMNS}
    counted = totals["present"] + totals["late"] + totals["absent"]
    return {
        "title": "Attendance report",
        "scope": scope,
        "start": start, "end": end,
        "generated": clock.stamp(),
        "threshold": threshold,
        "by": "class" if student_id else "student",
        "records": df,
        "summary": summary,
        "daily": analytics.daily(df),
        "at_risk": analytics.at_risk(df, threshold),
        "totals": dict(totals, counted=counted,
                       pct=round(100 * (totals["present"] + totals["late"]) / counted, 1) if counted else None),
    }


def summary_table(report: dict) -> pd.DataFrame:
    """The summary as a display/export table with readable headers."""
    s = report["summary"]
    if report["by"] == "class":
        out = s.rename(columns={"code": "Code", "class_name": "Class", **SUMMARY_COLUMNS})
        cols = ["Code", "Class"]
    else:
        out = s.rename(columns=SUMMARY_COLUMNS)
        cols = ["Roll no", "Student"]
    return out[cols + ["Present", "Late", "Absent", "Excused", "Classes counted", "Attendance %"]]


def records_table(report: dict) -> pd.DataFrame:
    r = report["records"]
    out = pd.DataFrame({
        "Date": r["date"].dt.strftime("%Y-%m-%d"), "Time": r["start_time"], "Class": r["code"],
        "Roll no": r["roll_no"], "Student": r["student"], "Status": r["status"],
        "Note": r["note"].fillna(""), "Method": r["method"],
    })
    return out


def daily_table(report: dict) -> pd.DataFrame:
    d = report["daily"]
    return pd.DataFrame({
        "Date": pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d"), "Present": d["present"], "Late": d["late"],
        "Absent": d["absent"], "Excused": d["excused"], "Attendance %": d["pct"],
    })


def _header_lines(report: dict) -> list[str]:
    t = report["totals"]
    pct = f"{t['pct']:.1f}%" if t["pct"] is not None else "n/a"
    return [
        f"Scope: {report['scope']}",
        f"Period: {report['start'].isoformat()} to {report['end'].isoformat()}",
        f"Overall attendance: {pct}  (Present {t['present']}, Late {t['late']}, Absent {t['absent']}, Excused {t['excused']})",
        f"Generated: {report['generated']} IST",
    ]


def to_csv(report: dict) -> bytes:
    lines = [f"# {report['title']}"] + [f"# {line}" for line in _header_lines(report)]
    return ("\n".join(lines) + "\n" + summary_table(report).to_csv(index=False)).encode("utf-8")


def to_excel(report: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        info = pd.DataFrame({report["title"]: _header_lines(report)})
        info.to_excel(writer, sheet_name="Summary", index=False)
        summary_table(report).to_excel(writer, sheet_name="Summary", index=False, startrow=len(info) + 2)
        daily_table(report).to_excel(writer, sheet_name="By day", index=False)
        records_table(report).to_excel(writer, sheet_name="Records", index=False)
        for sheet in writer.sheets.values():
            for column in sheet.columns:
                width = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column)
                sheet.column_dimensions[column[0].column_letter].width = min(max(10, width + 2), 60)
    return buffer.getvalue()


def _latin(text) -> str:
    """The built-in PDF fonts are Latin-1 only; replace anything else."""
    return str(text).replace("–", "-").replace("—", "-").encode("latin-1", "replace").decode("latin-1")


def to_pdf(report: dict) -> bytes:
    from fpdf import FPDF

    table = summary_table(report)
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin(f"SAMS - {report['title']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in _header_lines(report):
        pdf.cell(0, 6, _latin(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    widths = [28, 78] + [24] * 4 + [34, 30]
    aligns = ["L", "L"] + ["R"] * 6

    def row(values, bold=False, fill=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        for value, width, align in zip(values, widths, aligns):
            pdf.cell(width, 7, _latin(value)[:48], border=1, align=align, fill=fill)
        pdf.ln()

    pdf.set_fill_color(232, 236, 241)
    row(table.columns, bold=True, fill=True)
    threshold = report["threshold"]
    for values in table.itertuples(index=False):
        pct = values[-1]
        shown = list(values[:-1]) + [f"{pct:.1f}" + (" *" if pct < threshold else "") if pd.notna(pct) else "n/a"]
        if pdf.get_y() > 185:
            pdf.add_page()
            row(table.columns, bold=True, fill=True)
        row(shown)
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, _latin(f"* below the {threshold:.0f}% attendance threshold. Excused classes are not counted. "
                          "Late counts as attended."), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
