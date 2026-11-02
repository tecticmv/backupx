"""
Restore wizard: pure decisions (no I/O).

The wizard (docs/UX-OVERHAUL.md, P2) restores a job into a *scratch* copy on
the backupx server first: a throwaway database container (label
backupx.restore=<id>, no published ports, removed after RESTORE_TTL_HOURS) or
an extract directory for file jobs. Replacing production is a second, gated
step (typed database name + step-up + plan hash) that takes a pre-restore
backup first (restic tag pre-restore, kept PRE_RESTORE_KEEP_DAYS, never
touched by retention, which only forgets --tag automated) and can be undone.

This module holds what can be decided without touching anything: settings,
names, size estimates, the disk check, the restore-point summary (PITR windows
and gaps), the key-table / verification comparison, image picking from dump
headers and the parsing of the marker output the scripts print.
"""

import os
import re
from datetime import datetime, timedelta, timezone

from . import pitr

LABEL = 'backupx.restore'
LABEL_JOB = 'backupx.job'
CONTAINER_PREFIX = 'backupx-restore-'
PRE_RESTORE_TAG = 'pre-restore'

ENGINE_FILES = 'files'
ENGINE_MYSQL = 'mysql'
ENGINE_MARIADB = 'mariadb'
ENGINE_POSTGRES = 'postgres'
DB_ENGINES = (ENGINE_MYSQL, ENGINE_MARIADB, ENGINE_POSTGRES)

POINT_LATEST = 'latest'
POINT_SNAPSHOT = 'snapshot'
POINT_TIME = 'time'
POINT_TYPES = (POINT_LATEST, POINT_SNAPSHOT, POINT_TIME)

TARGET_SCRATCH = 'scratch'
TARGET_DOWNLOAD = 'download'
DB_TARGETS = (TARGET_SCRATCH, TARGET_DOWNLOAD)
FILE_TARGETS = ('original', 'alternate', 'scratch')

# Restore row statuses
S_RUNNING = 'running'
S_READY = 'ready'
S_FAILED = 'failed'
S_CANCELLED = 'cancelled'
S_REMOVED = 'removed'
S_EXPIRED = 'expired'
S_REPLACING = 'replacing'
S_REPLACED = 'replaced'
S_UNDOING = 'undoing'
S_UNDONE = 'undone'
S_DONE = 'done'
# A scratch container may exist in these states (cleanup leaves them alone
# unless they are past cleanup_at or their task died)
LIVE_STATUSES = (S_RUNNING, S_READY, S_REPLACING, S_REPLACED, S_UNDOING, S_UNDONE)

MYSQL_SYSTEM_SCHEMAS = ('mysql', 'sys', 'information_schema', 'performance_schema')
PG_SKIP_DATABASES = ('template0', 'template1')


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def settings():
    base = os.environ.get('APP_DATA_DIR', '/app/data')
    return {
        'work_dir': os.environ.get('RESTORE_WORK_DIR') or os.path.join(base, 'restores'),
        'ttl_hours': max(1, _env_int('RESTORE_TTL_HOURS', 24)),
        'max_keep_hours': max(24, _env_int('RESTORE_MAX_KEEP_HOURS', 7 * 24)),
        'pre_restore_keep_days': max(1, _env_int('PRE_RESTORE_KEEP_DAYS', 30)),
        'disk_factor': max(1.0, _env_float('RESTORE_DISK_FACTOR', 2.5)),
        'dump_expansion': max(1.0, _env_float('RESTORE_DUMP_EXPANSION', 5.0)),
        'docker_network': os.environ.get('RESTORE_DOCKER_NETWORK', '').strip(),
        'helper_image': os.environ.get('RESTORE_HELPER_IMAGE', 'busybox:1.36').strip(),
        'host_files_dir': os.environ.get('RESTORE_HOST_FILES_DIR', '/var/tmp/backupx-restore').rstrip('/'),
        'timeout': max(600, _env_int('RESTORE_TIMEOUT', 6 * 3600)),
        'cleanup_minutes': max(1, _env_int('RESTORE_CLEANUP_MINUTES', 10)),
        'ready_timeout': max(60, _env_int('RESTORE_READY_TIMEOUT', 900)),
        'import_rate': max(1, _env_int('RESTORE_IMPORT_MB_PER_S', 10)),
        'find_limit': max(10, _env_int('RESTORE_FIND_LIMIT', 500)),
    }


def utcnow():
    return datetime.now(timezone.utc)


def utc(value):
    return pitr._utc(value) if value is not None else None


def iso(value):
    value = utc(value)
    return value.isoformat() if value else None


def container_name(restore_id):
    return f'{CONTAINER_PREFIX}{restore_id}'


def fmt_bytes(n):
    if n is None:
        return '?'
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def fmt_time(value):
    value = utc(value)
    return value.strftime('%Y-%m-%d %H:%M:%S UTC') if value else '?'


# ---------------------------------------------------------------------------
# Engines and images
# ---------------------------------------------------------------------------

def engine_for(job, db_config=None, server_version=''):
    """files / mysql / mariadb / postgres for a job."""
    if job.get('backup_type') != 'database':
        return ENGINE_FILES
    db_type = ((db_config or {}).get('type') or 'mysql').lower()
    if db_type in ('postgres', 'postgresql'):
        return ENGINE_POSTGRES
    if db_type == 'mariadb' or 'mariadb' in (server_version or '').lower():
        return ENGINE_MARIADB
    return ENGINE_MYSQL


_MYSQL_HEADER = re.compile(r'^--\s*Server version\s+(\S+)', re.M)
_MARIA_DUMP = re.compile(r'^--\s*MariaDB dump', re.M)
_PG_HEADER = re.compile(r'^--\s*Dumped from database version\s+(\d+)(?:\.(\d+))?', re.M)


def server_version_from_dump(header_text):
    """Server version text from the first lines of a mysqldump / pg_dump(all) file, or ''."""
    text = header_text or ''
    m = _MYSQL_HEADER.search(text)
    if m:
        version = m.group(1)
        if _MARIA_DUMP.search(text) and 'mariadb' not in version.lower():
            version += '-MariaDB'
        return version
    m = _PG_HEADER.search(text)
    if m:
        return 'PostgreSQL ' + m.group(1) + ('.' + m.group(2) if m.group(2) else '')
    return ''


def pg_major(version_text):
    m = re.search(r'(\d+)(?:\.(\d+))?', version_text or '')
    if not m:
        return None
    major = int(m.group(1))
    if major < 10 and m.group(2) is not None:
        return f'{major}.{m.group(2)}'  # 9.6
    return str(major)


def scratch_image(engine, version_text=''):
    """Image for the scratch server (same flavor and major[.minor] as production), or None if unknown."""
    if engine == ENGINE_POSTGRES:
        major = pg_major((version_text or '').replace('PostgreSQL', ''))
        return f'postgres:{major}' if major else None
    if not version_text:
        return None
    return pitr.scratch_server_image(version_text)


def walg_scratch_image(major):
    """PostgreSQL image for a WAL-G restore: glibc >= 2.27 so the v2.0.1 wal-g build runs in it."""
    return f'postgres:{major or 10}-bullseye'


# ---------------------------------------------------------------------------
# Sizes and the disk check
# ---------------------------------------------------------------------------

def estimate_restored_bytes(method, dump_size=None, binlog_bytes=0, base_uncompressed=None, cfg=None):
    """Rough size of the restored database (files: the snapshot size).

    A gzipped SQL dump grows ~5x when loaded (RESTORE_DUMP_EXPANSION); binlogs
    replayed add about their own size; a WAL-G base backup restores to its
    uncompressed size plus the WAL it replays.
    """
    cfg = cfg or settings()
    if method == 'files':
        return int(dump_size or 0)
    if method == 'walg':
        return int((base_uncompressed or 0) + (binlog_bytes or 0))
    return int((dump_size or 0) * cfg['dump_expansion'] + (binlog_bytes or 0))


def disk_check(path, free_bytes, restored_bytes, cfg=None):
    """{path, free_bytes, needed_bytes, ok} - needed = estimated restored size x RESTORE_DISK_FACTOR.

    (The E-Petition test needed ~9.9 GB of work space for a 4.3 GB database.)
    """
    cfg = cfg or settings()
    needed = int(restored_bytes * cfg['disk_factor'])
    return {'path': path, 'free_bytes': int(free_bytes), 'needed_bytes': needed,
            'ok': free_bytes is not None and free_bytes >= needed}


def disk_error(disk, restored_bytes, cfg=None):
    cfg = cfg or settings()
    return (f"Not enough free disk on the backupx server: this restore needs about "
            f"{fmt_bytes(disk['needed_bytes'])} (estimated restored size {fmt_bytes(restored_bytes)} "
            f"x {cfg['disk_factor']:g}), but {disk['path']} has {fmt_bytes(disk['free_bytes'])} free. "
            f"Free some space or remove old scratch restores first.")


def hashed_plan(plan):
    """The part of a plan the plan hash covers (free disk space changes from call to call)."""
    return {k: v for k, v in plan.items() if k != 'disk'}


# ---------------------------------------------------------------------------
# Restore points
# ---------------------------------------------------------------------------

def pitr_gaps(windows):
    """Unrestorable ranges between consecutive windows (oldest first)."""
    gaps = []
    ordered = [w for w in windows if w.get('to')]
    ordered.sort(key=lambda w: utc(w.get('from') or w['to']))
    for prev, nxt in zip(ordered, ordered[1:]):
        start = utc(prev['to'])
        end = utc(nxt.get('from') or nxt['to'])
        if end and start and end > start:
            gaps.append({'from': iso(start), 'to': iso(end),
                         'reason': nxt.get('reason') or 'no full backup in the new chain yet'})
    return gaps


def restorable_windows(windows):
    return [w for w in windows if w.get('anchored') and w.get('from') and w.get('to')]


def time_in_windows(target, windows):
    target = utc(target)
    return any(utc(w['from']) <= target <= utc(w['to']) for w in restorable_windows(windows))


def snapshot_size(snapshot):
    summary = (snapshot or {}).get('summary') or {}
    for key in ('total_bytes_processed', 'data_added'):
        if summary.get(key):
            return int(summary[key])
    return None


def is_pre_restore(snapshot):
    return PRE_RESTORE_TAG in ((snapshot or {}).get('tags') or [])


def dump_path(snapshot):
    """The .sql.gz path a database snapshot holds."""
    for p in (snapshot or {}).get('paths') or []:
        if p.endswith('.sql.gz'):
            return p
    paths = (snapshot or {}).get('paths') or []
    return paths[0] if paths else None


def latest_point(engine, snapshots, windows, pitr_enabled):
    """{point_type, time, snapshot_id, label} for "latest restorable"."""
    usable = restorable_windows(windows) if pitr_enabled else []
    if engine != ENGINE_FILES and usable:
        newest = max(usable, key=lambda w: utc(w['to']))
        t = utc(newest['to']).replace(microsecond=0)
        return {'point_type': POINT_TIME, 'time': iso(t), 'snapshot_id': None,
                'label': f'{fmt_time(t)} (point-in-time, newest shipped log)'}
    snaps = [s for s in snapshots or [] if not is_pre_restore(s)] or list(snapshots or [])
    if not snaps:
        return None
    newest = max(snaps, key=lambda s: s.get('time') or '')
    return {'point_type': POINT_SNAPSHOT, 'time': newest.get('time'), 'snapshot_id': newest.get('id'),
            'label': f"snapshot {newest.get('short_id') or (newest.get('id') or '')[:8]} "
                     f"from {fmt_time(newest.get('time'))}"}


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def plan_summary(plan):
    if plan.get('method') == 'files':
        d = plan.get('dump') or {}
        return f"snapshot {str(d.get('snapshot_id') or '')[:8]} from {fmt_time(d.get('time'))}, ~{fmt_bytes(d.get('size'))}"
    parts = []
    if plan.get('method') == 'walg':
        w = plan.get('wal') or {}
        parts.append(f"base backup {w.get('base_backup')} from {fmt_time(w.get('base_time'))}")
        parts.append('WAL replay to the target time')
    else:
        d = plan.get('dump') or {}
        parts.append(f"dump of {fmt_time(d.get('time'))}")
        b = plan.get('binlogs')
        if b:
            # 'files' already counts the open-file copy when 'partial' is set
            n = int(b.get('files') or 0)
            if b.get('partial') and n <= 1:
                parts.append('1 binlog file (open-file copy)')
            else:
                parts.append(f"{n} binlog file{'s' if n != 1 else ''}"
                             + (' incl. the open-file copy' if b.get('partial') else ''))
    return ' + '.join(parts) + f", ~{fmt_bytes(plan.get('download_bytes'))} to download"


def downtime_text(databases, restored_bytes, cfg=None):
    cfg = cfg or settings()
    seconds = max(30, restored_bytes / (cfg['import_rate'] * 1024 * 1024))
    minutes = max(1, round(seconds / 60))
    names = ', '.join(databases) if databases else 'the databases'
    return (f"{names} is dropped and re-imported: the application gets errors or missing data until the "
            f"import ends (~{minutes} min for ~{fmt_bytes(restored_bytes)}). Stop the application's writes first.")


def confirm_text(db_config):
    """The phrase to type before replacing production: the database name."""
    dbs = configured_databases(db_config)
    if len(dbs) == 1:
        return dbs[0]
    return (db_config or {}).get('name') or 'production'


def configured_databases(db_config):
    raw = ((db_config or {}).get('databases') or '*').strip()
    if raw in ('', '*'):
        return []
    return [d.strip() for d in raw.split(',') if d.strip()]


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

_IDENT = re.compile(r'^[A-Za-z0-9_$][A-Za-z0-9_$-]*$')


def normalize_key_tables(values, engine):
    """Validated key table names: "db.table" (MySQL), "db.table" or "db.schema.table" (PG)."""
    out, bad = [], []
    if isinstance(values, str):
        values = re.split(r'[\s,]+', values)
    for raw in values or []:
        name = (raw or '').strip()
        if not name:
            continue
        parts = name.split('.')
        max_parts = 3 if engine == ENGINE_POSTGRES else 2
        if not (2 <= len(parts) <= max_parts) or not all(_IDENT.match(p) for p in parts):
            bad.append(name)
            continue
        if engine == ENGINE_POSTGRES and len(parts) == 2:
            name = f'{parts[0]}.public.{parts[1]}'
        if name not in out:
            out.append(name)
    return out, bad


def normalize_verify_config(data, engine):
    """(config, errors) from a PUT body."""
    data = data or {}
    tables, bad = normalize_key_tables(data.get('key_tables') or [], engine)
    errors = [f'not a table name: {b}' for b in bad]
    check_sql = (data.get('check_sql') or '').strip()
    if len(check_sql) > 4000:
        errors.append('check SQL is longer than 4000 characters')
    if check_sql and not re.match(r'(?is)^\s*(select|with|show)\b', check_sql):
        errors.append('check SQL must be a read-only SELECT / WITH / SHOW statement')
    if ';' in check_sql.rstrip().rstrip(';'):
        errors.append('check SQL must be a single statement')
    health_url = (data.get('health_url') or '').strip()
    if health_url and not re.match(r'^https?://[^\s]+$', health_url):
        errors.append('health URL must start with http:// or https://')
    count_only, bad_co = normalize_key_tables(data.get('drill_count_only_tables') or [], engine)
    errors += [f'not a table name (drill count-only): {b}' for b in bad_co]
    return {'key_tables': tables[:50], 'check_sql': check_sql.rstrip().rstrip(';'),
            'health_url': health_url, 'drill_count_only_tables': count_only[:200]}, errors


def parse_marked_output(text):
    """{label: (rc, [lines])} from script output with BXSQL-BEGIN/END markers."""
    out = {}
    current, lines = None, []
    for raw in (text or '').splitlines():
        line = raw.rstrip('\r')
        if line.startswith('BXSQL-BEGIN '):
            current, lines = line[len('BXSQL-BEGIN '):].strip(), []
            continue
        if line.startswith('BXSQL-END ') and current is not None:
            rest = line[len('BXSQL-END '):].strip().rsplit(' ', 1)
            try:
                rc = int(rest[-1])
            except (ValueError, IndexError):
                rc = 1
            out[current] = (rc, lines)
            current, lines = None, []
            continue
        if current is not None:
            lines.append(line)
    return out


def parse_counts(lines):
    """{name: int} from "name<TAB>count" lines (estimates and exact counts)."""
    counts = {}
    for line in lines or []:
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        name = '.'.join(p for p in parts[:-1] if p)
        try:
            counts[name] = int(float(parts[-1] or 0))
        except ValueError:
            continue
    return counts


def compare(prod_est, restored_est, key_tables, prod_exact, restored_exact):
    """Rows for the Verify table, key tables first; match only judged on exact counts."""
    names = set(restored_est) | set(prod_est) | set(key_tables)
    rows = []
    for name in names:
        key = name in key_tables
        pe, re_ = prod_exact.get(name), restored_exact.get(name)
        match = (pe == re_) if (key and pe is not None and re_ is not None) else None
        rows.append({'name': name, 'prod_estimate': prod_est.get(name), 'restored_estimate': restored_est.get(name),
                     'prod_exact': pe, 'restored_exact': re_, 'key': key, 'match': match})
    rows.sort(key=lambda r: (not r['key'], r['name']))
    return rows


def key_mismatches(rows):
    """Key tables whose exact counts differ or couldn't be counted on one side."""
    return [r['name'] for r in rows if r['key'] and r['match'] is not True]


# ---------------------------------------------------------------------------
# File restores
# ---------------------------------------------------------------------------

def validate_restore_paths(paths, allowed_roots):
    """(clean paths, error). Paths must be absolute, inside the job's backed-up directories."""
    clean = []
    roots = [os.path.normpath(r) for r in allowed_roots or [] if r]
    for raw in paths or []:
        p = (raw or '').strip()
        if not p.startswith('/') or '..' in p.split('/'):
            return None, f'not an absolute path: {raw}'
        p = os.path.normpath(p)
        if p == '/':
            return None, 'restoring the whole filesystem root is not allowed'
        if roots and not any(p == r or p.startswith(r.rstrip('/') + '/') for r in roots):
            return None, f'{p} is outside the directories this job backs up'
        if p not in clean:
            clean.append(p)
    if not clean:
        return None, 'select at least one path'
    return clean, None


def default_alternate_path(job_id, now=None, root=None):
    now = now or utcnow()
    root = (root or '/var/tmp/backupx-restore').rstrip('/')
    return f"{root}/{job_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def cleanup_at(now=None, hours=None, cfg=None):
    cfg = cfg or settings()
    return (now or utcnow()) + timedelta(hours=hours or cfg['ttl_hours'])


def extend_until(current, now=None, hours=24, cfg=None):
    """New cleanup time: +hours from max(now, current), capped at now + RESTORE_MAX_KEEP_HOURS."""
    cfg = cfg or settings()
    now = now or utcnow()
    base = max(now, utc(current) or now)
    return min(base + timedelta(hours=max(1, int(hours or 24))), now + timedelta(hours=cfg['max_keep_hours']))
