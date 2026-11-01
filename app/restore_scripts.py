"""
Shell for the restore wizard. Builds text only.

Two places run these scripts:

- *local*: inside the backupx container (`bash -s`, script on stdin). restic
  and wal-g run here; the docker CLI talks to the host daemon through the
  mounted socket, so scratch containers run on the backupx server. Files reach
  containers through `docker exec -i` / `docker cp` (tar on stdin), never bind
  mounts of container paths. Secrets (restic password, S3 keys) are in the
  process environment (subprocess env=), never in argv or the script.
- *prod*: on the job's server over SSH (main._ssh_popen, script on stdin). The
  database password is a 0600 option file / exported PGPASSWORD written by the
  script itself, as in the backup scripts; never argv, never `docker exec -e`.

Every script prints `BACKUPX-STEP n/m text` markers (task progress) and SQL
results between `BXSQL-BEGIN <label>` / `BXSQL-END <label> <rc>` markers
(parsed by restore.parse_marked_output).
"""

import shlex

from . import pitr_scripts
from . import restore as r

q = pitr_scripts.q

PREAMBLE = r'''set -uo pipefail
step() { echo "BACKUPX-STEP $1 $2"; }
die() { echo "ERROR: $*" >&2; exit 1; }
TAB="$(printf '\t')"
'''

RUN_SQL = r'''
run_sql() {  # label db sql: prints the rows between markers, never aborts the script
    echo "BXSQL-BEGIN $1"
    printf '%s\n' "$3" | bxsql "$2" 2>&1
    echo "BXSQL-END $1 $?"
}
'''


def docker_args(restore_id, job_id, network=None, labels_only=False):
    """Labels (+ network) for `docker run/create` of a scratch container. No secrets."""
    parts = [f'--label {q(r.LABEL + "=" + restore_id)}', f'--label {q(r.LABEL_JOB + "=" + str(job_id))}']
    if not labels_only:
        parts.append(f'--network {q(network)}' if network else '--network none')
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# Scratch database: MySQL / MariaDB
# ---------------------------------------------------------------------------

def mysql_flavor_bits(flavor):
    is_maria = flavor == r.ENGINE_MARIADB
    return {
        'client': 'mariadb' if is_maria else 'mysql',
        'admin': 'mariadb-admin' if is_maria else 'mysqladmin',
        'dump': 'mariadb-dump' if is_maria else 'mysqldump',
        'root_env': '-e MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1' if is_maria else '-e MYSQL_ALLOW_EMPTY_PASSWORD=yes',
        # GTID on so a dump's SET @@GLOBAL.GTID_PURGED loads; replays go by position
        'server_args': '' if is_maria else '--gtid-mode=ON --enforce-gtid-consistency=ON',
    }


def mysql_start(flavor, tries=300):
    """Start $SERVER_IMAGE as $NAME with $DOCKER_ARGS and wait until it accepts queries."""
    b = mysql_flavor_bits(flavor)
    return f'''
docker run -d --name "$NAME" $DOCKER_ARGS {b['root_env']} -e TZ=UTC "$SERVER_IMAGE" {b['server_args']} >/dev/null \\
    || die "could not start $SERVER_IMAGE (image pull or name clash?)"
ready=0
for i in $(seq 1 {tries}); do
    # The entrypoint restarts the server once after initialising; wait for the final one
    if docker exec "$NAME" sh -c 'test -f /var/lib/mysql/*.pid 2>/dev/null || ls /var/run/mysqld/*.pid >/dev/null 2>&1' \\
       && docker exec "$NAME" {b['admin']} ping -uroot --silent >/dev/null 2>&1 \\
       && ! docker logs "$NAME" 2>&1 | tail -n 5 | grep -q 'Temporary server'; then
        sleep 3
        if docker exec "$NAME" {b['client']} -uroot -e 'SELECT 1' >/dev/null 2>&1; then ready=1; break; fi
    fi
    if [ "$(docker inspect -f '{{{{.State.Running}}}}' "$NAME" 2>/dev/null)" = false ]; then
        docker logs --tail 30 "$NAME" >&2; die "the scratch server exited"
    fi
    sleep 2
done
[ "$ready" = 1 ] || {{ docker logs --tail 30 "$NAME" >&2; die "scratch server did not start"; }}
'''


def mysql_scratch_load(name, image, flavor, dump_file, d_args, n=2, m=3):
    """Start the scratch server and load a gzipped dump (already restored into the work dir)."""
    b = mysql_flavor_bits(flavor)
    return PREAMBLE + f'''
set -e
NAME={q(name)}
SERVER_IMAGE={q(image)}
DOCKER_ARGS={q(d_args)}
DUMP={q(dump_file)}
step {n}/{m} "Starting a scratch $SERVER_IMAGE container ($NAME, no published ports)"
{mysql_start(flavor)}
step {n + 1}/{m} "Loading the dump"
gunzip -c "$DUMP" | docker exec -i "$NAME" {b['client']} -uroot
echo "Loaded into $NAME"
'''


# ---------------------------------------------------------------------------
# Scratch database: PostgreSQL
# ---------------------------------------------------------------------------

PG_WAIT = r'''
ready=0
for i in $(seq 1 300); do
    if docker logs "$NAME" 2>&1 | grep -q "$READY_LOG" && docker exec "$NAME" pg_isready -q -U "$SU" >/dev/null 2>&1 \
       && docker exec "$NAME" psql -X -U "$SU" -d postgres -tAc 'select 1' >/dev/null 2>&1; then ready=1; break; fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = false ]; then
        docker logs --tail 30 "$NAME" >&2; die "the scratch server exited"
    fi
    sleep 2
done
[ "$ready" = 1 ] || { docker logs --tail 30 "$NAME" >&2; die "scratch server did not start"; }
'''


def pg_scratch_load(name, image, dump_file, mode, databases, work, d_args, n=2, m=3):
    """Start postgres:<major> and load a gzipped pg_dumpall / pg_dump file.

    mode: cluster (pg_dumpall, or globals + per-db --create dumps), single (one
    pg_dump without CREATE DATABASE), multi (pg_dumps joined by "-- NEXT DB --",
    in the order of `databases`).
    """
    dbs = ' '.join(q(d) for d in databases)
    return PREAMBLE + f'''
set -e
NAME={q(name)}
SERVER_IMAGE={q(image)}
DOCKER_ARGS={q(d_args)}
DUMP={q(dump_file)}
WORK={q(work)}
SU=postgres
READY_LOG='PostgreSQL init process complete'
MODE={q(mode)}
DBS=({dbs})
step {n}/{m} "Starting a scratch $SERVER_IMAGE container ($NAME, no published ports)"
docker run -d --name "$NAME" $DOCKER_ARGS -e POSTGRES_HOST_AUTH_METHOD=trust -e TZ=UTC "$SERVER_IMAGE" >/dev/null \\
    || die "could not start $SERVER_IMAGE"
{PG_WAIT}
step {n + 1}/{m} "Loading the dump"
load() {{ docker exec -i "$NAME" psql -X -q -U "$SU" -d "$1" -v ON_ERROR_STOP=0 >/dev/null 2>>"$WORK/load.err"; }}
: > "$WORK/load.err"
case "$MODE" in
    cluster) gunzip -c "$DUMP" | load postgres ;;
    single)  docker exec "$NAME" createdb -U "$SU" "${{DBS[0]}}"; gunzip -c "$DUMP" | load "${{DBS[0]}}" ;;
    multi)
        mkdir -p "$WORK/parts"
        gunzip -c "$DUMP" | awk -v d="$WORK/parts" 'BEGIN{{n=1}} /^-- NEXT DB --$/{{n++; next}} {{print > (d "/" n ".sql")}}'
        i=1
        for db in "${{DBS[@]}}"; do
            docker exec "$NAME" createdb -U "$SU" "$db"
            [ -f "$WORK/parts/$i.sql" ] && load "$db" < "$WORK/parts/$i.sql"
            i=$((i + 1))
        done ;;
esac
ERRS=$(grep -c 'ERROR' "$WORK/load.err" || true)
if [ "${{ERRS:-0}}" -gt 0 ]; then
    echo "psql reported $ERRS error(s) while loading (\\"already exists\\" for roles is expected):"
    grep 'ERROR' "$WORK/load.err" | head -n 20
fi
echo "Loaded into $NAME"
'''


WALG_FETCH_SH = '''#!/bin/sh
# backupx scratch restore: restore_command helper (scratch container only)
D=$(dirname "$0")
set -a
. "$D/env"
set +a
exec "$D/wal-g" wal-fetch "$1" "$2"
'''


def pg_walg_start(name, d_args_create, n, m):
    """Create the scratch container, copy the prepared data directory in, start it, wait for promotion."""
    return PREAMBLE + f'''
set -e
NAME={q(name)}
step {n}/{m} "Starting the scratch server (WAL replay to the target time)"
docker create --name "$NAME" {d_args_create} -e TZ=UTC "$SERVER_IMAGE" >/dev/null || die "could not create the container"
docker cp "$PGDATA_SRC/." "$NAME:/var/lib/postgresql/data" || die "could not copy the data directory into $NAME"
docker start "$NAME" >/dev/null
step {n + 1}/{m} "Replaying WAL"
last=""
for i in $(seq 1 "$WAIT_TRIES"); do
    if [ "$(docker inspect -f '{{{{.State.Running}}}}' "$NAME" 2>/dev/null)" = false ]; then
        docker logs --tail 40 "$NAME" >&2; die "the scratch server exited during recovery"
    fi
    line=$(docker logs "$NAME" 2>&1 | grep -E 'restored log file|recovery stopping|last completed transaction|database system is ready' | tail -n1) || true
    [ -n "$line" ] && [ "$line" != "$last" ] && echo "$line" && last="$line"
    for su in postgres "$ALT_SU"; do
        [ -n "$su" ] || continue
        if [ "$(docker exec "$NAME" psql -X -U "$su" -d postgres -tAc 'select pg_is_in_recovery()' 2>/dev/null)" = f ]; then
            echo "BACKUPX-SUPERUSER $su"
            docker logs "$NAME" 2>&1 | grep -E 'recovery stopping|last completed transaction|recovery target' | tail -n 3 || true
            exit 0
        fi
    done
    sleep 3
done
docker logs --tail 40 "$NAME" >&2
die "recovery did not finish in time"
'''


# ---------------------------------------------------------------------------
# SQL runners (scratch container / production)
# ---------------------------------------------------------------------------

def scratch_runner(engine, name, superuser='postgres'):
    if engine == r.ENGINE_POSTGRES:
        return f'''NAME={q(name)}
bxsql() {{ docker exec -i "$NAME" psql -X -U {q(superuser)} -d "${{1:-postgres}}" -tA -F "$TAB" -v ON_ERROR_STOP=1; }}
'''
    client = mysql_flavor_bits(engine)['client']
    return f'''NAME={q(name)}
bxsql() {{ docker exec -i "$NAME" {client} -uroot -N -B; }}
'''


def _docker_detect():
    return 'if docker ps >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo -n docker"; fi\n'


def prod_runner(db_config, engine, client_image=''):
    """Shell defining bxsql (query), bximport (stdin -> database) and bxdump (dbs -> stdout)
    for the job's own connection path. Creates $BXD (0700 temp dir, removed on exit)."""
    container = (db_config.get('docker_container') or '').strip()
    user = db_config.get('username') or ''
    host = db_config.get('host') or 'localhost'
    head = 'BXD=$(mktemp -d) || exit 1\nchmod 700 "$BXD"\n'
    if engine == r.ENGINE_POSTGRES:
        port = int(db_config.get('port') or 5432)
        if container:
            c = q(container)
            return head + _docker_detect() + f'''trap 'rm -rf "$BXD"' EXIT
PG_DB=$($DOCKER exec {c} printenv POSTGRES_DB 2>/dev/null); [ -n "$PG_DB" ] || PG_DB=postgres
PG_USER=""
for u in {q(user)} "$($DOCKER exec {c} printenv POSTGRES_USER 2>/dev/null)" postgres; do
    [ -n "$u" ] || continue
    if [ "$($DOCKER exec {c} psql -X -U "$u" -d "$PG_DB" -tAc "select rolsuper from pg_roles where rolname = current_user" 2>/dev/null)" = t ]; then
        PG_USER="$u"; break
    fi
done
[ -n "$PG_USER" ] || die "no PostgreSQL superuser usable in container {container}"
bxsql() {{ $DOCKER exec -i {c} psql -X -U "$PG_USER" -d "${{1:-$PG_DB}}" -tA -F "$TAB" -v ON_ERROR_STOP=1; }}
bximport() {{ $DOCKER exec -i {c} psql -X -q -U "$PG_USER" -d "$1" -v ON_ERROR_STOP=1 >/dev/null; }}
bxdump() {{ for db in "$@"; do $DOCKER exec {c} pg_dump -U "$PG_USER" --clean --if-exists --create -d "$db" || return 1; done; }}
'''
        password = db_config.get('password') or ''
        conn = f'-h {q(host)} -p {port} -U {q(user)}'
        return head + f'''trap 'rm -rf "$BXD"' EXIT
export PGPASSWORD={q(password)}
PG_DB=postgres
bxsql() {{ psql -X {conn} -d "${{1:-postgres}}" -tA -F "$TAB" -v ON_ERROR_STOP=1; }}
bximport() {{ psql -X -q {conn} -d "$1" -v ON_ERROR_STOP=1 >/dev/null; }}
bxdump() {{ for db in "$@"; do pg_dump {conn} --clean --if-exists --create -d "$db" || return 1; done; }}
'''
    # MySQL / MariaDB
    port = int(db_config.get('port') or 3306)
    init = '--init-command="SET SESSION lock_wait_timeout=120"'
    dump_flags = '--single-transaction --routines --triggers --events --add-drop-database --hex-blob'
    if container:
        c = q(container)
        cnf = pitr_scripts.mysql_cnf(db_config, docker_mode=True)
        return head + _docker_detect() + f'''CNF_ARG=""
cleanup() {{ [ -n "$CNF_ARG" ] && $DOCKER exec {c} rm -f /tmp/.backupx-restore.cnf; rm -rf "$BXD"; }}
trap cleanup EXIT
MYSQL=$($DOCKER exec {c} sh -c 'command -v mysql || command -v mariadb' 2>/dev/null | tail -n1)
MYSQLDUMP=$($DOCKER exec {c} sh -c 'command -v mysqldump || command -v mariadb-dump' 2>/dev/null | tail -n1)
[ -n "$MYSQL" ] || die "no mysql client in container {container}"
if ! printf 'SELECT 1' | $DOCKER exec -i {c} "$MYSQL" -N -B >/dev/null 2>&1; then
    # Same login the backups use when the socket login needs credentials: option file on stdin
    printf '%s' {q(cnf)} | $DOCKER exec -i {c} sh -c 'umask 077; cat > /tmp/.backupx-restore.cnf' || die "can't pass the credentials"
    CNF_ARG="--defaults-extra-file=/tmp/.backupx-restore.cnf"
fi
GTID_FLAG=$($DOCKER exec {c} "$MYSQLDUMP" --help 2>/dev/null | grep -q -- '--set-gtid-purged' && echo --set-gtid-purged=OFF)
bxsql() {{ $DOCKER exec -i {c} "$MYSQL" $CNF_ARG -N -B; }}
bximport() {{ $DOCKER exec -i {c} "$MYSQL" $CNF_ARG {init}; }}
bxdump() {{ $DOCKER exec {c} "$MYSQLDUMP" $CNF_ARG --databases "$@" {dump_flags} $GTID_FLAG; }}
'''
    cnf = pitr_scripts.mysql_cnf(db_config, docker_mode=False)
    write = f"( umask 077; printf '%s' {q(cnf)} > \"$BXD/my.cnf\" ) || die \"can't write the option file\"\n"
    if client_image:
        img = q(client_image)
        return head + 'trap \'rm -rf "$BXD"\' EXIT\n' + write + _docker_detect().replace('docker ps', 'docker info') + f'''
# The option file goes in on stdin ahead of the data (up to a marker line), so neither
# the password nor a host path is in docker's argv and it works with any docker daemon
run_client() {{ prog=$1; shift; {{ cat "$BXD/my.cnf"; printf '\\n__BACKUPX_CNF_END__\\n'; cat; }} | \\
    $DOCKER run --rm -i -u 0:0 --network host --read-only --tmpfs /tmp:rw,mode=1777,size=64m --cap-drop ALL \\
        --security-opt no-new-privileges --entrypoint sh {img} -c '
    umask 077; while IFS= read -r l; do [ "$l" = __BACKUPX_CNF_END__ ] && break; printf "%s\\n" "$l"; done > /tmp/my.cnf
    p=$1; shift; exec "$p" --defaults-extra-file=/tmp/my.cnf "$@"' sh "$prog" "$@"; }}
GTID_FLAG=$($DOCKER run --rm --entrypoint mysqldump {img} --help 2>/dev/null | grep -q -- '--set-gtid-purged' && echo --set-gtid-purged=OFF)
bxsql() {{ run_client mysql -N -B; }}
bximport() {{ run_client mysql {init}; }}
bxdump() {{ run_client mysqldump --databases "$@" {dump_flags} $GTID_FLAG; }}
'''
    return head + 'trap \'rm -rf "$BXD"\' EXIT\n' + write + f'''MYSQL=$(command -v mysql || command -v mariadb)
MYSQLDUMP=$(command -v mysqldump || command -v mariadb-dump)
[ -n "$MYSQL" ] || die "no mysql client on this server"
GTID_FLAG=$("$MYSQLDUMP" --help 2>/dev/null | grep -q -- '--set-gtid-purged' && echo --set-gtid-purged=OFF)
bxsql() {{ "$MYSQL" --defaults-extra-file="$BXD/my.cnf" -N -B; }}
bximport() {{ "$MYSQL" --defaults-extra-file="$BXD/my.cnf" {init}; }}
bxdump() {{ "$MYSQLDUMP" --defaults-extra-file="$BXD/my.cnf" --databases "$@" {dump_flags} $GTID_FLAG; }}
'''


def mysql_connect_args(db_config):
    """-h/-P/-u for direct MySQL connections (appended to bxsql etc. by prod_runner users)."""
    host = db_config.get('host') or 'localhost'
    if host == 'localhost':
        host = '127.0.0.1'
    return f"-h {q(host)} -P {int(db_config.get('port') or 3306)} -u {q(db_config.get('username') or '')}"


# ---------------------------------------------------------------------------
# Verification queries
# ---------------------------------------------------------------------------

def _sql_lit(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _mysql_ident(name):
    return '`' + name.replace('`', '``') + '`'


def _pg_ident(name):
    return '"' + name.replace('"', '""') + '"'


def verify_body(engine, databases, key_tables, check_sql='', analyze=False):
    """Statements printing estimates (est), exact counts of key tables (exact:<name>) and the check (check).

    databases: the databases to compare ([] = all non-system ones).
    """
    lines = [RUN_SQL]
    if engine == r.ENGINE_POSTGRES:
        if databases:
            lines.append('DBS="' + ' '.join(databases) + '"')
        else:
            lines.append('DBS=$(printf "%s\\n" "SELECT datname FROM pg_database WHERE datallowconn '
                         'AND NOT datistemplate ORDER BY 1" | bxsql "" 2>/dev/null)')
        est = ("SELECT current_database(), n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint FROM pg_class c "
               "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN "
               "('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'")
        if analyze:
            est = 'ANALYZE; ' + est
        lines.append(f'for db in $DBS; do run_sql "est:$db" "$db" {q(est)}; done')
        for name in key_tables:
            db, schema, table = name.split('.', 2)
            sql = f'SELECT COUNT(*) FROM {_pg_ident(schema)}.{_pg_ident(table)}'
            lines.append(f'run_sql {q("exact:" + name)} {q(db)} {q(sql)}')
        if check_sql:
            first = databases[0] if databases else ''
            lines.append(f'run_sql check {q(first)} {q(check_sql)}')
        return '\n'.join(lines) + '\n'
    where = ', '.join(_sql_lit(s) for s in r.MYSQL_SYSTEM_SCHEMAS)
    est = ("SELECT table_schema, table_name, COALESCE(table_rows, 0) FROM information_schema.tables "
           f"WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ({where})")
    if databases:
        est += ' AND table_schema IN (' + ', '.join(_sql_lit(d) for d in databases) + ')'
    if engine == r.ENGINE_MYSQL:
        # MySQL 8 caches information_schema statistics for a day by default
        est = 'SET SESSION information_schema_stats_expiry = 0; ' + est
    lines.append(f'run_sql est "" {q(est)}')
    lines.append(f'run_sql dbs "" {q("SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN (" + where + ")")}')
    for name in key_tables:
        db, table = name.split('.', 1)
        lines.append(f'run_sql {q("exact:" + name)} "" {q(f"SELECT COUNT(*) FROM {_mysql_ident(db)}.{_mysql_ident(table)}")}')
    if check_sql:
        lines.append(f'run_sql check "" {q(check_sql)}')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Replace production
# ---------------------------------------------------------------------------

STRIP_DEFINERS = r"sed -E 's/DEFINER=`[^`]*`@`[^`]*`//g'"


def scratch_dump(engine, name, databases, out_file, superuser='postgres', strip_definers=False):
    """Dump the restored databases from the scratch container, importable into production.

    MySQL: --databases with --add-drop-database (a real point-in-time
    replacement), routines/triggers/events, --set-gtid-purged=OFF (a managed
    GTID server keeps its own GTID history); DEFINER clauses stripped for
    direct/managed targets (the importing user may not have SET_USER_ID).
    PostgreSQL: pg_dump --clean --if-exists --create per database.
    """
    dbs = ' '.join(q(d) for d in databases)
    if engine == r.ENGINE_POSTGRES:
        body = f'''for db in {dbs}; do docker exec "$NAME" pg_dump -U {q(superuser)} --clean --if-exists --create -d "$db" || exit 1; done'''
    else:
        b = mysql_flavor_bits(engine)
        gtid = '' if engine == r.ENGINE_MARIADB else ' --set-gtid-purged=OFF'
        body = (f'docker exec "$NAME" {b["dump"]} -uroot --databases {dbs} --single-transaction --routines '
                f'--triggers --events --add-drop-database --hex-blob{gtid}')
        if strip_definers:
            body += f' | {STRIP_DEFINERS}'
    return PREAMBLE + f'''
NAME={q(name)}
OUT={q(out_file)}
( {body} ) | gzip > "$OUT"
RC=$?
[ $RC -eq 0 ] || die "dumping the scratch database failed"
[ -n "$(gzip -dc "$OUT" 2>/dev/null | head -c 1)" ] || die "the scratch dump is empty"
echo "Scratch dump: $(du -h "$OUT" | cut -f1)"
'''


UPLOAD_SHELL = ('umask 077; d=$(mktemp -d "${TMPDIR:-/tmp}/backupx-restore.XXXXXX") && '
                'cat > "$d/restore.sql.gz" && echo "$d"')


def replace_script(runner, engine, databases, remote_dir, n, m):
    """On the job's server: rollback copy of production, then import the scratch dump.

    Import failure -> the rollback copy is imported back (prints
    BACKUPX-ROLLBACK-OK / -FAILED). The directory stays for a rollback after
    verification; cleanup_remote_script removes it.
    """
    dbs = ' '.join(q(d) for d in databases)
    terminate = ''
    target_db = '""'
    if engine == r.ENGINE_POSTGRES:
        names = ', '.join(_sql_lit(d) for d in databases)
        terminate = (f'''printf '%s\\n' {q(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ({names}) AND pid <> pg_backend_pid()")} '''
                     '| bxsql "$MAINT" >/dev/null 2>&1 || true')
        target_db = '"$MAINT"'
    return PREAMBLE + runner + f'''
D={q(remote_dir)}
[ -f "$D/restore.sql.gz" ] || die "the uploaded dump is missing"
MAINT=postgres
{'printf "SELECT 1" | bxsql postgres >/dev/null 2>&1 || MAINT=template1' if engine == r.ENGINE_POSTGRES else ''}
step {n}/{m} "Saving a rollback copy of production on the server"
bxdump {dbs} | gzip > "$D/rollback.sql.gz"
[ "${{PIPESTATUS[0]}}" -eq 0 ] || die "could not dump production for the rollback copy; nothing was changed"
[ -n "$(gzip -dc "$D/rollback.sql.gz" 2>/dev/null | head -c 1)" ] || die "the rollback copy is empty; nothing was changed"
echo "Rollback copy: $(du -h "$D/rollback.sql.gz" | cut -f1)"
step {n + 1}/{m} "Importing the restored data into production"
{terminate}
gunzip -c "$D/restore.sql.gz" | bximport {target_db}
RC=$?
if [ $RC -ne 0 ]; then
    echo "BACKUPX-IMPORT-FAILED $RC"
    {terminate}
    if gunzip -c "$D/rollback.sql.gz" | bximport {target_db}; then echo "BACKUPX-ROLLBACK-OK"; else echo "BACKUPX-ROLLBACK-FAILED"; fi
    exit 3
fi
echo "BACKUPX-IMPORT-OK"
'''


def rollback_script(runner, engine, databases, remote_dir):
    terminate = ''
    target_db = '""'
    if engine == r.ENGINE_POSTGRES:
        names = ', '.join(_sql_lit(d) for d in databases)
        terminate = (f'''printf '%s\\n' {q(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ({names}) AND pid <> pg_backend_pid()")} '''
                     '| bxsql "$MAINT" >/dev/null 2>&1 || true')
        target_db = '"$MAINT"'
    return PREAMBLE + runner + f'''
D={q(remote_dir)}
MAINT=postgres
{'printf "SELECT 1" | bxsql postgres >/dev/null 2>&1 || MAINT=template1' if engine == r.ENGINE_POSTGRES else ''}
echo "Importing the rollback copy back into production"
{terminate}
if gunzip -c "$D/rollback.sql.gz" | bximport {target_db}; then echo "BACKUPX-ROLLBACK-OK"; else echo "BACKUPX-ROLLBACK-FAILED"; exit 1; fi
'''


def cleanup_remote_script(remote_dir):
    return f'''D={q(remote_dir)}
case "$D" in */backupx-restore.*) rm -rf "$D" ;; esac
'''


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def files_restore_script(job, target, repo, snapshot_id, paths, target_path, pre_restore, restore_id):
    """On the job's server: optional pre-restore backup of the paths that exist, then restic restore."""
    insecure = ' --insecure-tls' if target.get('skip_ssl_verify') else ''
    includes = ' '.join(f'--include {q(p)}' for p in paths)
    plist = ' '.join(q(p) for p in paths)
    total = 3 if pre_restore else 2
    pre = ''
    if pre_restore:
        pre = f'''
step 1/{total} "Pre-restore backup of the current files"
EXISTING=()
for p in {plist}; do [ -e "$p" ] && EXISTING+=("$p"); done
if [ ${{#EXISTING[@]}} -gt 0 ]; then
    OUT=$(restic backup --json --tag {r.PRE_RESTORE_TAG} --tag {q('restore:' + restore_id)} "${{EXISTING[@]}}"{insecure} 2>&1) || {{
        printf '%s\\n' "$OUT" | tail -n 5; die "the pre-restore backup failed; nothing was restored"; }}
    SNAP=$(printf '%s\\n' "$OUT" | grep '"message_type":"summary"' | sed -n 's/.*"snapshot_id":"\\([0-9a-f]*\\)".*/\\1/p' | tail -n1)
    [ -n "$SNAP" ] || die "the pre-restore backup did not report a snapshot; nothing was restored"
    echo "BACKUPX-PRE-RESTORE-SNAPSHOT $SNAP"
else
    echo "None of the paths exist yet; no pre-restore backup needed"
fi
'''
    n = 2 if pre_restore else 1
    return PREAMBLE + f'''
export PATH="$HOME/.local/bin:$PATH"
export AWS_ACCESS_KEY_ID={q(target.get('access_key') or '')}
export AWS_SECRET_ACCESS_KEY={q(target.get('secret_key') or '')}
export RESTIC_PASSWORD={q(job['restic_password'])}
export RESTIC_REPOSITORY={q(repo)}
{pre}
step {n}/{total} "Preparing {target_path}"
mkdir -p {q(target_path)} || die "can't create {target_path}"
step {n + 1}/{total} "Restoring {len(paths)} path(s)"
restic restore {q(snapshot_id)} --target {q(target_path)} {includes}{insecure} 2>&1
'''


def files_to_host_script(src_dir, host_dir, helper_image):
    """Copy a local extract to a directory on the backupx server's own filesystem (helper container)."""
    parent, name = host_dir.rsplit('/', 1)
    return PREAMBLE + f'''
set -e
tar -C {q(src_dir)} -cf - . | docker run --rm -i --network none -v {q(parent + ':/out')} {q(helper_image)} \\
    sh -c {q('mkdir -p /out/' + name + ' && tar -C /out/' + name + ' -xf -')}
echo "Extracted to {host_dir} on the backupx server"
'''


def remove_host_dir_script(host_dir, helper_image):
    parent, name = host_dir.rsplit('/', 1)
    return PREAMBLE + f'''
docker run --rm --network none -v {q(parent + ':/out')} {q(helper_image)} rm -rf {q('/out/' + name)}
'''


def remove_containers_script(restore_id=None):
    """docker rm -f the scratch containers of one restore (or none if restore_id is empty)."""
    return PREAMBLE + f'''
IDS=$(docker ps -aq --filter {q('label=' + r.LABEL + '=' + (restore_id or ''))})
if [ -n "$IDS" ]; then docker rm -f -v $IDS >/dev/null && echo "Removed container(s): $IDS"; else echo "No container left"; fi
'''


LIST_CONTAINERS = ('docker ps -a --filter label=' + r.LABEL +
                   ' --format \'{{.Label "' + r.LABEL + '"}}\t{{.Names}}\t{{.Status}}\'')


def shell_words(items):
    return ' '.join(shlex.quote(str(i)) for i in items)
