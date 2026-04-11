"""
Audit logging decorator for Flask routes.
"""

import logging
from functools import wraps
from typing import Callable, Optional

from flask import request, g
from flask_login import current_user

from .logger import get_audit_logger, AuditLogger

logger = logging.getLogger(__name__)


def audit(
    action: str,
    resource_type: str,
    get_resource_id: Optional[Callable] = None,
    get_resource_name: Optional[Callable] = None,
    get_old_value: Optional[Callable] = None,
    get_new_value: Optional[Callable] = None
):
    """
    Decorator for automatic audit logging of route handlers.

    Args:
        action: Action type (CREATE, UPDATE, DELETE, RUN_BACKUP, etc.)
        resource_type: Resource type (job, server, s3_config, etc.)
        get_resource_id: Callable to extract resource ID from request/kwargs
        get_resource_name: Callable to extract resource name
        get_old_value: Callable to get the old value before operation
        get_new_value: Callable to get the new value after operation

    Usage:
        @app.route('/api/jobs/<job_id>', methods=['DELETE'])
        @audit(
            action=AuditLogger.ACTION_DELETE,
            resource_type=AuditLogger.RESOURCE_JOB,
            get_resource_id=lambda: request.view_args.get('job_id'),
            get_resource_name=lambda: get_job(request.view_args.get('job_id')).get('name')
        )
        def delete_job(job_id):
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            audit_logger = get_audit_logger()

            # Extract audit context before operation
            resource_id = None
            resource_name = None
            old_value = None
            user_id = None
            user_name = None
            ip_address = request.remote_addr
            user_agent = request.headers.get('User-Agent', '')[:500]

            # Get current user info
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
                user_name = current_user.id

            # Get resource ID
            if get_resource_id:
                try:
                    resource_id = get_resource_id()
                except Exception as e:
                    logger.debug(f"Could not get resource_id: {e}")

            # Get resource name
            if get_resource_name:
                try:
                    resource_name = get_resource_name()
                except Exception as e:
                    logger.debug(f"Could not get resource_name: {e}")

            # Get old value for UPDATE/DELETE
            if get_old_value and action in (AuditLogger.ACTION_UPDATE, AuditLogger.ACTION_DELETE):
                try:
                    old_value = get_old_value()
                except Exception as e:
                    logger.debug(f"Could not get old_value: {e}")

            # Execute the actual route handler
            error_message = None
            status = 'success'

            try:
                result = f(*args, **kwargs)

                # Check if response indicates failure
                if hasattr(result, '__iter__') and len(result) == 2:
                    response, status_code = result
                    if status_code >= 400:
                        status = 'failure'
                        if hasattr(response, 'get_json'):
                            error_data = response.get_json()
                            if error_data:
                                error_message = error_data.get('error', str(error_data))

                return result

            except Exception as e:
                status = 'failure'
                error_message = str(e)[:500]
                raise

            finally:
                # Log the audit event
                if audit_logger:
                    new_value = None
                    if get_new_value and action in (AuditLogger.ACTION_CREATE, AuditLogger.ACTION_UPDATE):
                        try:
                            new_value = get_new_value()
                        except Exception as e:
                            logger.debug(f"Could not get new_value: {e}")

                    try:
                        audit_logger.log(
                            action=action,
                            resource_type=resource_type,
                            resource_id=resource_id,
                            resource_name=resource_name,
                            old_value=old_value,
                            new_value=new_value,
                            user_id=user_id,
                            user_name=user_name,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            status=status,
                            error_message=error_message
                        )
                    except Exception as e:
                        logger.error(f"Failed to log audit event: {e}")

        return wrapper
    return decorator


def audit_login(user_id: str, user_name: str, success: bool, ip_address: str, user_agent: str = ''):
    """
    Log a login attempt.

    Args:
        user_id: User ID
        user_name: Username
        success: Whether login was successful
        ip_address: IP address of the request
        user_agent: User agent string
    """
    audit_logger = get_audit_logger()
    if not audit_logger:
        return

    audit_logger.log(
        action=AuditLogger.ACTION_LOGIN if success else AuditLogger.ACTION_LOGIN_FAILED,
        resource_type=AuditLogger.RESOURCE_SESSION,
        resource_id=user_id,
        resource_name=user_name,
        user_id=user_id,
        user_name=user_name,
        ip_address=ip_address,
        user_agent=user_agent[:500] if user_agent else '',
        status='success' if success else 'failure',
        error_message=None if success else 'Invalid credentials'
    )


def audit_logout(user_id: str, user_name: str, ip_address: str, user_agent: str = ''):
    """
    Log a logout event.

    Args:
        user_id: User ID
        user_name: Username
        ip_address: IP address of the request
        user_agent: User agent string
    """
    audit_logger = get_audit_logger()
    if not audit_logger:
        return

    audit_logger.log(
        action=AuditLogger.ACTION_LOGOUT,
        resource_type=AuditLogger.RESOURCE_SESSION,
        resource_id=user_id,
        resource_name=user_name,
        user_id=user_id,
        user_name=user_name,
        ip_address=ip_address,
        user_agent=user_agent[:500] if user_agent else '',
        status='success'
    )


def audit_backup_run(
    job_id: str,
    job_name: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    ip_address: Optional[str] = None,
    triggered_by: str = 'manual'
):
    """
    Log a backup job run initiation.

    Args:
        job_id: Job ID
        job_name: Job name
        user_id: User who triggered the backup (None for scheduled)
        user_name: Username
        ip_address: IP address (None for scheduled)
        triggered_by: 'manual' or 'scheduled'
    """
    audit_logger = get_audit_logger()
    if not audit_logger:
        return

    audit_logger.log(
        action=AuditLogger.ACTION_RUN_BACKUP,
        resource_type=AuditLogger.RESOURCE_JOB,
        resource_id=job_id,
        resource_name=job_name,
        user_id=user_id,
        user_name=user_name or ('scheduler' if triggered_by == 'scheduled' else 'unknown'),
        ip_address=ip_address,
        new_value={'triggered_by': triggered_by},
        status='success'
    )


def audit_backup_complete(
    job_id: str,
    job_name: str,
    success: bool,
    duration: float,
    message: str = ''
):
    """
    Log backup job completion.

    Args:
        job_id: Job ID
        job_name: Job name
        success: Whether backup was successful
        duration: Backup duration in seconds
        message: Status message or error
    """
    audit_logger = get_audit_logger()
    if not audit_logger:
        return

    audit_logger.log(
        action=AuditLogger.ACTION_BACKUP_COMPLETE if success else AuditLogger.ACTION_BACKUP_FAILED,
        resource_type=AuditLogger.RESOURCE_JOB,
        resource_id=job_id,
        resource_name=job_name,
        user_id=None,
        user_name='system',
        new_value={'duration': duration, 'message': message[:500] if message else ''},
        status='success' if success else 'failure',
        error_message=message[:500] if not success and message else None
    )
