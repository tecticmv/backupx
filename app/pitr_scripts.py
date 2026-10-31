"""
Shell for point-in-time recovery: the binlog streamer installed on target
hosts, the scripts backupx sends over SSH, and the downloadable restore script.

Everything here only builds text. Secrets (DB password, restic password, S3
keys) are put into scripts that travel over ssh's stdin (see main._ssh_run),
never into any argv: inside the scripts they are shell variables, exported
environment variables, or 0600 files written with the printf builtin.
"""

import shlex
from datetime import timezone

from . import pitr

STREAMER_VERSION = '1'
STREAMER_PATH = '/usr/local/lib/backupx-pitr/binlog-streamer.sh'
UNIT_NAME = 'backupx-pitr-binlog@.service'
UNIT_PATH = '/etc/systemd/system/' + UNIT_NAME
CONF_ROOT = '/etc/backupx-pitr'
STATE_ROOT = '/var/lib/backupx-pitr'


def unit_instance(job_id):
    return f'backupx-pitr-binlog@{job_id}.service'


def q(value):
    return shlex.quote('' if value is None else str(value))


# Shared by the streamer and the prerequisite check: run the MySQL/MariaDB
# client programs either from this host or, when a container or client image is
# configured, from that image (same version as the server; MySQL and MariaDB
# binlog clients are not interchangeable). Expects CONF_DIR (holding my.cnf),
# optional SPOOL, PITR_CONTAINER, PITR_IMAGE.
CLIENT_RUNNER = r'''
pitr_setup_client() {
    DOCKER=docker
    PITR_RUN_IMAGE=""
    PITR_NET=host
    if [ -n "${PITR_CONTAINER:-}" ] || [ -n "${PITR_IMAGE:-}" ]; then
        if ! docker info >/dev/null 2>&1; then DOCKER="sudo -n docker"; fi
    fi
    if [ -n "${PITR_CONTAINER:-}" ]; then
        PITR_RUN_IMAGE=$($DOCKER inspect -f '{{.Config.Image}}' "$PITR_CONTAINER" 2>/dev/null) || {
            echo "container $PITR_CONTAINER not found" >&2; return 1; }
        PITR_NET="container:$PITR_CONTAINER"
    elif [ -n "${PITR_IMAGE:-}" ]; then
        PITR_RUN_IMAGE="$PITR_IMAGE"
    fi
    if [ -n "$PITR_RUN_IMAGE" ]; then
        CNF=/tmp/backupx-my.cnf
        OUT_DIR=/spool
    else
        CNF="$CONF_DIR/my.cnf"
        OUT_DIR="${SPOOL:-}"
    fi
}

# pitr_client [--name NAME] PROGRAM ARGS...
# In a container the option file arrives on stdin (written to /tmp inside it), so
# neither the password nor a host path for it is needed; only the spool is mounted.
pitr_client() {
    local name=""
    if [ "${1:-}" = "--name" ]; then name="$2"; shift 2; fi
    if [ -n "$PITR_RUN_IMAGE" ]; then
        $DOCKER run --rm -i --init -u 0:0 --network "$PITR_NET" --entrypoint sh \
            ${SPOOL:+-v "$SPOOL:/spool"} ${name:+--name "$name"} "$PITR_RUN_IMAGE" \
            -c 'umask 077; cat > /tmp/backupx-my.cnf && exec "$@"' sh "$@" < "$CONF_DIR/my.cnf"
    else
        "$@"
    fi
}

pitr_find_programs() {
    BINLOG=$(pitr_client sh -c 'command -v mariadb-binlog || command -v mysqlbinlog' 2>/dev/null | tail -n1)
    MYSQL=$(pitr_client sh -c 'command -v mariadb || command -v mysql' 2>/dev/null | tail -n1)
    [ -n "$BINLOG" ] && [ -n "$MYSQL" ]
}

pitr_q() {
    pitr_client "$MYSQL" --defaults-extra-file="$CNF" -N -B -e "$1"
}
'''

STREAMER_SCRIPT = r'''#!/bin/bash
# backupx point-in-time recovery: binlog streamer (version @VERSION@).
# Managed by backupx - changes here are overwritten.
#
# Keeps `mysqlbinlog --read-from-remote-server --raw --stop-never` connected and
# writes byte-exact binlog copies into the spool; backupx ships closed files.
# Resumes from the newest spool file, or the file after the last shipped one;
# anything that can't continue the sequence is logged to $STATE/events as a gap.
set -u
JOB="${1:?usage: binlog-streamer.sh JOB_ID}"
CONF_DIR="/etc/backupx-pitr/$JOB"
STATE="/var/lib/backupx-pitr/$JOB"
SPOOL="$STATE/spool"
. "$CONF_DIR/env"
umask 007  # the SSH user's group ships and deletes the files
CONTAINER_NAME="backupx-pitr-$JOB"

log() { echo "$*" >&2; }
event() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$STATE/events"; log "$*"; }
@CLIENT_RUNNER@
seq_of() { printf '%s' "$1" | sed -n 's/.*\.0*\([0-9][0-9]*\)$/\1/p'; }
newest_spool() { ls -1 "$SPOOL" 2>/dev/null | grep -E '\.[0-9]{6,}$' | sort -t. -k2,2n | tail -n1; }

pitr_setup_client || exit 1
pitr_find_programs || { log "mysqlbinlog/mysql client not found"; exit 1; }

UUID=$(pitr_q "SELECT @@server_uuid" 2>/dev/null)
[ -n "$UUID" ] || UUID=$(pitr_q "SELECT CONCAT('server-id-', @@server_id)") || { log "can't query the server"; exit 1; }
pitr_q "SELECT VERSION()" > "$STATE/server_version.new" 2>/dev/null && mv -f "$STATE/server_version.new" "$STATE/server_version"
LOGS=$(pitr_q "SHOW BINARY LOGS" | cut -f1,2) || { log "SHOW BINARY LOGS failed"; exit 1; }
NAMES=$(printf '%s\n' "$LOGS" | cut -f1 | grep -E '\.[0-9]{6,}$' | sort -t. -k2,2n)
[ -n "$NAMES" ] || { log "no binary logs on the server (is log_bin on?)"; exit 1; }

OLD_UUID=$(cat "$STATE/server_uuid" 2>/dev/null || true)
if [ -n "$OLD_UUID" ] && [ "$OLD_UUID" != "$UUID" ]; then
    if [ -n "$(newest_spool)" ]; then
        # The old server's files must be shipped first (all of them count as closed now)
        touch "$STATE/spool-closed"
        log "server changed ($OLD_UUID -> $UUID); waiting for backupx to ship the old spool"
        sleep 60
        exit 1
    fi
    event "gap: server_uuid changed $OLD_UUID -> $UUID"
    rm -f "$STATE/last_shipped"
fi
if [ -e "$STATE/spool-closed" ]; then
    if [ -n "$(newest_spool)" ]; then
        log "spool marked closed; waiting for backupx to ship it"
        sleep 60
        exit 1
    fi
    rm -f "$STATE/spool-closed"
fi
printf '%s\n' "$UUID" > "$STATE/server_uuid"

first_after() {  # first server binlog whose number is greater than $1's
    local n; n=$(seq_of "$1")
    printf '%s\n' "$NAMES" | while read -r name; do
        [ "$(seq_of "$name")" -gt "$n" ] && { printf '%s\n' "$name"; break; }
    done
}

NEWEST=$(newest_spool)
LAST=$(cat "$STATE/last_shipped" 2>/dev/null || true)
if [ -n "$NEWEST" ]; then
    if printf '%s\n' "$NAMES" | grep -qx "$NEWEST"; then
        START="$NEWEST"  # re-fetch the file being written when we stopped
    else
        START=$(first_after "$NEWEST")
        [ -n "$START" ] || START=$(printf '%s\n' "$NAMES" | tail -n1)
        event "gap: $NEWEST was purged on the server before it was fully streamed; continuing at $START"
    fi
elif [ -n "$LAST" ]; then
    START=$(first_after "$LAST")
    if [ -z "$START" ]; then
        START=$(printf '%s\n' "$NAMES" | tail -n1)
    elif [ "$(seq_of "$START")" -ne $(( $(seq_of "$LAST") + 1 )) ]; then
        event "gap: binlogs after $LAST were purged on the server before they were streamed; continuing at $START"
    fi
else
    START=$(printf '%s\n' "$NAMES" | tail -n1)
    log "first start: streaming from the current binlog $START"
fi

if pitr_client "$BINLOG" --help 2>/dev/null | grep -q -- '--connection-server-id'; then
    SID_OPT="--connection-server-id=$PITR_SERVER_ID"
else
    SID_OPT="--stop-never-slave-server-id=$PITR_SERVER_ID"
fi

[ -n "$PITR_RUN_IMAGE" ] && $DOCKER rm -f "$CONTAINER_NAME" >/dev/null 2>&1
log "streaming from $START (server $UUID)"
pitr_client --name "$CONTAINER_NAME" "$BINLOG" --defaults-extra-file="$CNF" \
    --read-from-remote-server --raw --stop-never "$SID_OPT" \
    --result-file="$OUT_DIR/" "$START" &
PID=$!
stop() {
    kill "$PID" 2>/dev/null
    [ -n "$PITR_RUN_IMAGE" ] && $DOCKER rm -f "$CONTAINER_NAME" >/dev/null 2>&1
    wait "$PID" 2>/dev/null
    exit 0
}
trap stop TERM INT

# Watchdog: in sync = we hold the server's current binlog and at least what the
# server had a minute ago. Out of sync for 5 checks in a row = stalled
# connection; exit so systemd restarts us (resuming from the newest file).
behind=0
prev_size=""
while kill -0 "$PID" 2>/dev/null; do
    sleep 60 & wait $!
    CUR=$(pitr_q "SHOW BINARY LOGS" 2>/dev/null | tail -n1 | cut -f1,2) || continue
    CUR_NAME=$(printf '%s' "$CUR" | cut -f1)
    CUR_SIZE=$(printf '%s' "$CUR" | cut -f2)
    LOCAL=$(newest_spool)
    LOCAL_SIZE=$(stat -c %s "$SPOOL/$LOCAL" 2>/dev/null || echo 0)
    if [ "$CUR_NAME" = "$LOCAL" ] && { [ -z "$prev_size" ] || [ "$LOCAL_SIZE" -ge "$prev_size" ]; }; then
        behind=0
        touch "$STATE/heartbeat"
    else
        behind=$((behind + 1))
    fi
    if [ "$CUR_NAME" = "$LOCAL" ]; then prev_size="$CUR_SIZE"; else prev_size=""; fi
    if [ "$behind" -ge 5 ]; then
        log "stalled: server at $CUR_NAME, spool at $LOCAL; restarting"
        kill "$PID" 2>/dev/null
        [ -n "$PITR_RUN_IMAGE" ] && $DOCKER rm -f "$CONTAINER_NAME" >/dev/null 2>&1
        break
    fi
done
wait "$PID"
exit 1
'''.replace('@CLIENT_RUNNER@', CLIENT_RUNNER).replace('@VERSION@', STREAMER_VERSION)

UNIT_FILE = '''[Unit]
Description=backupx PITR binlog streamer (%i)
After=network-online.target docker.service
Wants=network-online.target
# Restart forever: a database outage must not leave the streamer stopped
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/local/lib/backupx-pitr/binlog-streamer.sh %i
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
'''


def mysql_cnf(db_config, docker_mode):
    """[client] option file content for the streamer (holds the password)."""
    password = (db_config.get('password') or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    user = (db_config.get('username') or '').replace('\n', '')
    if docker_mode:
        host, port = '127.0.0.1', 3306  # inside the database container's network namespace
    else:
        host = (db_config.get('host') or 'localhost').replace('\n', '')
        port = int(db_config.get('port') or 3306)
        if host == 'localhost':
            host = '127.0.0.1'  # TCP, not the socket (the client may run in a container)
    return f'[client]\nuser="{user}"\npassword="{password}"\nhost={host}\nport={port}\n'


def mysql_env(job_id, db_config, client_image=''):
    container = (db_config.get('docker_container') or '').strip()
    lines = [
        f'PITR_JOB={q(job_id)}',
        f'PITR_CONTAINER={q(container)}',
        f'PITR_IMAGE={q("" if container else client_image)}',
        f'PITR_SERVER_ID={pitr.connection_server_id(job_id)}',
    ]
    return '\n'.join(lines) + '\n'


_SUDO = 'SUDO=""; if [ "$(id -u)" != 0 ]; then SUDO="sudo -n"; fi'
# Run a shell command line as the postgres OS user (root: su; others: sudo -n -u postgres)
_AS_PG = '''as_pg() {
    if [ "$(id -u)" = 0 ]; then su -s /bin/sh postgres -c "$1"; else sudo -n -u postgres sh -c "$1"; fi
}'''


def ensure_and_scan_script(job_id, db_config, client_image=''):
    """Install/refresh the streamer, start it if needed, then report the spool.

    Files are only replaced (and the unit restarted) when their content changed.
    """
    docker_mode = bool((db_config.get('docker_container') or '').strip())
    return f'''
set -u
{_SUDO}
J={q(job_id)}
CONF_DIR="{CONF_ROOT}/$J"
STATE="{STATE_ROOT}/$J"
SPOOL="$STATE/spool"
UNIT="backupx-pitr-binlog@$J.service"
GRP=$(id -gn)
fail() {{ printf 'PITR\\tERROR\\t%s\\n' "$*"; exit 1; }}
ENV_CONTENT={q(mysql_env(job_id, db_config, client_image))}
CNF_CONTENT={q(mysql_cnf(db_config, docker_mode))}
STREAMER_CONTENT={q(STREAMER_SCRIPT)}
UNIT_CONTENT={q(UNIT_FILE)}

$SUDO mkdir -p "$CONF_DIR" /usr/local/lib/backupx-pitr "$SPOOL" \\
    || fail "can't create the streamer directories (root or passwordless sudo needed)"
$SUDO chmod 700 "$CONF_DIR"
$SUDO chown "root:$GRP" "$STATE" "$SPOOL" && $SUDO chmod 2770 "$STATE" "$SPOOL" \\
    || fail "can't set up $STATE"

CHANGED=0
put() {{  # put DEST MODE CONTENT -- content goes through a pipe, never argv
    local tmp="$1.new.$$"
    printf '%s' "$3" | $SUDO sh -c 'umask 077; cat > "$1"' sh "$tmp" || return 1
    if $SUDO cmp -s "$tmp" "$1"; then
        $SUDO rm -f "$tmp"
    else
        $SUDO chmod "$2" "$tmp" && $SUDO mv -f "$tmp" "$1" && CHANGED=1
    fi
}}
put "$CONF_DIR/env" 600 "$ENV_CONTENT" || fail "can't write $CONF_DIR/env"
put "$CONF_DIR/my.cnf" 600 "$CNF_CONTENT" || fail "can't write $CONF_DIR/my.cnf"
put {STREAMER_PATH} 755 "$STREAMER_CONTENT" || fail "can't write the streamer script"
put {UNIT_PATH} 644 "$UNIT_CONTENT" || fail "can't write the systemd unit"
unset CNF_CONTENT

if [ "$CHANGED" = 1 ]; then
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$UNIT" >/dev/null 2>&1
    $SUDO systemctl restart "$UNIT" || fail "systemctl restart $UNIT failed"
    printf 'PITR\\tINSTALL\\tupdated\\n'
elif ! $SUDO systemctl is-active --quiet "$UNIT"; then
    $SUDO systemctl reset-failed "$UNIT" >/dev/null 2>&1
    $SUDO systemctl enable "$UNIT" >/dev/null 2>&1
    $SUDO systemctl start "$UNIT" || fail "systemctl start $UNIT failed"
    printf 'PITR\\tINSTALL\\tstarted\\n'
fi

printf 'PITR\\tNOW\\t%s\\n' "$(date +%s)"
printf 'PITR\\tACTIVE\\t%s\\n' "$($SUDO systemctl is-active "$UNIT" 2>/dev/null)"
[ -f "$STATE/server_uuid" ] && printf 'PITR\\tUUID\\t%s\\n' "$(head -n1 "$STATE/server_uuid")"
[ -f "$STATE/server_version" ] && printf 'PITR\\tSERVER_VERSION\\t%s\\n' "$(head -n1 "$STATE/server_version")"
[ -f "$STATE/last_shipped" ] && printf 'PITR\\tLAST_SHIPPED\\t%s\\n' "$(head -n1 "$STATE/last_shipped")"
[ -e "$STATE/spool-closed" ] && printf 'PITR\\tCLOSED_ALL\\t1\\n'
[ -e "$STATE/heartbeat" ] && printf 'PITR\\tHEARTBEAT\\t%s\\n' "$(stat -c %Y "$STATE/heartbeat")"
for f in "$SPOOL"/*; do
    [ -f "$f" ] || continue
    printf 'PITR\\tFILE\\t%s\\t%s\\n' "${{f##*/}}" "$(stat -c '%s	%Y' "$f")"
done
if [ -f "$STATE/events" ]; then
    n=0
    while IFS= read -r line; do
        n=$((n + 1))
        printf 'PITR\\tEVENT\\t%d\\t%s\\n' "$n" "$line"
    done < "$STATE/events"
fi
if ! $SUDO systemctl is-active --quiet "$UNIT"; then
    $SUDO journalctl -u "$UNIT" -n 8 --no-pager -o cat 2>/dev/null | sed 's/^/PITR\\tJOURNAL\\t/'
fi
exit 0
'''


def _iso(ts):
    from datetime import datetime
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ship_script(job_id, segments, partial, stale, ack_events, restic_password, target_blocks):
    """Ship closed binlog segments (+ a copy of the open file) to every storage.

    target_blocks: main._restic_target_blocks(binlog_job, targets, 'pitr_ship'),
    so each storage gets the usual init-if-missing handling and markers. Only
    when every storage succeeded are the shipped files deleted from the spool.
    """
    cmds = []
    for index, seg in enumerate(segments):
        tags = ['pitr', 'binlog', f"first:{seg['first_file']}", f"last:{seg['last_file']}",
                f"until:{_iso(seg['end_time'])}", f"chain:{seg['chain_id']}"]
        files = ' '.join(f'"$SPOOL"/{q(name)}' for name in seg['files'])
        cmds.append(
            f"    printf '{{\"message_type\":\"backupx_pitr\",\"what\":\"segment\",\"index\":{index}}}\\n'\n"
            f"    restic backup --json --host backupx-pitr {' '.join('--tag ' + q(t) for t in tags)} "
            f'"$@" {files} || return $?')
    if partial:
        tags = ['pitr', 'partial', f"file:{partial['name']}", f"until:{_iso(partial['mtime'])}"]
        cmds.append(
            f'    rm -rf "$STATE/partial" && mkdir -p "$STATE/partial" '
            f'&& cp -p "$SPOOL"/{q(partial["name"])} "$STATE/partial/" || return 1\n'
            f"    printf '{{\"message_type\":\"backupx_pitr\",\"what\":\"partial\",\"index\":0}}\\n'\n"
            f"    restic backup --json --host backupx-pitr {' '.join('--tag ' + q(t) for t in tags)} "
            f'"$@" "$STATE/partial"/{q(partial["name"])} || return $?')
    body = '\n'.join(cmds) or '    true'
    shipped = [name for seg in segments for name in seg['files']] + list(stale)
    remove = ' '.join(f'"$SPOOL"/{q(n)}' for n in shipped)
    last_file = segments[-1]['last_file'] if segments else ''
    remove_cmd = ('rm -f -- ' + remove) if remove else 'true'
    last_cmd = ('printf "%s\\n" ' + q(last_file) + ' > "$STATE/last_shipped"') if last_file else 'true'
    ack_cmd = f'sed -i "1,{int(ack_events)}d" "$STATE/events" 2>/dev/null' if ack_events else 'true'
    return f'''
set -u
export PATH="$HOME/.local/bin:$PATH"
export RESTIC_PASSWORD={q(restic_password)}
J={q(job_id)}
STATE="{STATE_ROOT}/$J"
SPOOL="$STATE/spool"
cd "$SPOOL" || {{ echo "spool $SPOOL missing"; exit 1; }}

# restic options for the storage (e.g. --insecure-tls) arrive as "$@"
pitr_ship() {{
{body}
}}
{target_blocks}
if [ "$BACKUPX_FAILED" = 0 ]; then
    {remove_cmd}
    {last_cmd}
    {ack_cmd}
    printf 'PITR\\tSHIPPED\\t1\\n'
fi
rm -rf "$STATE/partial"
exit $BACKUPX_FAILED
'''


def teardown_script(job_id):
    """Stop and remove the streamer for a job; the spool is left for inspection."""
    return f'''
set -u
{_SUDO}
J={q(job_id)}
UNIT="backupx-pitr-binlog@$J.service"
$SUDO systemctl disable --now "$UNIT" >/dev/null 2>&1
$SUDO rm -rf "{CONF_ROOT}/$J"
if docker info >/dev/null 2>&1; then docker rm -f "backupx-pitr-$J" >/dev/null 2>&1; \\
else $SUDO docker rm -f "backupx-pitr-$J" >/dev/null 2>&1; fi
printf 'PITR\\tSTOPPED\\t1\\n'
exit 0
'''


def mysql_prereq_script(db_config, client_image=''):
    container = (db_config.get('docker_container') or '').strip()
    docker_mode = bool(container)
    return f'''
set -u
{_SUDO}
fact() {{ printf 'PITR\\t%s\\t%s\\n' "$1" "$2"; }}
CONF_DIR=$(mktemp -d)
trap 'rm -rf "$CONF_DIR"' EXIT
CNF_CONTENT={q(mysql_cnf(db_config, docker_mode))}
( umask 077; printf '%s' "$CNF_CONTENT" > "$CONF_DIR/my.cnf" )
unset CNF_CONTENT
SPOOL=""
PITR_CONTAINER={q(container)}
PITR_IMAGE={q('' if container else client_image)}
{CLIENT_RUNNER}
[ "$(id -u)" = 0 ] && fact is_root 1 || fact is_root 0
sudo -n true >/dev/null 2>&1 && fact sudo 1 || fact sudo 0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then fact systemd 1; else fact systemd 0; fi
if docker info >/dev/null 2>&1 || sudo -n docker info >/dev/null 2>&1; then fact docker 1; else fact docker 0; fi
fact free_mb "$(df -Pm /var/lib 2>/dev/null | awk 'NR==2 {{print $4}}')"
if ! pitr_setup_client 2>"$CONF_DIR/err"; then
    fact connect_error "$(head -c 300 "$CONF_DIR/err" | tr '\\n' ' ')"
    exit 0
fi
pitr_find_programs
fact binlog_client "${{BINLOG##*/}}"
[ -n "$BINLOG" ] && fact binlog_client_version "$(pitr_client "$BINLOG" --version 2>/dev/null | head -n1)"
DUMP=$(pitr_client sh -c 'command -v mysqldump || command -v mariadb-dump' 2>/dev/null | tail -n1)
fact dump_client "${{DUMP##*/}}"
[ -n "$DUMP" ] && fact dump_client_version "$(pitr_client "$DUMP" --version 2>/dev/null | head -n1)"
if [ -z "$MYSQL" ]; then
    fact connect_error "no mysql/mariadb client program found"
    exit 0
fi
OUT=$(pitr_client "$MYSQL" --defaults-extra-file="$CNF" -N -B --force -e "
SELECT 'version', VERSION();
SELECT 'user', CURRENT_USER();
SELECT 'log_bin', @@log_bin;
SELECT 'binlog_format', @@binlog_format;
SELECT 'binlog_row_image', @@binlog_row_image;
SELECT 'server_id', @@server_id;
SELECT 'gtid_mode', @@gtid_mode;
SELECT 'server_uuid', @@server_uuid;
SELECT 'time_zone', CONCAT(@@system_time_zone, ' / ', @@time_zone);
SELECT 'binlog_expire_seconds', @@expire_logs_days * 86400;
SELECT 'binlog_expire_seconds', @@binlog_expire_logs_seconds;
" 2>"$CONF_DIR/err")
if ! printf '%s\\n' "$OUT" | grep -q '^version'; then
    fact connect_error "$(grep -v 'Using a password' "$CONF_DIR/err" | head -c 300 | tr '\\n' ' ')"
    exit 0
fi
printf '%s\\n' "$OUT" | while IFS="$(printf '\\t')" read -r k v; do
    [ -n "$k" ] && [ "$v" != "NULL" ] && fact "$k" "$v"
done
fact grants "$(pitr_client "$MYSQL" --defaults-extra-file="$CNF" -N -B -e 'SHOW GRANTS' 2>/dev/null | tr '\\n' '|')"
exit 0
'''


# ---------------------------------------------------------------------------
# Restore script (downloaded by the user, run on any docker host)
# ---------------------------------------------------------------------------

def mysql_restore_script(job, plan, target_time, storage, main_repo, binlog_repo, server_image,
                         binlog_image, flavor):
    """A bash script that restores `job` to `target_time` in a throwaway local container.

    server_image runs the scratch database (same flavor/version as production);
    binlog_image provides mysqlbinlog / mariadb-binlog (the official mysql
    images don't ship mysqlbinlog). Files reach containers through tar on stdin
    and `docker exec -i`, so it works with a local or a remote docker daemon.
    No secret is embedded: the restic password and the storage keys are read
    from the environment (or prompted for).
    """
    key = storage.get('id') or ''
    anchor = plan['anchor']
    dump_snapshot = anchor['snapshots'][key]
    snapshots = [seg['snapshots'][key] for seg in plan['segments']]
    partial = plan.get('partial')
    partial_snapshot = partial['snapshots'][key] if partial else ''
    t = target_time.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    base, start_seq = pitr.split_binlog_name(anchor['binlog_file'])
    is_maria = flavor == 'mariadb'
    insecure = ' --insecure-tls' if storage.get('skip_ssl_verify') else ''
    client = 'mariadb' if is_maria else 'mysql'
    admin = 'mariadb-admin' if is_maria else 'mysqladmin'
    dump_tool = 'mariadb-dump' if is_maria else 'mysqldump'
    root_env = '-e MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1' if is_maria else '-e MYSQL_ALLOW_EMPTY_PASSWORD=yes'
    # GTID mode on, so the dump's SET @@GLOBAL.GTID_PURGED loads; the replay itself is by position
    server_args = '' if is_maria else '--gtid-mode=ON --enforce-gtid-consistency=ON'
    skip_gtids = '' if is_maria else '--skip-gtids'
    anchor_time = pitr._utc(anchor['time']).strftime('%Y-%m-%d %H:%M:%S')
    return f'''#!/usr/bin/env bash
# backupx point-in-time restore
#   job:          {job.get('name')} ({job.get('id')})
#   restore to:   {t} UTC
#   storage:      {storage.get('name')}
#   dump:         snapshot {dump_snapshot} taken {anchor_time} UTC,
#                 binlog position {anchor['binlog_file']}:{anchor['binlog_pos']}
#   binlogs:      {len(snapshots)} shipped segment(s){' + the open-file copy' if partial else ''}
#
# Restores into a NEW local container ({server_image}); it never touches the
# production database. Needs: bash, docker, restic, ~3x the dump size free.
# Run:   RESTIC_PASSWORD=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... bash this-script.sh
# (the job's restic password and the storage keys; missing ones are prompted for)
set -euo pipefail

TARGET_TIME={q(t)}  # UTC; the replay stops before the first event at or after this time
MAIN_REPO={q(main_repo)}
BINLOG_REPO={q(binlog_repo)}
SERVER_IMAGE={q(server_image)}
BINLOG_IMAGE={q(binlog_image)}
DUMP_SNAPSHOT={q(dump_snapshot)}
START_FILE={q(anchor['binlog_file'])}
START_POS={int(anchor['binlog_pos'])}
BINLOG_BASE={q(base)}
START_SEQ={int(start_seq)}
BINLOG_SNAPSHOTS=({' '.join(q(s) for s in snapshots)})
PARTIAL_SNAPSHOT={q(partial_snapshot)}
NAME="${{NAME:-backupx-pitr-restore-{job.get('id')}}}"
WORK="${{WORK:-$(mktemp -d)}}"

for v in RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    if [ -z "${{!v:-}}" ]; then read -r -s -p "$v: " "$v"; echo; export "$v"; fi
done
export RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
command -v restic >/dev/null || {{ echo "restic is not installed" >&2; exit 1; }}
command -v docker >/dev/null || {{ echo "docker is not installed" >&2; exit 1; }}
if docker ps -a --format '{{{{.Names}}}}' | grep -qx "$NAME"; then
    echo "A container named $NAME exists already; remove it or set NAME=..." >&2; exit 1
fi
seq_of() {{ printf '%s' "$1" | sed -n 's/.*\\.0*\\([0-9][0-9]*\\)$/\\1/p'; }}

echo "== Work directory: $WORK"
mkdir -p "$WORK/dump" "$WORK/binlog-raw" "$WORK/binlogs"

echo "== Fetching the dump"
restic -r "$MAIN_REPO"{insecure} restore "$DUMP_SNAPSHOT" --target "$WORK/dump"
DUMP=$(find "$WORK/dump" -type f -name '*.sql.gz' | head -n1)
[ -n "$DUMP" ] || {{ echo "no .sql.gz in snapshot $DUMP_SNAPSHOT" >&2; exit 1; }}

echo "== Fetching ${{#BINLOG_SNAPSHOTS[@]}} binlog segment(s)"
for s in "${{BINLOG_SNAPSHOTS[@]}}"; do
    restic -r "$BINLOG_REPO"{insecure} restore "$s" --target "$WORK/binlog-raw"
done
find "$WORK/binlog-raw" -path '*/spool/*' -type f -exec mv -f {{}} "$WORK/binlogs/" \\;
if [ -n "$PARTIAL_SNAPSHOT" ]; then
    restic -r "$BINLOG_REPO"{insecure} restore "$PARTIAL_SNAPSHOT" --target "$WORK/binlog-raw/partial"
    find "$WORK/binlog-raw/partial" -type f -exec mv -f {{}} "$WORK/binlogs/" \\;
fi
# The files from the dump's position on, in order, without holes
FILES=()
prev=""
for f in $(cd "$WORK/binlogs" && ls -1 | grep -F "$BINLOG_BASE." | grep -E '\\.[0-9]{{6,}}$' | sort -t. -k2,2n); do
    n=$(seq_of "$f")
    [ "$n" -ge "$START_SEQ" ] || continue
    if [ -n "$prev" ] && [ "$n" -ne $((prev + 1)) ]; then echo "binlog sequence has a hole before $f" >&2; exit 1; fi
    FILES+=("$f")
    prev=$n
done
[ "${{FILES[0]:-}}" = "$START_FILE" ] || {{ echo "binlog $START_FILE (the dump's position) is missing" >&2; exit 1; }}
echo "   ${{#FILES[@]}} binlog file(s): ${{FILES[0]}} .. ${{FILES[${{#FILES[@]}}-1]}}"

echo "== Decoding binlogs from $START_FILE:$START_POS until $TARGET_TIME UTC ($BINLOG_IMAGE)"
# The open-file copy can end mid-event: the reader then stops with an error after the
# last complete event, which is expected (an unfinished transaction is rolled back).
set +e
tar -C "$WORK/binlogs" -cf - "${{FILES[@]}}" | docker run --rm -i -e TZ=UTC --entrypoint sh "$BINLOG_IMAGE" -c '
    mkdir -p /tmp/b && tar -C /tmp/b -xf - && cd /tmp/b &&
    exec "$(command -v mariadb-binlog || command -v mysqlbinlog)" "$@"' sh {skip_gtids} \\
    --start-position="$START_POS" --stop-datetime="$TARGET_TIME" "${{FILES[@]}}" > "$WORK/replay.sql" 2> "$WORK/replay.err"
RC=$?
set -e
if [ $RC -ne 0 ]; then
    if [ -n "$PARTIAL_SNAPSHOT" ]; then
        echo "   (reader stopped at the end of the open-file copy: $(tail -n1 "$WORK/replay.err"))"
    else
        cat "$WORK/replay.err" >&2; echo "decoding the binlogs failed" >&2; exit 1
    fi
fi

echo "== Starting a scratch $SERVER_IMAGE container ($NAME, no published ports)"
docker run -d --name "$NAME" {root_env} -e TZ=UTC "$SERVER_IMAGE" {server_args} >/dev/null
ready=0
for i in $(seq 1 150); do
    # The entrypoint restarts the server once after initialising; wait for the final one
    if docker exec "$NAME" sh -c 'test -f /var/lib/mysql/*.pid 2>/dev/null || ls /var/run/mysqld/*.pid >/dev/null 2>&1' \\
       && docker exec "$NAME" {admin} ping -uroot --silent >/dev/null 2>&1 \\
       && ! docker logs "$NAME" 2>&1 | tail -n 5 | grep -q 'Temporary server'; then
        sleep 3
        if docker exec "$NAME" {client} -uroot -e 'SELECT 1' >/dev/null 2>&1; then ready=1; break; fi
    fi
    sleep 2
done
[ "$ready" = 1 ] || {{ echo "scratch server did not start; see: docker logs $NAME" >&2; exit 1; }}

echo "== Loading the dump"
gunzip -c "$DUMP" | docker exec -i "$NAME" {client} -uroot

echo "== Replaying binlogs"
docker exec -i "$NAME" {client} -uroot < "$WORK/replay.sql"

echo
echo "== Done. The database as of $TARGET_TIME UTC is in container $NAME:"
echo "   docker exec -it $NAME {client} -uroot"
echo "   docker exec $NAME {dump_tool} -uroot --all-databases --single-transaction | gzip > restored.sql.gz"
echo "   Remove when finished: docker rm -f $NAME && rm -rf $WORK"
'''


# ---------------------------------------------------------------------------
# WAL-G (PostgreSQL)
# ---------------------------------------------------------------------------

WALG_ENV_PATH = '/etc/wal-g/wal-g.env'
WALG_ARCHIVE_SCRIPT_PATH = '/usr/local/bin/wal-g-archive.sh'

WALG_ARCHIVE_SCRIPT = '''#!/bin/sh
# backupx: PostgreSQL archive_command -> wal-g wal-push. Managed by backupx.
set -a
. /etc/wal-g/wal-g.env
set +a
exec /usr/local/bin/wal-g wal-push "$1"
'''


def walg_env(storage, prefix, pgdata, restic_password=None):
    """/etc/wal-g/wal-g.env content (0600, owned by postgres; holds the S3 keys)."""
    from .main import s3_endpoint_url  # lazy: main imports this module
    lines = {
        'WALG_S3_PREFIX': f"s3://{storage['bucket']}/{prefix}{pitr.WAL_REPO_SUFFIX}",
        'AWS_ACCESS_KEY_ID': storage.get('access_key') or '',
        'AWS_SECRET_ACCESS_KEY': storage.get('secret_key') or '',
        'AWS_ENDPOINT': s3_endpoint_url(storage['endpoint']),
        'AWS_S3_FORCE_PATH_STYLE': 'true',
        'AWS_REGION': storage.get('region') or 'us-east-1',
        'WALG_COMPRESSION_METHOD': 'lz4',
        'PGHOST': '/var/run/postgresql',
        'PGDATA': pgdata or '',
    }
    # TODO(pitr-pg): storages with skip_ssl_verify need a CA file (WALG_S3_CA_CERT_FILE) for wal-g
    return ''.join(f'{k}={q(v)}\n' for k, v in lines.items())


def walg_install_script(storage, prefix, pgdata=''):
    """Install wal-g (build picked by glibc / PostgreSQL version), its env file and
    the archive script. Prints the postgresql.conf changes; never touches
    postgresql.conf and never restarts PostgreSQL."""
    builds = ' '.join(f'{v}:{a}:{s}:{g[0]}.{g[1]}' for g, v, a, s in pitr.WALG_BUILDS)
    return f'''
set -u
{_SUDO}
{_AS_PG}
fail() {{ printf 'PITR\\tERROR\\t%s\\n' "$*"; exit 1; }}
PG_NUM=$(echo 'SHOW server_version_num' | as_pg 'psql -XtA' 2>/dev/null)
PG_MAJOR=$(( ${{PG_NUM:-0}} / 10000 ))
GLIBC=$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\\.[0-9]+$')
[ "$(uname -m)" = x86_64 ] || fail "only amd64 WAL-G builds are wired up"
PICK=""
for b in {builds}; do
    v=${{b%%:*}}; rest=${{b#*:}}; asset=${{rest%%:*}}; rest=${{rest#*:}}; sha=${{rest%%:*}}; min=${{rest#*:}}
    if [ "$(printf '%s\\n%s\\n' "$min" "$GLIBC" | sort -V | head -n1)" = "$min" ]; then
        case "$v" in v3*) [ -n "$PG_MAJOR" ] && [ "$PG_MAJOR" -lt 12 ] && continue ;; esac
        PICK="$v $asset $sha"; break
    fi
done
[ -n "$PICK" ] || fail "no WAL-G build for glibc $GLIBC"
set -- $PICK
if ! /usr/local/bin/wal-g --version 2>/dev/null | grep -q "$1"; then
    T=$(mktemp -d)
    curl -fsSL -o "$T/w.tgz" "https://github.com/wal-g/wal-g/releases/download/$1/$2.tar.gz" || fail "download failed"
    echo "$3  $T/w.tgz" | sha256sum -c - >/dev/null || fail "checksum mismatch for $2"
    tar -xzf "$T/w.tgz" -C "$T" || fail "can't unpack $2"
    $SUDO install -m 0755 "$T/$2" /usr/local/bin/wal-g || fail "can't install wal-g"
    rm -rf "$T"
fi
printf 'PITR\\tWALG_VERSION\\t%s\\n' "$(/usr/local/bin/wal-g --version 2>&1 | head -n1)"
PGDATA_DIR={q(pgdata)}
[ -n "$PGDATA_DIR" ] || PGDATA_DIR=$(echo 'SHOW data_directory' | as_pg 'psql -XtA' 2>/dev/null)
ENV_CONTENT={q(walg_env(storage, prefix, '__PGDATA__'))}
ENV_CONTENT=$(printf '%s' "$ENV_CONTENT" | sed "s#__PGDATA__#$PGDATA_DIR#")
$SUDO mkdir -p /etc/wal-g && $SUDO chmod 750 /etc/wal-g && $SUDO chown root:postgres /etc/wal-g
printf '%s\\n' "$ENV_CONTENT" | $SUDO sh -c 'umask 077; cat > {WALG_ENV_PATH}.new' \\
    && $SUDO chown postgres:postgres {WALG_ENV_PATH}.new && $SUDO mv -f {WALG_ENV_PATH}.new {WALG_ENV_PATH} \\
    || fail "can't write {WALG_ENV_PATH}"
unset ENV_CONTENT
printf '%s' {q(WALG_ARCHIVE_SCRIPT)} | $SUDO sh -c 'cat > {WALG_ARCHIVE_SCRIPT_PATH}' \\
    && $SUDO chmod 755 {WALG_ARCHIVE_SCRIPT_PATH} || fail "can't write the archive script"
printf 'PITR\\tPGDATA\\t%s\\n' "$PGDATA_DIR"
printf 'PITR\\tINSTALLED\\t1\\n'
exit 0
'''


def walg_run_script(inner):
    """Run `inner` (e.g. 'wal-g backup-push "$PGDATA"') as postgres with the wal-g env loaded.

    The env file is sourced inside the postgres shell, so the S3 keys are never in any argv.
    """
    line = f'set -a; . {WALG_ENV_PATH}; set +a; PATH=/usr/local/bin:$PATH; {inner}'
    return f'''
set -u
{_AS_PG}
as_pg {q(line)}
'''


def pg_prereq_script():
    return f'''
set -u
{_AS_PG}
fact() {{ printf 'PITR\\t%s\\t%s\\n' "$1" "$2"; }}
[ "$(id -u)" = 0 ] && fact is_root 1 || fact is_root 0
sudo -n true >/dev/null 2>&1 && fact sudo 1 || fact sudo 0
fact glibc "$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\\.[0-9]+$')"
fact walg_version "$(/usr/local/bin/wal-g --version 2>/dev/null | head -n1)"
SQL="
SELECT 'server_version_num', current_setting('server_version_num')
UNION ALL SELECT 'server_version', current_setting('server_version')
UNION ALL SELECT 'wal_level', current_setting('wal_level')
UNION ALL SELECT 'archive_mode', current_setting('archive_mode')
UNION ALL SELECT 'archive_command', current_setting('archive_command')
UNION ALL SELECT 'archive_timeout', current_setting('archive_timeout')
UNION ALL SELECT 'data_directory', current_setting('data_directory')
UNION ALL SELECT 'archiver_archived_count', archived_count::text FROM pg_stat_archiver
UNION ALL SELECT 'archiver_last_archived_wal', coalesce(last_archived_wal, '') FROM pg_stat_archiver
UNION ALL SELECT 'archiver_last_archived_time', coalesce(extract(epoch from last_archived_time)::bigint::text, '') FROM pg_stat_archiver
UNION ALL SELECT 'archiver_failed_count', failed_count::text FROM pg_stat_archiver
UNION ALL SELECT 'archiver_last_failed_wal', coalesce(last_failed_wal, '') FROM pg_stat_archiver
UNION ALL SELECT 'archiver_last_failed_time', coalesce(last_failed_time::text, '') FROM pg_stat_archiver;
"
OUT=$(printf '%s' "$SQL" | as_pg 'psql -XtA -F "	"' 2>&1) || {{ fact connect_error "$(printf '%s' "$OUT" | head -c 300 | tr '\\n' ' ')"; exit 0; }}
printf '%s\\n' "$OUT" | while IFS="$(printf '\\t')" read -r k v; do [ -n "$k" ] && fact "$k" "$v"; done
exit 0
'''


def pg_restore_script(job, target_time, storage, prefix, image='postgres:10-bullseye'):
    """Restore a WAL-G cluster to target_time into a scratch container (PostgreSQL 10 layout).

    The scratch image must have glibc >= 2.27 so the server's wal-g build runs in it
    as restore_command: postgres:10 is Debian 9 (glibc 2.24), postgres:10-bullseye works.

    TODO(pitr-pg): PostgreSQL 12+ uses recovery.signal + postgresql.auto.conf
    instead of recovery.conf; pick by major version once a 12+ target exists.
    """
    from .main import s3_endpoint_url
    t = target_time.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S+00')
    walg_prefix = f"s3://{storage['bucket']}/{prefix}{pitr.WAL_REPO_SUFFIX}"
    return f'''#!/usr/bin/env bash
# backupx point-in-time restore (PostgreSQL / WAL-G)
#   job:        {job.get('name')} ({job.get('id')})
#   restore to: {t}
#   storage:    {storage.get('name')} ({walg_prefix})
# Restores into a NEW local container ({image}); never touches production.
# Needs: bash, docker, python3 and a linux/amd64 wal-g binary (WALG_BIN, default: wal-g on PATH;
# use the same release as on the server).
# Run:   AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... bash this-script.sh
# The scratch server fetches WAL itself: set NETWORK=<docker network> if the storage
# endpoint is only reachable from a specific docker network.
set -euo pipefail
TARGET_TIME={q(t)}
IMAGE={q(image)}
NAME="${{NAME:-backupx-pitr-restore-{job.get('id')}}}"
WORK="${{WORK:-$(mktemp -d)}}"
WALG_BIN="${{WALG_BIN:-$(command -v wal-g || true)}}"
[ -x "$WALG_BIN" ] || {{ echo "set WALG_BIN to a wal-g binary" >&2; exit 1; }}
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    if [ -z "${{!v:-}}" ]; then read -r -s -p "$v: " "$v"; echo; export "$v"; fi
done
export WALG_S3_PREFIX={q(walg_prefix)} AWS_ENDPOINT={q(s3_endpoint_url(storage['endpoint']))}
export AWS_S3_FORCE_PATH_STYLE=true AWS_REGION={q(storage.get('region') or 'us-east-1')}
mkdir -p "$WORK/pgdata" "$WORK/walg"
# Newest base backup that finished before the target time
BACKUP=$("$WALG_BIN" backup-list --json --detail 2>/dev/null | python3 -c '
import json, sys
from datetime import datetime
target = datetime.fromisoformat(sys.argv[1].replace("+00", "+00:00"))
rows = [b for b in json.load(sys.stdin)
        if datetime.fromisoformat(b["finish_time"].replace("Z", "+00:00")) <= target]
print(max(rows, key=lambda b: b["finish_time"])["backup_name"] if rows else "")
' "$TARGET_TIME")
[ -n "$BACKUP" ] || {{ echo "no base backup finished before $TARGET_TIME" >&2; exit 1; }}
echo "== Fetching base backup $BACKUP"
"$WALG_BIN" backup-fetch "$WORK/pgdata" "$BACKUP"
# The container fetches WAL itself (restore_command); its credentials go in a 0600
# env file, not in docker's argv.
cp "$WALG_BIN" "$WORK/walg/wal-g"
( umask 077; env | grep -E '^(WALG_|AWS_)' > "$WORK/walg/env" )
cat > "$WORK/pgdata/recovery.conf" <<RECOVERY
restore_command = '/walg/wal-g wal-fetch "%f" "%p"'
recovery_target_time = '$TARGET_TIME'
recovery_target_action = 'promote'
RECOVERY
rm -f "$WORK/pgdata/postmaster.pid"
# Debian/Ubuntu clusters keep postgresql.conf / pg_hba.conf in /etc/postgresql, outside
# the data directory: give the scratch server minimal ones (no published ports anyway)
[ -f "$WORK/pgdata/postgresql.conf" ] || printf "listen_addresses = '*'\\n" > "$WORK/pgdata/postgresql.conf"
[ -f "$WORK/pgdata/pg_hba.conf" ] || printf 'local all all trust\\nhost all all all trust\\n' > "$WORK/pgdata/pg_hba.conf"
[ -f "$WORK/pgdata/pg_ident.conf" ] || : > "$WORK/pgdata/pg_ident.conf"
echo "== Starting $IMAGE as $NAME (recovers to $TARGET_TIME, then promotes)"
# The image's entrypoint (as root) hands the data directory to its postgres user
docker run -d --name "$NAME" --env-file "$WORK/walg/env" ${{NETWORK:+--network "$NETWORK"}} \\
    -v "$WORK/pgdata:/var/lib/postgresql/data" -v "$WORK/walg:/walg:ro" "$IMAGE" >/dev/null
echo "Follow recovery: docker logs -f $NAME   Connect: docker exec -it $NAME psql -U postgres"
echo "Remove when finished: docker rm -f $NAME && rm -rf $WORK"
'''
