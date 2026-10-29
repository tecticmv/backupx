"""
Crontab day-of-week semantics for job schedules, the retention prune and the
overdue check.

APScheduler numbers weekdays from Monday (0 = mon), crontab from Sunday
(0/7 = sun). Schedules used to be passed straight through, so numeric weekdays
ran a day late. 2026-10-26 is a Monday.
"""
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from app.cron import cron_day_of_week, cron_fields, cron_trigger

MONDAY = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _fire_days(expr, tz='UTC', days=14):
    """Weekday names (Mon..Sun) of fires within `days` from Monday 00:00 UTC."""
    trigger = cron_trigger(expr, tz)
    out, fire = [], trigger.get_next_fire_time(None, MONDAY)
    end = MONDAY + timedelta(days=days)
    while fire is not None and fire < end:
        out.append(fire.strftime('%a'))
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
    return out


def _croniter_days(expr, days=14):
    """Reference: what standard cron (croniter) does for the same expression."""
    it = croniter(expr, MONDAY - timedelta(seconds=1))
    out, end = [], MONDAY + timedelta(days=days)
    while True:
        fire = it.get_next(datetime)
        if fire >= end:
            return out
        out.append(fire.strftime('%a'))


@pytest.mark.parametrize('field, expected', [
    ('*', '*'),
    ('?', '*'),
    ('0', 'sun'),
    ('7', 'sun'),
    ('1', 'mon'),
    ('6', 'sat'),
    ('1-5', 'mon,tue,wed,thu,fri'),
    ('0-6', '*'),
    ('0-7', '*'),
    ('5-7', 'fri,sat,sun'),
    ('1,3,5', 'mon,wed,fri'),
    ('0,6', 'sat,sun'),
    ('*/2', 'tue,thu,sat,sun'),     # 0,2,4,6
    ('1-5/2', 'mon,wed,fri'),
    ('1/2', 'mon,wed,fri'),         # 1 through 6, step 2
    ('sun', 'sun'),
    ('SUN', 'sun'),
    ('mon-fri', 'mon,tue,wed,thu,fri'),
    ('fri-sun', 'fri,sat,sun'),
    ('sat,sun', 'sat,sun'),
    ('mon,3,fri', 'mon,wed,fri'),
])
def test_day_of_week_translation(field, expected):
    assert cron_day_of_week(field) == expected


@pytest.mark.parametrize('field', ['8', '-1', 'funday', '5-1', '1-', '', '1,,2', '*/0', '*/x', 'L', '1#2'])
def test_invalid_day_of_week_rejected(field):
    with pytest.raises(ValueError):
        cron_day_of_week(field)


@pytest.mark.parametrize('expr', [
    '0 2 * * *',
    '0 2 * * 0',
    '0 2 * * 7',
    '0 2 * * 1',
    '0 2 * * 6',
    '0 2 * * 1-5',
    '0 2 * * 1,3,5',
    '30 4 * * 0,6',
    '0 2 * * */2',
    '0 2 * * 1-5/2',
    '0 4 * * sun',
    '0 4 * * mon-fri',
])
def test_fire_days_match_standard_cron(expr):
    assert _fire_days(expr) == _croniter_days(expr)


def test_sunday_zero_fires_on_sunday_not_monday():
    assert _fire_days('0 2 * * 0', days=7) == ['Sun']
    assert _fire_days('0 2 * * 7', days=7) == ['Sun']
    assert _fire_days('0 2 * * 1-5', days=7) == ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    # the old pass-through behaviour, for the record: APScheduler reads 0 as Monday
    raw = CronTrigger(minute='0', hour='2', day_of_week='0', timezone='UTC')
    assert raw.get_next_fire_time(None, MONDAY).strftime('%a') == 'Mon'


def test_other_fields_pass_through():
    assert cron_fields('*/15 1-3 1,15 */2 1') == {
        'minute': '*/15', 'hour': '1-3', 'day': '1,15', 'month': '*/2', 'day_of_week': 'mon'}


@pytest.mark.parametrize('expr', ['', '0 2 * *', '0 2 * * * *'])
def test_wrong_field_count_rejected(expr):
    with pytest.raises(ValueError):
        cron_fields(expr)


def test_timezone_respected():
    # Sunday 02:00 in Maldives time is Saturday 21:00 UTC
    fire = cron_trigger('0 2 * * 0', 'Indian/Maldives').get_next_fire_time(None, MONDAY)
    assert fire.strftime('%a %H:%M') == 'Sun 02:00'
    assert fire.astimezone(timezone.utc).strftime('%a %H:%M') == 'Sat 21:00'


# --- call sites --------------------------------------------------------------

def test_scheduler_sync_uses_cron_weekdays(monkeypatch):
    from app import main

    added = {}

    class FakeScheduler:
        running = True
        timezone = 'UTC'

        def add_job(self, func, trigger, **kwargs):
            added[kwargs['id']] = kwargs

        def remove_job(self, job_id):
            added.pop(job_id, None)

        def start(self):
            pass

    monkeypatch.setattr(main, 'scheduler', FakeScheduler())
    monkeypatch.setattr(main, '_scheduled_crons', {})
    monkeypatch.setattr(main, '_scheduled_prune', {'cron': None})
    monkeypatch.setenv('RETENTION_ENABLED', 'true')
    monkeypatch.setenv('RETENTION_PRUNE_CRON', '0 4 * * 0')
    monkeypatch.setattr(main, 'load_jobs', lambda: {
        'weekdays': {'schedule_enabled': True, 'schedule_cron': '0 2 * * 1-5'},
        'sunday': {'schedule_enabled': True, 'schedule_cron': '0 3 * * 7'},
    })
    monkeypatch.setattr(main, 'get_scheduler_timezone', lambda: 'UTC')
    main.sync_schedules()

    assert added['weekdays']['day_of_week'] == 'mon,tue,wed,thu,fri'
    assert added['sunday']['day_of_week'] == 'sun'
    from app import retention
    assert added[retention.PRUNE_SCHEDULE_ID]['day_of_week'] == 'sun'


def test_overdue_uses_cron_weekdays():
    from app.main import job_overdue_since

    # Sunday-only job (crontab 0), checked on Monday 12:00 with a 1h grace:
    # it was due Sunday 02:00, not "Monday 02:00" as APScheduler would read 0.
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    job = {'schedule_enabled': True, 'schedule_cron': '0 2 * * 0', 'status': 'success',
           'created_at': '2026-01-01T00:00:00', 'last_run': '2026-10-18T02:00:05'}
    due = job_overdue_since(job, 'UTC', now=now, grace=3600)
    assert due == datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)

    job['last_run'] = '2026-10-25T02:00:05'
    assert job_overdue_since(job, 'UTC', now=now, grace=3600) is None
