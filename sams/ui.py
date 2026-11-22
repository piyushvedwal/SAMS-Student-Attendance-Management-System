"""Shared look and feel: status colours, chips, calendar and charts."""
import calendar
import html
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

# Stacking order and colours were checked for colour-blind separation on both the
# light and the dark surface. Status is never shown by colour alone: every chip
# and chart also carries the status name.
STATUS_ORDER = ["Present", "Late", "Excused", "Absent"]
PALETTE = {
    "light": {"Present": "#1baf7a", "Late": "#eda100", "Excused": "#2a78d6", "Absent": "#e34948",
              "series": "#2a78d6", "ink": "#0b0b0b", "muted": "#52514e", "surface": "#ffffff", "grid": "#e7e6e1"},
    "dark": {"Present": "#199e70", "Late": "#c98500", "Excused": "#3987e5", "Absent": "#e66767",
             "series": "#3987e5", "ink": "#f0efec", "muted": "#c3c2b7", "surface": "#0e1117", "grid": "#33343a"},
}
STATUS_LETTER = {"Present": "P", "Late": "L", "Excused": "E", "Absent": "A"}


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def colors() -> dict:
    return PALETTE[mode()]


def inject_css() -> None:
    c = colors()
    st.html(f"""
    <style>
      .sams-chip {{display:inline-flex;align-items:center;gap:6px;padding:2px 10px 2px 8px;border-radius:999px;
        border:1px solid {c['grid']};font-size:0.82rem;line-height:1.5;white-space:nowrap}}
      .sams-dot {{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}}
      .sams-cal {{width:100%;border-collapse:separate;border-spacing:4px;table-layout:fixed}}
      .sams-cal th {{font-size:0.75rem;font-weight:600;color:{c['muted']};text-align:left;padding:2px 6px;
        text-transform:uppercase;letter-spacing:.04em}}
      .sams-cal td {{vertical-align:top;height:86px;border:1px solid {c['grid']};border-radius:8px;padding:5px 6px}}
      .sams-cal td.empty {{border:none}}
      .sams-cal td.today {{border:2px solid {c['series']}}}
      .sams-cal .num {{font-size:0.78rem;color:{c['muted']};margin-bottom:3px}}
      .sams-cal .rec {{display:flex;align-items:center;gap:5px;font-size:0.74rem;line-height:1.45;white-space:nowrap;
        overflow:hidden}}
      .sams-legend {{display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 2px;font-size:0.82rem;color:{c['muted']}}}
      .sams-legend span {{display:inline-flex;align-items:center;gap:6px}}
      .sams-note {{color:{c['muted']};font-size:0.9rem}}
      @media (max-width: 700px) {{ .sams-cal td {{height:64px;padding:3px}} .sams-cal .rec .code {{display:none}} }}
    </style>""")


def chip(status: str) -> str:
    c = colors()
    return (f'<span class="sams-chip"><span class="sams-dot" style="background:{c.get(status, c["muted"])}"></span>'
            f'{html.escape(status)}</span>')


def legend() -> str:
    c = colors()
    items = "".join(
        f'<span><span class="sams-dot" style="background:{c[s]}"></span>{STATUS_LETTER[s]} = {s}</span>'
        for s in STATUS_ORDER)
    return f'<div class="sams-legend">{items}</div>'


def pct_text(value, digits: int = 1) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:.{digits}f}%"


def calendar_html(year: int, month: int, records: pd.DataFrame, today: date) -> str:
    """Month grid with one line per class meeting: status dot, letter and class code."""
    c = colors()
    by_day: dict[int, list] = {}
    for r in records.itertuples():
        day = pd.Timestamp(r.date)
        if day.year == year and day.month == month:
            by_day.setdefault(day.day, []).append(r)
    head = "".join(f"<th>{d}</th>" for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    body = ""
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        body += "<tr>"
        for day in week:
            if day == 0:
                body += '<td class="empty"></td>'
                continue
            cls = ' class="today"' if date(year, month, day) == today else ""
            lines = ""
            for r in sorted(by_day.get(day, []), key=lambda x: x.start_time):
                tip = html.escape(f"{r.code} {r.start_time}: {r.status}" + (f" ({r.note})" if r.note else ""))
                lines += (f'<div class="rec" title="{tip}"><span class="sams-dot" style="background:{c[r.status]}"></span>'
                          f'<b>{STATUS_LETTER[r.status]}</b><span class="code">{html.escape(r.code)}</span></div>')
            body += f'<td{cls}><div class="num">{day}</div>{lines}</td>'
        body += "</tr>"
    return f'<table class="sams-cal"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{legend()}'


# ---------------------------------------------------------------------- charts
def _base(chart: alt.Chart, height: int) -> alt.Chart:
    c = colors()
    return (chart.properties(height=height)
            .configure_view(strokeWidth=0)
            .configure_axis(gridColor=c["grid"], domainColor=c["grid"], tickColor=c["grid"],
                            labelColor=c["muted"], titleColor=c["muted"], labelFontSize=12, titleFontSize=12)
            .configure_legend(labelColor=c["ink"], titleColor=c["muted"], orient="top", title=None,
                              labelFontSize=12, symbolType="square"))


def trend_chart(daily: pd.DataFrame, threshold: float, height: int = 260):
    """Attendance % per day: one series, so one colour and no legend."""
    c = colors()
    data = daily.dropna(subset=["pct"]).copy()
    if data.empty:
        st.info("No attendance has been recorded in this period.")
        return
    low = max(0, int(min(data["pct"].min(), threshold) // 10) * 10 - 10)
    x = alt.X("date:T", title=None, axis=alt.Axis(format="%d %b", grid=False, labelAngle=0, tickCount=8))
    y = alt.Y("pct:Q", title="Attendance %", scale=alt.Scale(domain=[low, 100], zero=False, nice=False),
              axis=alt.Axis(tickCount=5))
    tips = [alt.Tooltip("date:T", title="Date", format="%a %d %b %Y"), alt.Tooltip("pct:Q", title="Attendance %"),
            alt.Tooltip("present:Q", title="Present"), alt.Tooltip("late:Q", title="Late"),
            alt.Tooltip("absent:Q", title="Absent"), alt.Tooltip("excused:Q", title="Excused")]
    line = alt.Chart(data).mark_line(color=c["series"], strokeWidth=2).encode(x=x, y=y)
    points = alt.Chart(data).mark_circle(color=c["series"], size=70, stroke=c["surface"], strokeWidth=2).encode(
        x=x, y=y, tooltip=tips)
    rule_data = pd.DataFrame({"y": [threshold], "label": [f"Required {threshold:.0f}%"]})
    rule = alt.Chart(rule_data).mark_rule(color=c["muted"], strokeDash=[4, 4], strokeWidth=1).encode(y="y:Q")
    label = alt.Chart(rule_data).mark_text(align="left", dx=4, dy=-7, color=c["muted"], fontSize=11).encode(
        x=alt.value(0), y="y:Q", text="label:N")
    st.altair_chart(_base(alt.layer(rule, label, line, points), height), width="stretch")


def percent_bars(data: pd.DataFrame, label: str, threshold: float, row_height: int = 34):
    """Horizontal bars of attendance % per class, with the threshold marked."""
    c = colors()
    data = data.dropna(subset=["pct"]).copy()
    if data.empty:
        st.info("No attendance has been recorded yet.")
        return
    order = list(data[label])
    y = alt.Y(f"{label}:N", sort=order, title=None, axis=alt.Axis(labelLimit=260, ticks=False, domain=False))
    x = alt.X("pct:Q", title="Attendance %", scale=alt.Scale(domain=[0, 100]), axis=alt.Axis(tickCount=5))
    tips = [alt.Tooltip(f"{label}:N", title="Class"), alt.Tooltip("pct:Q", title="Attendance %"),
            alt.Tooltip("present:Q", title="Present"), alt.Tooltip("late:Q", title="Late"),
            alt.Tooltip("absent:Q", title="Absent"), alt.Tooltip("excused:Q", title="Excused")]
    bars = alt.Chart(data).mark_bar(color=c["series"], size=14, cornerRadiusEnd=4).encode(x=x, y=y, tooltip=tips)
    values = alt.Chart(data).mark_text(align="left", dx=6, color=c["ink"], fontSize=12).encode(
        x=x, y=y, text=alt.Text("pct:Q", format=".1f"))
    rule_data = pd.DataFrame({"x": [threshold]})
    rule = alt.Chart(rule_data).mark_rule(color=c["muted"], strokeDash=[4, 4], strokeWidth=1).encode(x="x:Q")
    st.altair_chart(_base(alt.layer(bars, values, rule), max(120, row_height * len(data) + 40)),
                    width="stretch")
    st.caption(f"Dashed line: required {threshold:.0f}%. Late counts as attended; excused classes are not counted.")


def status_mix_chart(mix: pd.DataFrame, height: int = 260):
    """Stacked weekly counts by status, with a table view for exact numbers."""
    c = colors()
    if mix.empty:
        st.info("No attendance has been recorded in this period.")
        return
    data = mix.copy()
    data["order"] = data["status"].map({s: i for i, s in enumerate(STATUS_ORDER)})
    data["week_label"] = pd.to_datetime(data["week"]).dt.strftime("%d %b")
    weeks = list(dict.fromkeys(data.sort_values("week")["week_label"]))
    chart = alt.Chart(data).mark_bar(size=30, stroke=c["surface"], strokeWidth=2).encode(
        x=alt.X("week_label:N", sort=weeks, title="Week starting", axis=alt.Axis(labelAngle=0, grid=False)),
        y=alt.Y("count:Q", title="Records", stack="zero"),
        color=alt.Color("status:N", scale=alt.Scale(domain=STATUS_ORDER, range=[c[s] for s in STATUS_ORDER])),
        order=alt.Order("order:Q"),
        tooltip=[alt.Tooltip("week_label:N", title="Week starting"), alt.Tooltip("status:N", title="Status"),
                 alt.Tooltip("count:Q", title="Records")],
    )
    st.altair_chart(_base(chart, height), width="stretch")
    with st.expander("View as table"):
        table = data.pivot_table(index="week_label", columns="status", values="count", fill_value=0)
        st.dataframe(table.reindex(index=weeks, columns=STATUS_ORDER, fill_value=0), width="stretch")
