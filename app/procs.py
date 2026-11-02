"""Running local processes (mostly restic) so a timeout doesn't leave a restic lock behind.

subprocess.run(timeout=...) SIGKILLs the child on timeout, and a SIGKILLed
restic can't remove the lock file it created in the repository: the lock
stays until someone runs `restic unlock`. Here a timed-out process gets SIGINT
first (restic then removes its lock and exits), SIGTERM after `grace`
seconds, SIGKILL as the last resort.
"""
import os
import signal
import subprocess

_REAL_RUN = subprocess.run


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def stop_grace():
    """Seconds a process gets after SIGINT before SIGTERM (`PROC_STOP_GRACE`, default 10)."""
    return max(0.0, _env_float('PROC_STOP_GRACE', 10))


def stop_gently(proc, grace=None, term_grace=5.0):
    """SIGINT, wait `grace` s, SIGTERM, wait `term_grace` s, SIGKILL. Never raises."""
    grace = stop_grace() if grace is None else grace
    signals = ((signal.SIGINT, grace), (signal.SIGTERM, term_grace)) if hasattr(proc, 'send_signal') else ()
    for sig, wait in signals:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(sig)
        except (OSError, ValueError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def run_gently(cmd, timeout=None, *, env=None, text=True, input=None, capture_output=True, grace=None, **kwargs):
    """subprocess.run(cmd, capture_output=True, ...) that stops a timed-out process gently.

    Same result (CompletedProcess) and the same subprocess.TimeoutExpired on
    timeout, with whatever output the process wrote (also after SIGINT).
    """
    if subprocess.run is not _REAL_RUN:
        # Tests replace subprocess.run with fakes that record every command
        # (e.g. that no secret is in argv); keep them seeing these calls too.
        return subprocess.run(cmd, timeout=timeout, env=env, text=text, input=input,
                              capture_output=capture_output, **kwargs)
    pipe = subprocess.PIPE if capture_output else None
    stdin = subprocess.PIPE if input is not None else kwargs.pop('stdin', None)
    with subprocess.Popen(cmd, stdin=stdin, stdout=pipe, stderr=pipe, text=text, env=env, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_gently(proc, grace)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = None, None
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        except BaseException:
            stop_gently(proc, grace)
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
