"""Exact Gregorian date arithmetic."""
import calendar
import datetime

WEEKDAYS = {"ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
            "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}


def parse(value, ctx):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        ctx.fail(f"invalid date {value!r}: use YYYY-MM-DD")


def add_months(date, months):
    year, month = divmod(date.month - 1 + months, 12)
    year, month = date.year + year, month + 1
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


def breakdown(start, end):
    """Whole months first (a day past the month end is clamped, as calendars do), then the remaining days."""
    sign = 1
    if end < start:
        start, end, sign = end, start, -1
    months = (end.year - start.year) * 12 + end.month - start.month
    if add_months(start, months) > end:
        months -= 1
    days = (end - add_months(start, months)).days
    return {"years": sign * (months // 12), "months": sign * (months % 12), "days": sign * days}


def run(args, ctx):
    names = WEEKDAYS[args.get("language", "ru")]
    date = parse(args["date"], ctx)
    operation = args["operation"]
    if operation == "between":
        if "other_date" not in args:
            ctx.fail("between needs other_date")
        other = parse(args["other_date"], ctx)
        days = (other - date).days
        low, high = sorted((date, other))
        weeks, rest = divmod((high - low).days, 7)
        workdays = weeks * 5 + sum((low + datetime.timedelta(days=i)).weekday() < 5 for i in range(rest))
        return {"from": date.isoformat(), "to": other.isoformat(), "days": days, "weeks": round(days / 7, 2),
                "breakdown": breakdown(date, other), "weekdays_mon_fri": workdays,
                "note": "the end date is not counted"}
    if operation == "shift":
        if "days" not in args:
            ctx.fail("shift needs days")
        try:
            result = date + datetime.timedelta(days=args["days"])
        except OverflowError:
            ctx.fail("the resulting date is outside years 1-9999")
        return {"from": date.isoformat(), "days": args["days"], "date": result.isoformat(),
                "weekday": names[result.weekday()]}
    iso = date.isocalendar()
    return {"date": date.isoformat(), "weekday": names[date.weekday()], "iso_week": iso.week, "iso_year": iso.year,
            "day_of_year": date.timetuple().tm_yday, "leap_year": calendar.isleap(date.year),
            "days_left_in_year": (datetime.date(date.year, 12, 31) - date).days}
