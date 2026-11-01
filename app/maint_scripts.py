"""
Shell for database maintenance (the enable-PITR wizard's config changes and
restarts). Text only; app.pitr_enable_service sends these over SSH stdin.

Output lines are `MAINT\\t<key>\\t<value>`; repeated keys (log lines, option
lines) are collected with parse_lines().

Secrets: an admin or job database password only ever appears inside a script
(stdin of ssh), lands in a 0600 option file in a mktemp directory and is
removed by a trap; mysql/mysqld never get it in argv. In container mode the
option file goes into the client container on its stdin (pitr_client).
"""

import base64

from . import pitr_enable as pe
from . import pitr_scripts as ps

q = ps.q

HEAD = r'''set -u
SUDO=""; if [ "$(id -u)" != 0 ]; then SUDO="sudo -n"; fi
fact() { printf 'MAINT\t%s\t%s\n' "$1" "$2"; }
line() { while IFS= read -r l; do printf 'MAINT\t%s\t%s\n' "$1" "$l"; done; }
if docker info >/dev/null 2>&1; then DOCKER=docker; elif sudo -n docker info >/dev/null 2>&1; then DOCKER="sudo -n docker"; else DOCKER=""; fi
'''

# Debian/Ubuntu `my_print_defaults` prints every option, passwords included; never echo those.
MASK = r"sed -E 's/(pass(word)?[^=]*=).*/\1***/I'"


def parse(text):
    return ps.pitr.parse_kv_output(text, prefix='MAINT\t')


def parse_lines(text, key):
    out = []
    prefix = f'MAINT\t{key}\t'
    for raw in (text or '').splitlines():
        if raw.startswith(prefix):
            out.append(raw[len(prefix):])
    return out


def b64(text):
    return base64.b64encode((text or '').encode()).decode()


def unb64(text):
    try:
        return base64.b64decode(text or '').decode(errors='replace')
    except Exception:
        return ''


def _docker_mode(db_config):
    return bool((db_config.get('docker_container') or '').strip())


def _cnf(db_config, admin=None):
    """[client] option file for the job's login or a one-time admin login (+ a connect timeout)."""
    cfg = dict(db_config)
    if admin and admin.get('mode') == 'password':
        cfg.update(username=admin['username'], password=admin['password'])
    return ps.mysql_cnf(cfg, _docker_mode(db_config)) + 'connect-timeout=5\n'


def _client_setup(db_config, client_image, admin=None):
    """CLIENT_RUNNER with a temp CONF_DIR holding the option file; defines pitr_q."""
    container = (db_config.get('docker_container') or '').strip()
    return f'''CONF_DIR=$(mktemp -d)
trap 'rm -rf "$CONF_DIR"' EXIT
CNF_CONTENT={q(_cnf(db_config, admin))}
( umask 077; printf '%s' "$CNF_CONTENT" > "$CONF_DIR/my.cnf" )
unset CNF_CONTENT
SPOOL=""
PITR_CONTAINER={q(container)}
PITR_IMAGE={q('' if container else client_image)}
{ps.CLIENT_RUNNER}
pitr_setup_client 2>"$CONF_DIR/err" || {{ fact connect_error "$(head -c 300 "$CONF_DIR/err" | tr '\\n' ' ')"; exit 0; }}
pitr_find_programs || true
'''


# ---------------------------------------------------------------------------
# MySQL / MariaDB
# ---------------------------------------------------------------------------

def mysql_host_probe_script(db_config):
    """How the database is installed on the job's server (see pitr_enable.mysql_target)."""
    container = (db_config.get('docker_container') or '').strip()
    dests = ' '.join(pe.DOCKER_CONF_DESTS)
    dirs = ' '.join(pe.HOST_DROPIN_DIRS)
    return HEAD + ps.ROOT_FACTS + f'''
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then fact systemd 1; else fact systemd 0; fi
fact local_ips "$( (hostname -I 2>/dev/null; hostname -i 2>/dev/null; hostname 2>/dev/null) | tr '\\n' ' ')"
[ -n "$DOCKER" ] && fact docker 1 || fact docker 0
C={q(container)}
if [ -n "$C" ]; then
    if [ -n "$DOCKER" ] && $DOCKER container inspect "$C" >/dev/null 2>&1; then
        fact container_found 1
        fact docker_mounts "$($DOCKER inspect -f '{{{{range .Mounts}}}}{{{{.Destination}}}}|{{{{.Source}}}}|{{{{.RW}}}}|{{{{.Type}}}};{{{{end}}}}' "$C")"
        fact docker_image "$($DOCKER inspect -f '{{{{.Config.Image}}}}' "$C")"
        fact docker_restart_policy "$($DOCKER inspect -f '{{{{.HostConfig.RestartPolicy.Name}}}}' "$C")"
        MYSQLD=$($DOCKER exec "$C" sh -c 'command -v mariadbd || command -v mysqld' 2>/dev/null | tail -n1)
        fact mysqld_bin "$MYSQLD"
        [ -n "$MYSQLD" ] && fact mysqld_version "$($DOCKER exec "$C" "$MYSQLD" --version 2>/dev/null | head -n1)"
        for d in {dests}; do
            if $DOCKER exec "$C" test -f "$d/{pe.DROPIN_NAME}" 2>/dev/null; then
                fact dropin_b64 "$($DOCKER exec "$C" cat "$d/{pe.DROPIN_NAME}" | base64 | tr -d '\\n')"; break
            fi
        done
    else
        fact container_found 0
    fi
    exit 0
fi
for u in mariadb.service mysql.service mysqld.service; do
    if [ "$(systemctl is-active "$u" 2>/dev/null)" = active ]; then fact unit "$u"; break; fi
done
MYSQLD=$(command -v mariadbd || command -v mysqld || ls /usr/sbin/mariadbd /usr/sbin/mysqld /usr/libexec/mysqld 2>/dev/null | head -n1)
fact mysqld_bin "$MYSQLD"
[ -n "$MYSQLD" ] && fact mysqld_version "$("$MYSQLD" --version 2>/dev/null | head -n1)"
INCL=""
for f in /etc/my.cnf /etc/mysql/my.cnf; do
    [ -f "$f" ] || continue
    r=$(readlink -f "$f")
    INCL="$INCL $(sed -n 's/^[[:space:]]*!includedir[[:space:]]*\\([^[:space:]]*\\).*/\\1/p' "$r" | sed 's#/$##' | tr '\\n' ' ')"
done
fact includedirs "$INCL"
DIRS=""
for d in {dirs}; do [ -d "$d" ] && DIRS="$DIRS $d"; done
fact dropin_dirs "$DIRS"
for d in $DIRS; do
    if [ -f "$d/{pe.DROPIN_NAME}" ]; then fact dropin_path "$d/{pe.DROPIN_NAME}"; fact dropin_b64 "$(base64 < "$d/{pe.DROPIN_NAME}" | tr -d '\\n')"; break; fi
done
MPD=$(command -v my_print_defaults || command -v mariadb-print-defaults)
fact my_print_defaults "$MPD"
if [ -n "$MPD" ]; then
    # MariaDB: --mysqld (all server groups); MySQL 8.4 dropped that option: name the groups
    LE=$( {{ "$MPD" --mysqld 2>/dev/null || "$MPD" mysqld server 2>/dev/null; }} | sed -n 's/^--log[-_]error=//p' | tail -n1)
    fact error_log "$LE"
fi
# [client] sections that set default-character-set (mysqlbinlog rejects it)
FILES=""; VAL=""
for f in /etc/my.cnf /etc/mysql/my.cnf $(for d in $INCL $DIRS; do ls "$d"/*.cnf 2>/dev/null; done); do
    [ -f "$f" ] || continue
    r=$(readlink -f "$f")
    case " $FILES " in *" $r "*) continue ;; esac
    v=$(awk '/^[[:space:]]*\\[/ {{ s=tolower($0); gsub(/[][[:space:]]/, "", s) }}
             s == "client" && /^[[:space:]]*default[-_]character[-_]set[[:space:]]*=/ {{ sub(/^[^=]*=[[:space:]]*/, ""); print; exit }}' "$r")
    if [ -n "$v" ]; then FILES="$FILES $r"; VAL="$v"; fi
done
fact charset_files "$FILES"
fact charset_value "$VAL"
exit 0
'''


def mysql_sql_script(db_config, statements, client_image='', admin=None, query=None):
    """Run statements one by one (stop at the first failure); optionally a SELECT whose rows are printed.

    admin: None (the job's login), {'mode': 'password', ...} (one-time login, 0600 file, trap-removed)
    or {'mode': 'socket'} (the server's root over the local socket: `sudo mysql`, host installs).
    """
    stmts = '\n'.join(f'SQL_{i}={q(s)}' for i, s in enumerate(statements))
    body = f'''{stmts}
N={len(statements)}
i=0
while [ "$i" -lt "$N" ]; do
    eval "S=\\$SQL_$i"
    if OUT=$(run_q "$S" 2>&1); then fact sql "$i ok"; else
        fact sql "$i fail"
        fact sql_error "$i $(printf '%s' "$OUT" | grep -v 'Using a password' | head -c 400 | tr '\\n' ' ')"
        exit 0
    fi
    i=$((i + 1))
done
'''
    if query:
        body += f'''run_q {q(query)} 2>"$CONF_DIR/qerr" | line row || true
'''
    if admin and admin.get('mode') == 'socket':
        return HEAD + f'''CONF_DIR=$(mktemp -d); trap 'rm -rf "$CONF_DIR"' EXIT
MYSQL=$(command -v mariadb || command -v mysql)
[ -n "$MYSQL" ] || {{ fact connect_error "no mysql client on the server"; exit 0; }}
run_q() {{ $SUDO "$MYSQL" --protocol=socket -uroot -N -B -e "$1"; }}
''' + body + 'exit 0\n'
    return HEAD + _client_setup(db_config, client_image, admin) + '''run_q() { pitr_q "$1"; }
''' + body + 'exit 0\n'


def mysql_backup_config_script(tag, target):
    """tar the MySQL configuration into CONFIG_BACKUP_ROOT/<tag>/config.tgz (root, 0700)."""
    root = f'{pe.CONFIG_BACKUP_ROOT}/{tag}'
    if target['install'] == 'docker':
        src = target['config_path'].rsplit('/', 1)[0]
        paths = q(src)
    else:
        paths = '/etc/mysql /etc/my.cnf /etc/my.cnf.d'
    return HEAD + f'''$SUDO mkdir -p {q(root)} && $SUDO chmod 700 {pe.CONFIG_BACKUP_ROOT} {q(root)} || {{ fact error "can't create {root}"; exit 1; }}
P=""; for p in {paths}; do [ -e "$p" ] && P="$P $p"; done
$SUDO tar -czf {q(root + '/config.tgz')} $P 2>/dev/null || {{ fact error "tar failed"; exit 1; }}
fact backup {q(root + '/config.tgz')}
exit 0
'''


def write_file_script(tag, path, content, mode='644', owner='root:root'):
    """Save the previous version (or an 'absent' marker) under the backup dir, then write `path` atomically.

    An existing file keeps its owner and mode; a new one gets owner/mode.
    """
    root = f'{pe.CONFIG_BACKUP_ROOT}/{tag}'
    key = path.strip('/').replace('/', '__')
    return HEAD + f'''P={q(path)}
B={q(root + '/' + key)}
$SUDO mkdir -p {q(root)} && $SUDO chmod 700 {q(root)} || {{ fact error "can't create {root}"; exit 1; }}
if [ -e "$B.prev" ] || [ -e "$B.absent" ]; then :   # first version of this run already saved
elif $SUDO test -f "$P"; then $SUDO cp -p "$P" "$B.prev" || {{ fact error "can't back up $P"; exit 1; }}
else $SUDO touch "$B.absent"; fi
CONTENT={q(content)}
printf '%s' "$CONTENT" | $SUDO sh -c 'umask 022; cat > "$1"' sh "$P.backupx-new" || {{ fact error "can't write $P"; exit 1; }}
if $SUDO test -f "$P"; then
    $SUDO chown --reference="$P" "$P.backupx-new" 2>/dev/null; $SUDO chmod --reference="$P" "$P.backupx-new" 2>/dev/null
else
    $SUDO chown {owner} "$P.backupx-new" 2>/dev/null; $SUDO chmod {mode} "$P.backupx-new"
fi
$SUDO mv -f "$P.backupx-new" "$P" || {{ fact error "can't move $P into place"; exit 1; }}
fact wrote "$P"
exit 0
'''


def restore_file_script(tag, path):
    root = f'{pe.CONFIG_BACKUP_ROOT}/{tag}'
    key = path.strip('/').replace('/', '__')
    return HEAD + f'''P={q(path)}
B={q(root + '/' + key)}
if $SUDO test -f "$B.prev"; then $SUDO cp -p "$B.prev" "$P" && fact restored "$P"
elif $SUDO test -e "$B.absent"; then $SUDO rm -f "$P" && fact removed "$P"
else fact error "no backup of $P in {root}"; fi
exit 0
'''


def mysql_validate_script(target):
    """Validate the configuration as the server would read it, without starting it.

    MySQL 8.0.16+: mysqld --validate-config. Older / MariaDB: my_print_defaults --mysqld and
    mysqld --help --verbose (parses every option file and exits non-zero on unknown options).
    Prints the effective [mysqld] options (passwords masked) for the override check.
    """
    v = pe.server_version_tuple(target.get('version')) or (0, 0, 0)
    validate = target['flavor'] == 'mysql' and v >= (8, 0, 16)
    if target['install'] == 'docker':
        run = f'$DOCKER exec {q(target["container"])}'
        need_docker = '[ -n "$DOCKER" ] || { fact valid 0; fact verr "docker not usable"; exit 0; }\n'
    else:
        run = '$SUDO'
        need_docker = ''
    return HEAD + need_docker + f'''MYSQLD=$({run} sh -c 'command -v mariadbd || command -v mysqld || ls /usr/sbin/mariadbd /usr/sbin/mysqld 2>/dev/null | head -n1' | tail -n1)
MPD=$({run} sh -c 'command -v my_print_defaults || command -v mariadb-print-defaults' | tail -n1)
[ -n "$MYSQLD" ] || {{ fact valid 0; fact verr "mysqld not found"; exit 0; }}
if [ -n "$MPD" ]; then
    {{ {run} "$MPD" --mysqld 2>/dev/null || {run} "$MPD" mysqld server 2>/dev/null; }} | {MASK} | line pd
fi
ERR=$(mktemp)
if {'true' if validate else 'false'}; then
    {run} "$MYSQLD" --validate-config --user=mysql >"$ERR" 2>&1; RC=$?
else
    {run} "$MYSQLD" --help --verbose --user=mysql 2>"$ERR" >/dev/null; RC=$?
    grep -qiE 'unknown (variable|option)|\\[ERROR\\]' "$ERR" && RC=${{RC:-1}} && [ "$RC" = 0 ] && RC=1
fi
if [ "$RC" = 0 ]; then fact valid 1; else fact valid 0; grep -v '^$' "$ERR" | head -n 20 | {MASK} | line verr; fi
rm -f "$ERR"
exit 0
'''


def mysql_restart_script(target):
    if target['install'] == 'docker':
        return HEAD + f'''[ -n "$DOCKER" ] || {{ fact error "docker not usable"; exit 1; }}
fact since "$(date +%s)"
$DOCKER restart -t 120 {q(target['container'])} >/dev/null 2>&1 || {{ fact error "docker restart failed"; exit 1; }}
fact restarted 1
exit 0
'''
    unit = q(target['service'])
    return HEAD + f'''fact since "$(date +%s)"
$SUDO systemctl reset-failed {unit} >/dev/null 2>&1
$SUDO systemctl restart --no-block {unit} || {{ fact error "systemctl restart failed"; exit 1; }}
fact restarted 1
exit 0
'''


def mysql_status_script(target, db_config, client_image, names, since, error_log=''):
    """One health poll: service/container state, recent log lines, connect + current settings."""
    if target['install'] == 'docker':
        c = q(target['container'])
        state = f'''S=$($DOCKER inspect -f '{{{{.State.Status}}}}|{{{{.State.Restarting}}}}|{{{{.RestartCount}}}}' {c} 2>/dev/null)
fact state "$(printf '%s' "$S" | cut -d'|' -f1)"
fact restarting "$(printf '%s' "$S" | cut -d'|' -f2)"
fact restart_count "$(printf '%s' "$S" | cut -d'|' -f3)"
fact started_at "$(date -d "$($DOCKER inspect -f '{{{{.State.StartedAt}}}}' {c} 2>/dev/null)" +%s 2>/dev/null)"
$DOCKER logs --since {int(since)} {c} 2>&1 | tail -n 40 | line log
'''
    else:
        u = q(target['service'])
        log = (f'''LE={q(error_log)}
if [ -n "$LE" ] && $SUDO test -f "$LE" && [ "$($SUDO stat -c %Y "$LE")" -ge {int(since)} ]; then $SUDO tail -n 40 "$LE" | line log; fi
''' if error_log else '')
        state = f'''fact state "$(systemctl show -p ActiveState --value {u})"
fact sub "$(systemctl show -p SubState --value {u})"
fact restart_count "$(systemctl show -p NRestarts --value {u} 2>/dev/null)"
T=$(systemctl show -p ExecMainStartTimestamp --value {u}); [ -n "$T" ] && fact started_at "$(date -d "$T" +%s 2>/dev/null)"
$SUDO journalctl -u {u} --since @{int(since)} -o cat --no-pager 2>/dev/null | tail -n 40 | line log
''' + log
    select = '; '.join(f"SELECT 'var_{n}', @@{n}" for n in names) or "SELECT 'var_ping', 1"
    return HEAD + state + _client_setup(db_config, client_image) + f'''OUT=$(pitr_q {q(select)} 2>"$CONF_DIR/qerr")
if [ $? = 0 ] && [ -n "$OUT" ]; then
    fact connected 1
    printf '%s\\n' "$OUT" | while IFS="$(printf '\\t')" read -r k v; do [ -n "$k" ] && fact "$k" "$v"; done
else
    fact connected 0
    fact connect_error "$(grep -v 'Using a password' "$CONF_DIR/qerr" | head -c 300 | tr '\\n' ' ')"
fi
exit 0
'''


def charset_fix_script(tag, files, value, dropin_dir):
    """Comment out default-character-set in [client] of `files` (backed up) and set it for the
    client programs that use it in our own drop-in. Verifies mysqlbinlog starts with the host
    option files; restores everything if it doesn't."""
    root = f'{pe.CONFIG_BACKUP_ROOT}/{tag}'
    dropin = f'{dropin_dir.rstrip("/")}/{pe.CHARSET_DROPIN_NAME}'
    return HEAD + f'''R={q(root)}
$SUDO mkdir -p "$R" && $SUDO chmod 700 "$R" || {{ fact error "can't create $R"; exit 1; }}
DROPIN={q(dropin)}
restore_all() {{
    for f in {' '.join(q(f) for f in files)}; do
        k=$(printf '%s' "$f" | sed 's#^/##; s#/#__#g')
        $SUDO test -f "$R/$k.prev" && $SUDO cp -p "$R/$k.prev" "$f"
    done
    $SUDO rm -f "$DROPIN"
}}
for f in {' '.join(q(f) for f in files)}; do
    k=$(printf '%s' "$f" | sed 's#^/##; s#/#__#g')
    $SUDO test -f "$R/$k.prev" || $SUDO cp -p "$f" "$R/$k.prev" || {{ fact error "can't back up $f"; exit 1; }}
    TMP=$(mktemp)
    $SUDO cat "$f" | awk '/^[[:space:]]*\\[/ {{ s=tolower($0); gsub(/[][[:space:]]/, "", s) }}
        s == "client" && /^[[:space:]]*default[-_]character[-_]set[[:space:]]*=/ {{ print "# " $0 "   # moved by backupx to {pe.CHARSET_DROPIN_NAME}"; next }}
        {{ print }}' > "$TMP"
    $SUDO sh -c 'cat "$1" > "$2"' sh "$TMP" "$f" || {{ rm -f "$TMP"; restore_all; fact error "can't edit $f"; exit 1; }}
    rm -f "$TMP"
    fact edited "$f"
done
CONTENT={q(pe.charset_dropin(value))}
printf '%s' "$CONTENT" | $SUDO sh -c 'umask 022; cat > "$1"' sh "$DROPIN" || {{ restore_all; fact error "can't write $DROPIN"; exit 1; }}
fact wrote "$DROPIN"
B=$(command -v mariadb-binlog || command -v mysqlbinlog)
if [ -n "$B" ] && ! OUT=$("$B" --version 2>&1); then
    restore_all
    fact error "mysqlbinlog still fails with the host option files: $(printf '%s' "$OUT" | head -c 200 | tr '\\n' ' ')"
    exit 0
fi
M=$(command -v mariadb || command -v mysql)
if [ -n "$M" ] && ! "$M" --help >/dev/null 2>&1; then
    restore_all; fact error "the mysql client fails with the edited option files"; exit 0
fi
fact ok 1
exit 0
'''


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

def pg_host_probe_script():
    return HEAD + ps._AS_PG + '\n' + ps.ROOT_FACTS + r'''
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then fact systemd 1; else fact systemd 0; fi
Q() { printf '%s' "$1" | as_pg 'psql -XtA' 2>/dev/null; }
DD=$(Q 'SHOW data_directory'); fact data_directory "$DD"
CF=$(Q 'SHOW config_file'); fact config_file "$CF"
fact server_version_num "$(Q 'SHOW server_version_num')"
AAS=$(Q 'SHOW allow_alter_system'); [ -n "$AAS" ] && fact allow_alter_system "$AAS"
if [ -n "$CF" ]; then
    INC=$($SUDO sed -n "s/^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'\{0,1\}\([^' #]*\).*/\1/p" "$CF" | tail -n1)
    if [ -n "$INC" ]; then case "$INC" in /*) ;; *) INC="$(dirname "$CF")/$INC" ;; esac; fact include_dir "$INC"; fi
fi
if [ -n "$DD" ]; then
    $SUDO test -f "$DD/postgresql.auto.conf" && fact auto_conf_b64 "$($SUDO cat "$DD/postgresql.auto.conf" | base64 | tr -d '\n')"
    PID=$($SUDO head -n1 "$DD/postmaster.pid" 2>/dev/null)
    [ -n "$PID" ] && fact postgres_bin "$($SUDO readlink -f "/proc/$PID/exe" 2>/dev/null)"
fi
UNIT=""
for u in $(systemctl list-units --type=service --state=active --plain --no-legend 'postgresql*' 2>/dev/null | awk '{print $1}'); do
    case "$u" in postgresql@*) UNIT="$u"; break ;; esac
    [ -z "$UNIT" ] && UNIT="$u"
done
fact unit "$UNIT"
LOG=$(ls -t /var/log/postgresql/*.log 2>/dev/null | head -n1)
[ -z "$LOG" ] && [ -n "$DD" ] && LOG=$($SUDO sh -c "ls -t '$DD'/log/*.log '$DD'/pg_log/*.log 2>/dev/null" | head -n1)
fact log_file "$LOG"
exit 0
'''


def pg_sql_script(statements, query=None):
    stmts = '\n'.join(f'SQL_{i}={q(s)}' for i, s in enumerate(statements))
    body = f'''{stmts}
N={len(statements)}
i=0
while [ "$i" -lt "$N" ]; do
    eval "S=\\$SQL_$i"
    if OUT=$(printf '%s\\n' "$S" | as_pg 'psql -XtA -v ON_ERROR_STOP=1' 2>&1); then fact sql "$i ok"; else
        fact sql "$i fail"; fact sql_error "$i $(printf '%s' "$OUT" | head -c 400 | tr '\\n' ' ')"; exit 0
    fi
    i=$((i + 1))
done
'''
    if query:
        body += f'''printf '%s\\n' {q(query)} | as_pg 'psql -XtA -F "	"' 2>/dev/null | line row
'''
    return HEAD + ps._AS_PG + '\n' + body + 'exit 0\n'


def pg_backup_config_script(tag, data_directory, config_file):
    root = f'{pe.CONFIG_BACKUP_ROOT}/{tag}'
    conf_dir = (config_file or '').rsplit('/', 1)[0]
    return HEAD + f'''$SUDO mkdir -p {q(root)} && $SUDO chmod 700 {pe.CONFIG_BACKUP_ROOT} {q(root)} || {{ fact error "can't create {root}"; exit 1; }}
P=""
for p in {q(data_directory + '/postgresql.auto.conf')} {q(conf_dir)}; do [ -e "$p" ] && P="$P $p"; done
$SUDO tar -czf {q(root + '/config.tgz')} $P 2>/dev/null || {{ fact error "tar failed"; exit 1; }}
fact backup {q(root + '/config.tgz')}
exit 0
'''


def pg_validate_script(postgres_bin, config_file, names):
    """pg_file_settings errors (re-reads the files now) + `postgres -C` for each setting."""
    conf_dir = (config_file or '').rsplit('/', 1)[0]
    checks = '\n'.join(
        f'''V=$(as_pg {q(f'{postgres_bin} -D {conf_dir} -C {n}')} 2>"$ERR") && fact c_{n} "$V" || {{ fact valid 0; head -n 5 "$ERR" | line verr; }}'''
        for n in names)
    return HEAD + ps._AS_PG + f'''
ERR=$(mktemp); chmod 644 "$ERR"; trap 'rm -f "$ERR"' EXIT
E=$(printf '%s' "SELECT coalesce(sourcefile,'?') || ':' || coalesce(sourceline::text,'?') || ' ' || coalesce(name,'') || ': ' || error FROM pg_file_settings WHERE error IS NOT NULL" | as_pg 'psql -XtA' 2>&1)
if [ -n "$E" ]; then fact valid 0; printf '%s\\n' "$E" | line verr; exit 0; fi
{checks if postgres_bin and conf_dir else ''}
fact valid_done 1
exit 0
'''


def pg_restart_script(unit):
    return HEAD + f'''fact since "$(date +%s)"
$SUDO systemctl reset-failed {q(unit)} >/dev/null 2>&1
$SUDO systemctl restart --no-block {q(unit)} || {{ fact error "systemctl restart failed"; exit 1; }}
fact restarted 1
exit 0
'''


def pg_status_script(unit, names, since, log_file=''):
    u = q(unit)
    names_sql = ', '.join("'" + n + "'" for n in names) or "'wal_level'"
    log = (f'''LF={q(log_file)}
if [ -n "$LF" ] && $SUDO test -f "$LF" && [ "$($SUDO stat -c %Y "$LF")" -ge {int(since)} ]; then $SUDO tail -n 40 "$LF" | line log; fi
''' if log_file else '')
    return HEAD + ps._AS_PG + f'''
fact state "$(systemctl show -p ActiveState --value {u})"
fact sub "$(systemctl show -p SubState --value {u})"
fact restart_count "$(systemctl show -p NRestarts --value {u} 2>/dev/null)"
T=$(systemctl show -p ExecMainStartTimestamp --value {u}); [ -n "$T" ] && fact started_at "$(date -d "$T" +%s 2>/dev/null)"
$SUDO journalctl -u {u} --since @{int(since)} -o cat --no-pager 2>/dev/null | tail -n 20 | line log
{log}OUT=$(printf '%s' "SELECT 'var_' || name, setting FROM pg_settings WHERE name IN ({names_sql})" | as_pg 'psql -XtA -F "	"' 2>&1)
if [ $? = 0 ] && [ -n "$OUT" ]; then
    fact connected 1
    printf '%s\\n' "$OUT" | while IFS="$(printf '\\t')" read -r k v; do [ -n "$k" ] && fact "$k" "$v"; done
else
    fact connected 0; fact connect_error "$(printf '%s' "$OUT" | head -c 300 | tr '\\n' ' ')"
fi
exit 0
'''
