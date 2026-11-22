# SAMS: Student Attendance Management System

[![tests](https://github.com/piyushvedwal/SAMS-Student-Attendance-Management-System/actions/workflows/tests.yml/badge.svg)](https://github.com/piyushvedwal/SAMS-Student-Attendance-Management-System/actions/workflows/tests.yml)

**[▶ Live demo](https://sams-piyush-vedwal.streamlit.app/)** &nbsp;|&nbsp; Python · Streamlit · SQLite · pandas · Altair

A web app that digitises attendance for an educational institute. Teachers mark it, students and parents follow it, and administrators get reports and alerts on the patterns that need attention.

It is built from my own [Software Requirements Specification (SAMS v3.0)](https://drive.google.com/file/d/13AoEI0MQ890NerwzBkql2jHTir0z27br/view?usp=share_link): the roles, features, data model and use cases below follow that document, and the [coverage table](#srs-coverage) says plainly what is and is not implemented.

## Try it

Open the live demo and use the one-click sign-in for any role, or type a username with the password `Demo@1234`.

| Role | Username | What to look at |
|---|---|---|
| Administrator | `admin` | Institute dashboard, alerts, reports and exports, users, timetable changes, settings, audit log |
| Teacher | `kavita.rao` | A class waiting to be marked, student check-in codes, a flagged record and a leave request to review |
| Student | `aarav.sharma` | Calendar history, a class below the 75% threshold, upcoming schedule with a cancelled class |
| Parent | `parent.aarav.sharma` | Read-only view of the child's attendance, plus absence alerts |

The demo data is fictional (48 students, 4 teachers, 8 classes, about 5,000 attendance records) and rebuilds itself every day so the history is always recent.

## Features

**Teachers**
- Mark a class from the roster: Present, Absent, Late or Excused, with an optional note per student
- Bulk actions (all present / all absent) and corrections to attendance that is already saved
- **Student check-in**: open a 6-character code that is valid for a few minutes; students mark themselves, and late check-ins are recorded as Late
- Students on approved leave are shown on the roster and default to Excused
- Review records that students have flagged as wrong, and approve or reject leave requests

**Students**
- Dashboard with overall and per-class percentage, and how many classes can still be missed (or must be attended) to stay above the threshold
- Calendar view of every class, with filters by class and status, search, and a monthly summary
- Upcoming schedule and weekly timetable, including cancelled and moved classes
- Flag a record for review, request leave, and choose which notifications to receive

**Parents**
- Read-only overview, calendar and schedule for their child, with absence and low-attendance alerts

**Administrators**
- Dashboard: attendance trend, attendance by class, weekly status mix, students below the threshold
- Reports for today, this week, this month or any date range, by class or student, exported to **CSV, Excel or PDF**
- **Alerts** on unusual patterns (see below), classes left unmarked, and flagged check-ins
- User management, enrolment, timetable, one-off schedule changes with automatic notifications
- Configurable thresholds and timeouts, database backup download, full audit log

## How it works

### Attendance percentage

```
attendance % = (Present + Late) / (Present + Late + Absent)
```

Late counts as attended. Excused classes are left out entirely, so approved leave never lowers a percentage. Approving a leave request also turns absences already recorded in that period into Excused.

### Notifications and escalation

Alerts are stored per user with a de-duplication key, so one event never notifies the same person twice.

| Event | Who is told |
|---|---|
| Student marked absent | Student and parent |
| Attendance in a class falls below the low threshold (default 75%) | Student and parent, at most once a week per class |
| Falls below the critical threshold (default 60%) | Escalated to the class teacher and administrators |
| A finished class has no attendance | Reminder to its teacher |
| Class cancelled or moved | Every enrolled student and the teacher |
| Unusual pattern detected | Administrators |

### Unusual-pattern detection

Plain, explainable statistics over the attendance records:

- **Sudden drop**: a student's attendance over the last 14 days is at least 30 points below their own earlier record
- **Absence streak**: a student's 4 or more most recent classes are all absent
- **Class outlier**: one class meeting is more than 2.5 standard deviations (and 25 points) below that class's usual turnout
- **Flagged check-in**: a self check-in from a device already used by another student in the same window, or by a student on approved leave

The check-in device signature is coarse (network address plus browser), so identical phones on one network can look alike. Flagged check-ins are therefore recorded and shown to the teacher for review, never rejected automatically.

### Security

- Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes (200,000 iterations) and must meet a strength policy
- Role-based access: each role only ever sees its own screens and its own data
- Accounts lock for 5 minutes after 5 failed sign-ins; sessions sign out after inactivity
- Sign-ins, attendance changes and administrative actions are written to an audit log

### Data model

The tables follow the entity-relationship diagram in the SRS:

```
USER ──< TEACHER ──< CLASS ──< SCHEDULE ──< ATTENDANCE >── STUDENT >── USER
  │                    │                                      │
  └──< NOTIFICATION    └──< ENROLMENT >───────────────────────┘
```

Supporting tables: `leaves`, `flags` (discrepancy reports), `checkin_windows`, `checkins`, `schedule_changes`, `notification_prefs`, `settings`, `audit_log`.

## SRS coverage

| SRS area | Status | Notes |
|---|---|---|
| 3.1.1 Authentication and role-based access | ✅ Implemented | Four roles, password policy, lockout, inactivity timeout, audit log |
| 3.1.2 / 4.1 Attendance marking | ✅ Implemented | Manual, bulk and student check-in; Present / Absent / Late / Excused; notes |
| 3.1.3 / 4.4 Student history | ✅ Implemented | Calendar view, filters, search, period summary, flag for review |
| 4.5 Class schedule tracking | ✅ Implemented | Timetable, upcoming classes, change notifications, attendance requirement per class |
| 3.1.4 / 4.2 Reporting and analytics | ✅ Implemented | Daily, weekly, monthly and custom; trends; at-risk list; CSV, Excel, PDF |
| 3.1.5 / 4.3 Notifications and alerts | 🟡 Partial | In-app only. Thresholds, preferences, escalation and teacher reminders work; **email, SMS and push are not connected** |
| 2.3 Parent portal | ✅ Implemented | Read-only |
| 6 Security | 🟡 Partial | Hashing, RBAC, policy, timeout and audit are done. **No multi-factor authentication; the database file is not encrypted at rest** (HTTPS is provided by the host) |
| 7.1 Backups | 🟡 Partial | Manual database download by an administrator; no automatic daily backup |
| 3.3 Mobile | 🟡 Partial | Responsive web app; no native Android or iOS apps |
| 2.5 LMS integration and external API | ❌ Not implemented | |
| 3.3 Barcode, QR and biometric hardware | ❌ Not implemented | The check-in code covers the self-marking use case |
| 2.4 MySQL / PostgreSQL | ❌ Not implemented | Uses SQLite, behind one small module (`sams/db.py`) |
| 5 Performance and scale targets | ❌ Not verified | 2,000 concurrent users and 100,000 students were not load tested; SQLite on one server is not designed for that |

## Run locally

```bash
git clone https://github.com/piyushvedwal/SAMS-Student-Attendance-Management-System.git
cd SAMS-Student-Attendance-Management-System
pip install -r requirements.txt
streamlit run app.py
```

The database (`data/sams.db`) and demo data are created on first start.

For a real deployment set `SAMS_DEMO=0`: no demo data is created, the one-click sign-ins disappear, and a single `admin` account is created with the password from `SAMS_ADMIN_PASSWORD`. Note that hosts with an ephemeral disk (including Streamlit Community Cloud) lose the SQLite file on restart.

## Tests

```bash
pip install pytest
pytest
```

39 tests: the rules (percentages, marking, check-in and proxy flags, notifications and escalation, leave, flags, schedule changes, pattern detection, exports) and end-to-end runs of the app for every role.

## Project structure

```
app.py                  Sign-in, navigation and session handling
sams/
  db.py                 SQLite schema and access
  auth.py               Passwords, lockout, users
  attendance.py         Sessions, marking, percentages, check-in
  notifications.py      Alerts, thresholds, escalation, reminders
  analytics.py          Aggregates and unusual-pattern detection
  reports.py            Report building and CSV / Excel / PDF export
  workflow.py           Flags, leave requests, schedule changes
  seed.py               Demo data
  ui.py                 Colours, calendar and charts
  views/                Screens for each role
tests/                  Rule tests and end-to-end app tests
```

## Author

**Piyush Vedwal** · [Portfolio](https://portfolio-piyush-vedwal.streamlit.app/) · [GitHub](https://github.com/piyushvedwal) · [LinkedIn](https://www.linkedin.com/in/piyush-vedwal/)
