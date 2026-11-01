"""
Restore drills: pure helpers (no I/O).

A drill proves a PITR job can really be restored: it checksums the job's tables
in production (read-only), restores the job into a scratch copy on the backupx
server to the time right after that, checksums the copy, and checksums
production again. A table that didn't change between the two production reads
("quiet") must be identical in the copy, or the drill fails. Tables written to
during the drill can't be compared exactly (they're reported, not judged).
"""

import os
import re

from . import restore as r


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def settings():
    return {
        'enabled': (os.environ.get('RESTORE_DRILL_ENABLED') or 'true').lower() in ('1', 'true', 'yes'),
        # crontab in the scheduler timezone (standard weekdays): Saturday 03:30, off-hours, before the
        # Sunday prune
        'cron': os.environ.get('RESTORE_DRILL_CRON') or '30 3 * * 6',
        # Tables estimated larger than this are compared by row count only (checksums read every row)
        'max_table_rows': _env_int('RESTORE_DRILL_MAX_TABLE_ROWS', 5_000_000),
        'max_tables': _env_int('RESTORE_DRILL_MAX_TABLES', 2000),
        # Minutes to wait for the binlogs/WAL covering the drill time to be shipped
        'ship_wait_minutes': _env_int('RESTORE_DRILL_SHIP_WAIT', 20),
    }


def _mysql_ident(name):
    return '`' + name.replace('`', '``') + '`'


def _pg_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"


def list_tables_sql(engine, databases):
    """One read-only query listing base tables: rows (db, table, estimated rows)."""
    if engine == r.ENGINE_POSTGRES:
        return ("SELECT current_database(), n.nspname || '.' || c.relname, GREATEST(c.reltuples, 0)::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') "
                "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                "ORDER BY 2")
    where = ', '.join(_lit(s) for s in r.MYSQL_SYSTEM_SCHEMAS)
    sql = ("SELECT table_schema, table_name, COALESCE(table_rows, 0) FROM information_schema.tables "
           f"WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ({where})")
    if databases:
        sql += ' AND table_schema IN (' + ', '.join(_lit(d) for d in databases) + ')'
    return sql + ' ORDER BY 1, 2'


def parse_tables(lines):
    out = []
    for line in lines or []:
        parts = line.split('\t')
        if len(parts) >= 3:
            try:
                out.append((parts[0], parts[1], int(float(parts[2] or 0))))
            except ValueError:
                continue
    return out


def checksum_statement(engine, table, big):
    """Read-only statement printing one line: '<checksum or count>'.

    MySQL/MariaDB: CHECKSUM TABLE (consistent read of every row; same server version on both
    sides, so values are comparable). PostgreSQL: md5 over the ordered md5 of each row's text.
    big: count only.
    """
    db, name = table
    if engine == r.ENGINE_POSTGRES:
        schema, rel = name.split('.', 1)
        ref = f'{_pg_ident(schema)}.{_pg_ident(rel)}'
        if big:
            return f"SELECT 'count:' || count(*) FROM {ref}"
        return (f"SELECT count(*) || ':' || coalesce(md5(string_agg(h, '' ORDER BY h)), '') "
                f"FROM (SELECT md5(t::text) AS h FROM {ref} t) s")
    ref = f'{_mysql_ident(db)}.{_mysql_ident(name)}'
    if big:
        return f"SELECT CONCAT('count:', COUNT(*)) FROM {ref}"
    return f'CHECKSUM TABLE {ref}'


def checksum_body(engine, tables, big_names):
    """Shell body (needs bxsql + RUN_SQL) printing marked results per table."""
    from . import restore_scripts as rs
    lines = [rs.RUN_SQL]
    for db, name, _rows in tables:
        key = f'{db}.{name}'
        sql = checksum_statement(engine, (db, name), key in big_names)
        target_db = db if engine == r.ENGINE_POSTGRES else ''
        lines.append(f'run_sql {rs.q("ck:" + key)} {rs.q(target_db)} {rs.q(sql)}')
    return '\n'.join(lines) + '\n'


def parse_checksums(marked):
    """{table: value or None (error)} from parse_marked_output()."""
    out = {}
    for label, (rc, lines) in marked.items():
        if not label.startswith('ck:'):
            continue
        key = label[3:]
        value = None
        if rc == 0:
            text = [x for x in lines if x.strip()]
            if text:
                last = text[-1].split('\t')
                value = last[-1].strip()  # CHECKSUM TABLE prints "db.table<TAB>checksum"
        out[key] = value
    return out


def count_only_keys(engine, tables, configured, max_table_rows):
    """Table keys compared by row count instead of a checksum.

    Estimated larger than max_table_rows, or listed in the job's drill_count_only_tables
    ("db.table"; PostgreSQL "db.schema.table") - for big append-only tables (activity logs)
    whose full checksum would read gigabytes from production every week.
    """
    wanted = set(configured or [])
    out = set()
    for db, name, rows in tables:
        key = f'{db}.{name}'
        if rows > max_table_rows or key in wanted:
            out.add(key)
    return out


def disk_skip_reason(plan, error):
    """The warning when a drill can't run for lack of scratch disk (a skip, not a failed drill)."""
    disk = (plan or {}).get('disk') or {}
    if error and disk and not disk.get('ok'):
        return error
    return None


def compare(before, after, restored):
    """Judge a drill. before/after: production checksums around the restore; restored: the copy.

    Returns {ok, compared, quiet, changed: [...], mismatches: [...], missing: [...], errors: [...]}.
    """
    quiet, changed, mismatches, missing, errors = [], [], [], [], []
    for table, b in sorted(before.items()):
        a = after.get(table)
        got = restored.get(table)
        if b is None or a is None:
            errors.append(table)
            continue
        if a != b:
            changed.append(table)
            continue
        quiet.append(table)
        if table not in restored:
            missing.append(table)
        elif got != b:
            mismatches.append({'table': table, 'production': b, 'restored': got})
    ok = not mismatches and not missing and not errors
    identical = len(quiet) - len(mismatches) - len(missing)
    return {'ok': ok, 'compared': len(quiet), 'identical': identical, 'quiet': len(quiet), 'changed': changed[:50],
            'mismatches': mismatches[:50], 'missing': missing[:50], 'errors': errors[:50],
            'tables': len(before)}


def summary(result):
    if result.get('skipped'):
        return f"skipped: {result.get('warning') or 'not run'}"
    if result.get('error'):
        return f"failed: {result['error']}"
    c = result.get('comparison') or {}
    text = (f"{c.get('identical', c.get('compared', 0))} of {c.get('tables', 0)} tables identical to production at "
            f"{result.get('target_time')}")
    if c.get('changed'):
        text += f"; {len(c['changed'])} changed during the drill (not compared)"
    if c.get('mismatches'):
        text += f"; {len(c['mismatches'])} DIFFER: " + ', '.join(m['table'] for m in c['mismatches'][:5])
    if c.get('missing'):
        text += f"; missing in the copy: {', '.join(c['missing'][:5])}"
    return text


_SAFE = re.compile(r'^\s*(SELECT|CHECKSUM\s+TABLE)\b', re.I)


def is_read_only(sql):
    return bool(_SAFE.match(sql))
