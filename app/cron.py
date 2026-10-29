"""
Standard crontab expressions -> APScheduler CronTrigger fields.

APScheduler 3.x numbers day_of_week from Monday (0 = mon ... 6 = sun), while
crontab numbers it from Sunday (0 or 7 = sun ... 6 = sat). Passing a crontab
day-of-week field straight through makes every numeric weekday run a day late
(and rejects 7). CronTrigger.from_crontab doesn't help: in 3.10 it passes the
field through unchanged too. So the day-of-week field is translated here into
APScheduler day names; the other four fields mean the same in both.
"""
from apscheduler.triggers.cron import CronTrigger

# Crontab weekday number -> APScheduler name (index = crontab number, 7 = Sunday again)
_CRON_DAY_NAMES = ('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat')
_NAME_TO_CRON = {name: i for i, name in enumerate(_CRON_DAY_NAMES)}
# APScheduler's own weekday order, used for the output
_APS_ORDER = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def _cron_day(value, field):
    """One weekday token (number 0-7 or name) as a crontab number 0-7."""
    token = value.strip().lower()
    if token in _NAME_TO_CRON:
        return _NAME_TO_CRON[token]
    if token.isdigit() and 0 <= int(token) <= 7:
        return int(token)
    raise ValueError(f"invalid day of week {value!r} in {field!r}")


def cron_day_of_week(field):
    """Translate a crontab day-of-week field into an APScheduler day_of_week value.

    Supports '*', numbers 0-7 (0 and 7 = Sunday), day names, ranges (1-5,
    mon-fri), lists (1,3,5) and steps (*/2, 1-5/2, 1/2). Returns '*' or a list
    of APScheduler day names. Raises ValueError for anything else.
    """
    field = (field or '').strip()
    if not field:
        raise ValueError('empty day-of-week field')
    if field in ('*', '?'):
        return '*'
    days = set()
    for item in field.split(','):
        item = item.strip()
        if not item:
            raise ValueError(f"empty item in day-of-week field {field!r}")
        base, slash, step_text = item.partition('/')
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"invalid step in day-of-week field {field!r}")
            step = int(step_text)
        if base in ('*', '?'):
            start, end = 0, 6
        elif '-' in base:
            first, last = base.split('-', 1)
            start, end = _cron_day(first, field), _cron_day(last, field)
            if end == 0 and start > 0:
                end = 7  # "fri-sun" / "5-0": Sunday at the end of a range
            if start > end:
                raise ValueError(f"descending range {base!r} in day-of-week field {field!r}")
        else:
            start = _cron_day(base, field)
            # crontab "n/step" means n through the last day
            end = 6 if slash and start <= 6 else start
        days.update(n % 7 for n in range(start, end + 1, step))
    if len(days) == 7:
        return '*'
    names = {_CRON_DAY_NAMES[n] for n in days}
    return ','.join(name for name in _APS_ORDER if name in names)


def cron_fields(expression):
    """Five-field crontab expression -> CronTrigger keyword arguments (standard cron weekdays)."""
    parts = (expression or '').split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expression!r}")
    minute, hour, day, month, day_of_week = parts
    return {'minute': minute, 'hour': hour, 'day': day, 'month': month,
            'day_of_week': cron_day_of_week(day_of_week)}


def cron_trigger(expression, timezone=None):
    """CronTrigger for a crontab expression, with crontab day-of-week semantics."""
    return CronTrigger(timezone=timezone, **cron_fields(expression))
