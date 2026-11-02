"""
Gentle process stop (app/procs.py) and read-only restic calls without locks.

A restic killed with SIGKILL can't remove its repository lock; run_gently
sends SIGINT first. Listings (snapshots, stats, ls, find) run with --no-lock.
"""
import os
import subprocess
import sys
import time

import pytest

from app import procs

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX signals')


def _script(tmp_path, body):
    path = tmp_path / 'child.py'
    path.write_text(body)
    return [sys.executable, str(path)]


def test_run_gently_returns_output_like_subprocess_run():
    res = procs.run_gently([sys.executable, '-c', 'import sys; print("out"); print("err", file=sys.stderr); sys.exit(3)'],
                           timeout=30)
    assert res.returncode == 3 and res.stdout.strip() == 'out' and res.stderr.strip() == 'err'


def test_run_gently_passes_input():
    res = procs.run_gently([sys.executable, '-c', 'import sys; print(sys.stdin.read().upper())'],
                           timeout=30, input='hello')
    assert res.stdout.strip() == 'HELLO'


@posix_only
def test_timeout_sends_sigint_first_so_restic_can_drop_its_lock(tmp_path):
    marker = tmp_path / 'lock'
    marker.write_text('held')
    # Stands in for restic: on SIGINT it removes its lock file and exits 130
    cmd = _script(tmp_path, f"""
import signal, sys, time, os
def stop(*_):
    os.remove({str(marker)!r})
    print('lock removed', flush=True)
    sys.exit(130)
signal.signal(signal.SIGINT, stop)
print('started', flush=True)
time.sleep(60)
""")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        procs.run_gently(cmd, timeout=1, grace=10)
    assert time.monotonic() - t0 < 8
    assert not marker.exists()
    assert 'lock removed' in (exc.value.output or '')


@posix_only
def test_process_ignoring_sigint_is_terminated_then_killed(tmp_path):
    cmd = _script(tmp_path, """
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('started', flush=True)
time.sleep(60)
""")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run_gently(cmd, timeout=1, grace=0.5)
    # 1 s timeout + 0.5 s SIGINT grace + 5 s SIGTERM grace, then SIGKILL
    assert time.monotonic() - t0 < 12


@posix_only
def test_stop_gently_on_a_finished_process_is_a_noop():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    procs.stop_gently(proc)
    assert proc.returncode == 0


def test_task_stream_timeout_stops_the_process_gently(monkeypatch):
    from app import tasks
    calls = []
    monkeypatch.setattr(procs, 'stop_gently', lambda proc, grace=None, term_grace=5.0: calls.append(proc) or proc.kill())
    ctx = tasks.TaskContext.__new__(tasks.TaskContext)
    ctx.cancel_event = __import__('threading').Event()
    ctx.log = lambda *a, **k: None
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with pytest.raises(subprocess.TimeoutExpired):
        tasks.TaskContext.stream(ctx, proc, timeout=1)
    assert calls == [proc]


# --- read-only restic calls -----------------------------------------------------------

class FakeRun:
    def __init__(self, stdout='[]', exc=None):
        self.calls = []
        self.stdout = stdout
        self.exc = exc

    def __call__(self, cmd, timeout=None, env=None, **kw):
        self.calls.append({'cmd': list(cmd), 'timeout': timeout})
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, 0, self.stdout, '')


def _job_target():
    job = {'id': 'pxjob', 'name': 'Files', 'restic_password': 'pw', 'backup_prefix': 'pxjob'}
    target = {'id': 'pxst', 'name': 'Primary', 'endpoint': 's3.example.com', 'bucket': 'b',
              'access_key': 'AK', 'secret_key': 'SK', 'skip_ssl_verify': True}
    return job, target


def test_snapshot_listing_and_stats_run_without_a_lock(monkeypatch):
    import app.main as main
    fake = FakeRun('[{"id": "a", "time": "2026-11-01T00:00:00Z"}]')
    monkeypatch.setattr(main.procs, 'run_gently', fake)
    monkeypatch.setattr(main, 'job_restic_env', lambda job, target: {})
    main.invalidate_snapshot_cache('pxjob')
    job, target = _job_target()
    assert main.get_snapshots(job, target)[0]['id'] == 'a'
    fake.stdout = '{"total_size": 1}'
    assert main.get_repo_stats(job, target) == {'total_size': 1}
    assert [c['cmd'][:2] for c in fake.calls] == [['restic', 'snapshots'], ['restic', 'stats']]
    for call in fake.calls:
        assert '--no-lock' in call['cmd'] and '--insecure-tls' in call['cmd']
        assert call['timeout'] == main.restic_read_timeout()
    main.invalidate_snapshot_cache('pxjob')


def test_snapshot_listing_timeout_falls_back_to_the_cached_list(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'job_restic_env', lambda job, target: {})
    main.invalidate_snapshot_cache('pxjob')
    job, target = _job_target()
    fake = FakeRun('[{"id": "a", "time": "2026-11-01T00:00:00Z"}]')
    monkeypatch.setattr(main.procs, 'run_gently', fake)
    assert main.get_snapshots(job, target)[0]['id'] == 'a'
    # cache expired, restic now times out: the last list is shown instead of an error
    key = ('pxjob', 'pxst')
    main._snapshot_cache[key] = (main._snapshot_cache[key][0] - 3600, main._snapshot_cache[key][1])
    monkeypatch.setattr(main.procs, 'run_gently', FakeRun(exc=subprocess.TimeoutExpired('restic', 180)))
    assert main.get_snapshots(job, target)[0]['id'] == 'a'
    # nothing cached: a clear error
    main.invalidate_snapshot_cache('pxjob')
    with pytest.raises(RuntimeError, match='timed out'):
        main.get_snapshots(job, target)


def test_restic_read_timeout_is_configurable(monkeypatch):
    import app.main as main
    monkeypatch.setenv('RESTIC_READ_TIMEOUT', '600')
    assert main.restic_read_timeout() == 600
    monkeypatch.setenv('RESTIC_READ_TIMEOUT', 'x')
    assert main.restic_read_timeout() == 180


def test_every_local_restic_listing_uses_no_lock():
    """Source check: read-only restic subcommands run here always pass --no-lock."""
    import re
    root = os.path.join(os.path.dirname(__file__), '..', 'app')
    readonly = ('snapshots', 'ls', 'find', 'stats', 'cat', 'list', 'key list')
    offenders = []
    for name in ('main.py', 'health_service.py', 'restore_service.py', 'pitr_service.py'):
        text = open(os.path.join(root, name), encoding='utf-8').read()
        for m in re.finditer(r"\[\s*'restic',\s*'(\w+)'[^\]]*\]", text):
            if m.group(1) in readonly and '--no-lock' not in m.group(0):
                offenders.append(f'{name}: {m.group(0)[:80]}')
        for m in re.finditer(r"_restic(?:_local)?\(job, target, \[\s*'(\w+)'[^\]]*\]", text):
            if m.group(1) in readonly and '--no-lock' not in m.group(0):
                offenders.append(f'{name}: {m.group(0)[:80]}')
    assert offenders == []
