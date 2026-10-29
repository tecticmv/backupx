"""
Snapshot retention: turn a job's retention fields into restic forget/prune
commands, and check the result before anything is removed.

Only pure helpers live here (no database, no subprocess), so the policy and the
safety guards can be tested on their own. app.main runs the commands.

How snapshots are matched: backupx tags every snapshot it writes with
"automated" (database dumps also with "postgres-backup" / "mysql-backup"), and
restic records the target server's hostname. Forget only looks at "automated"
snapshots, so anything added to a repository by hand is never touched, and the
policy is applied per host + tags (+ paths for file jobs). Database dumps are
written to a fresh mktemp directory each run, so their paths differ every time
and must not be part of the group, or every dump would be its own group and
nothing would ever be removed.
"""

import os
import re

# Job field -> restic flag. These are the only retention fields jobs have.
RETENTION_RULES = (
    ('retention_hourly', '--keep-hourly'),
    ('retention_daily', '--keep-daily'),
    ('retention_weekly', '--keep-weekly'),
    ('retention_monthly', '--keep-monthly'),
)

BACKUPX_TAG = 'automated'

# Name of the weekly prune entry in the APScheduler job store (never a job id:
# those come from generate_id and have no underscores at the ends)
PRUNE_SCHEDULE_ID = '__retention_prune__'


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def settings():
    """Retention settings from the environment, read on every call."""
    return {
        # Off by default: deploying the code changes nothing until this is turned on
        'enabled': _env_bool('RETENTION_ENABLED', False),
        # Log what would be removed / freed without removing anything
        'dry_run': _env_bool('RETENTION_DRY_RUN', False),
        # Crontab expression in the scheduler timezone (day of week 0/7 = Sunday,
        # or day names); app.cron translates it for APScheduler.
        'prune_cron': (os.environ.get('RETENTION_PRUNE_CRON') or '0 4 * * sun').strip(),
        'prune_max_unused': (os.environ.get('RETENTION_PRUNE_MAX_UNUSED') or '10%').strip(),
        # How long restic waits for another process's repository lock
        'lock_wait': (os.environ.get('RETENTION_LOCK_WAIT') or '10m').strip(),
        'forget_timeout': int(os.environ.get('RETENTION_FORGET_TIMEOUT') or 900),
        'prune_timeout': int(os.environ.get('RETENTION_PRUNE_TIMEOUT') or 4 * 3600),
    }


def retention_policy(job):
    """The job's keep rules as [(flag, count)], only rules that keep at least one.

    0, empty, negative or unparseable values mean "rule not set". An empty
    result means the job has no retention policy and nothing may be forgotten.
    """
    policy = []
    for field, flag in RETENTION_RULES:
        try:
            count = int(job.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            policy.append((flag, count))
    return policy


def describe_policy(job):
    policy = retention_policy(job)
    if not policy:
        return 'none (keep everything)'
    return ', '.join(f"{count} {flag.rsplit('-', 1)[-1]}" for flag, count in policy)


def group_by(job):
    """restic --group-by for a job's snapshots (see the module docstring)."""
    if job.get('backup_type') == 'database':
        return 'host,tags'
    return 'host,paths,tags'


def forget_plan_args(job):
    """restic arguments that compute (never apply) the forget plan, or None.

    Always --dry-run: the snapshots to remove are checked by check_forget_plan
    and then removed by ID with forget_ids_args, so nothing is ever removed that
    wasn't checked. None means the job has no policy: do nothing.
    """
    policy = retention_policy(job)
    if not policy:
        return None
    args = ['forget', '--json', '--dry-run', '--tag', BACKUPX_TAG, '--group-by', group_by(job)]
    for flag, count in policy:
        args += [flag, str(count)]
    return args


def forget_ids_args(snapshot_ids):
    return ['forget', *snapshot_ids]


def prune_args(dry_run, max_unused):
    args = ['prune', '--max-unused', max_unused]
    if dry_run:
        args.append('--dry-run')
    return args


def _short_id(snapshot):
    return snapshot.get('short_id') or (snapshot.get('id') or '')[:8]


def check_forget_plan(groups):
    """Validate restic's forget plan (the --json --dry-run output).

    Returns (remove, keep_count, problems). remove is a list of snapshot dicts.
    A group where the policy keeps nothing but would remove something is a
    problem: the whole run is refused rather than empty a group.
    """
    remove, keep_count, problems = [], 0, []
    for group in groups or []:
        keep = group.get('keep') or []
        gone = group.get('remove') or []
        keep_count += len(keep)
        if gone and not keep:
            label = group.get('host') or '?'
            if group.get('tags'):
                label += ' [' + ','.join(group['tags']) + ']'
            problems.append(f"policy would remove all {len(gone)} snapshot(s) of {label}")
        for snap in gone:
            if not snap.get('id'):
                problems.append('restic returned a snapshot to remove without an id')
            else:
                remove.append(snap)
    return remove, keep_count, problems


def summarize_removed(snapshots, limit=10):
    ids = [_short_id(s) for s in snapshots]
    text = ', '.join(ids[:limit])
    if len(ids) > limit:
        text += f' (+{len(ids) - limit} more)'
    return text


_UNITS = {'B': 1, 'KIB': 1024, 'MIB': 1024 ** 2, 'GIB': 1024 ** 3, 'TIB': 1024 ** 4,
          'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3, 'TB': 1000 ** 4}


def parse_size(text):
    """'1.234 MiB' -> bytes (int), None if it isn't a size."""
    match = re.match(r'\s*([\d.]+)\s*([KMGT]?i?B)\b', text or '', re.IGNORECASE)
    if not match:
        return None
    unit = _UNITS.get(match.group(2).upper())
    if unit is None:
        return None
    return int(float(match.group(1)) * unit)


_PRUNE_SUMMARY = re.compile(
    r'^\s*(to repack|this removes|to delete|total prune|remaining|unused size after prune)\s*:\s*(.*)$',
    re.IGNORECASE)


def parse_prune_output(text):
    """Pick restic prune's summary out of its text output.

    Returns {'bytes_freed': int|None, 'summary': str}. bytes_freed is the
    "total prune" size: what this prune removes (or would, with --dry-run).
    """
    lines, freed = [], None
    for line in (text or '').splitlines():
        match = _PRUNE_SUMMARY.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        lines.append(f'{key}: {value}')
        if key == 'total prune':
            # "3 blobs / 1.234 MiB"
            freed = parse_size(value.split('/')[-1])
    return {'bytes_freed': freed, 'summary': '; '.join(lines)}


def parse_restic_version(text):
    """'restic 0.18.1 compiled with go1.25 on linux/amd64' -> (0, 18, 1), None if unknown."""
    match = re.search(r'restic\s+(\d+)\.(\d+)\.(\d+)', text or '')
    return tuple(int(x) for x in match.groups()) if match else None


def supports_retry_lock(version):
    """--retry-lock exists since restic 0.16.0."""
    return version is not None and version >= (0, 16, 0)
