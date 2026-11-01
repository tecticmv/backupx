"""
Step-up re-authentication and confirmed plans for risky actions.

Tiers (docs/UX-OVERHAUL.md):
- safe: one click, no extra checks.
- confirm / maintenance: the server derives an exact *plan* (what will change,
  expected downtime, what is rolled back on failure), shows it with its hash
  and a phrase to type; the client sends back the hash and the typed phrase.
  The server derives the plan again from current state and refuses if the
  hash no longer matches (state changed since the user looked) or the phrase
  is wrong. These actions also need a recent step-up: the admin re-entered
  the password within STEP_UP_TTL seconds (default 300).

Step-up state lives in the Flask session (server-side, filesystem/redis), so
it ends with the session and is per browser.
"""

import hashlib
import hmac
import json
import os
import time
from functools import wraps
from typing import Any, Callable, Optional

from flask import jsonify, session

STEP_UP_SESSION_KEY = 'step_up_at'


def step_up_ttl() -> int:
    try:
        return max(30, int(os.environ.get('STEP_UP_TTL', 300)))
    except (TypeError, ValueError):
        return 300


def grant_step_up(now: Optional[float] = None) -> float:
    """Record a successful password re-entry; returns the epoch it stays valid until."""
    now = time.time() if now is None else now
    session[STEP_UP_SESSION_KEY] = now
    session.modified = True
    return now + step_up_ttl()


def clear_step_up():
    session.pop(STEP_UP_SESSION_KEY, None)


def step_up_valid_until(now: Optional[float] = None) -> Optional[float]:
    """Epoch until which the step-up holds, or None if there is none / it expired."""
    at = session.get(STEP_UP_SESSION_KEY)
    if not at:
        return None
    try:
        until = float(at) + step_up_ttl()
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return until if now < until else None


def has_step_up() -> bool:
    return step_up_valid_until() is not None


def step_up_required_response():
    return jsonify({
        'error': 'Re-enter your password to continue',
        'code': 'step_up_required',
        'ttl': step_up_ttl(),
    }), 403


def requires_step_up(fn: Callable) -> Callable:
    """Route decorator (after @login_required): 403 step_up_required without a fresh step-up."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not has_step_up():
            return step_up_required_response()
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Plans and typed confirmation
# ---------------------------------------------------------------------------

def _canonical(plan: Any) -> bytes:
    return json.dumps(plan, sort_keys=True, separators=(',', ':'), default=str).encode()


def plan_hash(plan: Any, key: Optional[str] = None) -> str:
    """HMAC-SHA256 (SECRET_KEY) of the plan's canonical JSON, hex.

    Deterministic for the same plan content; any change to the plan (a value,
    a step, the downtime estimate) changes it.
    """
    key = key if key is not None else os.environ.get('SECRET_KEY', '')
    return hmac.new(key.encode(), _canonical(plan), hashlib.sha256).hexdigest()


def present_plan(plan: dict, confirm_text: Optional[str] = None) -> dict:
    """What a /plan endpoint returns: the plan, its hash and the phrase to type."""
    out = {'plan': plan, 'plan_hash': plan_hash(plan)}
    if confirm_text:
        out['confirm_text'] = confirm_text
    return out


class ConfirmationError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def verify_confirmation(plan: dict, submitted_hash: Optional[str], typed: Optional[str] = None,
                        confirm_text: Optional[str] = None):
    """Check a client's confirmation against the freshly re-derived plan.

    Raises ConfirmationError:
      - plan_hash_missing (400): no hash sent
      - plan_changed (409): the plan differs from the one the user confirmed
      - confirmation_mismatch (400): typed phrase wrong (exact match, trimmed)
    """
    if not submitted_hash:
        raise ConfirmationError('plan_hash_missing', 'Confirm the plan first (plan_hash missing)', 400)
    if not hmac.compare_digest(str(submitted_hash), plan_hash(plan)):
        raise ConfirmationError('plan_changed',
                                'The plan changed since you reviewed it; review the new plan and confirm again')
    if confirm_text is not None:
        if (typed or '').strip() != confirm_text.strip():
            raise ConfirmationError('confirmation_mismatch', f'Type "{confirm_text}" to confirm', 400)


def confirmation_error_response(err: ConfirmationError, plan: Optional[dict] = None,
                                confirm_text: Optional[str] = None):
    body = {'error': str(err), 'code': err.code}
    if err.code == 'plan_changed' and plan is not None:
        body.update(present_plan(plan, confirm_text))
    return jsonify(body), err.status


def check_confirmed(plan: dict, data: dict, confirm_text: Optional[str] = None):
    """For confirm/maintenance routes: None if OK, else a Flask error response.

    Usage in a route (after @login_required @requires_step_up):
        plan = derive_plan(...)            # from current state, never from the request
        error = check_confirmed(plan, request.get_json() or {}, confirm_text=name)
        if error: return error
    """
    try:
        verify_confirmation(plan, (data or {}).get('plan_hash'), (data or {}).get('confirm'), confirm_text)
    except ConfirmationError as e:
        return confirmation_error_response(e, plan, confirm_text)
    return None
