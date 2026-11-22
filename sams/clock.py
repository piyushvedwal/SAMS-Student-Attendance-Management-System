"""Single source of 'now' for the app, in institute local time (IST)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()


def stamp() -> str:
    return now().strftime(DATETIME_FMT)


def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], DATE_FMT).date()


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())
