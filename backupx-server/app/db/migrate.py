"""
Database migration utilities.
Supports migrating legacy JSON data to PostgreSQL.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_json_to_database(data_dir: str, db) -> None:
    """
    Migrate legacy JSON files to database.
    This is kept for backward compatibility with older installations.

    Args:
        data_dir: Directory containing JSON files
        db: Database backend instance
    """
    from datetime import datetime

    data_path = Path(data_dir)

    # Check if migration is needed
    has_data = False
    for table in ['servers', 's3_configs', 'db_configs', 'jobs', 'history']:
        try:
            count = db.fetchone(f'SELECT COUNT(*) as count FROM {table}')
            if count and count['count'] > 0:
                has_data = True
                break
        except Exception:
            pass

    if has_data:
        logger.debug("Database already has data, skipping JSON migration")
        return

    logger.info("Migrating JSON data to database...")

    # Migrate servers
    servers_file = data_path / 'servers.json'
    if servers_file.exists():
        try:
            with open(servers_file) as f:
                servers = json.load(f)
            for s in servers:
                db.execute('''
                    INSERT INTO servers (id, name, host, connection_type, ssh_port, ssh_user, ssh_key, agent_port, agent_api_key, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (s['id'], s['name'], s['host'], s.get('connection_type', 'ssh'),
                      s.get('ssh_port', 22), s.get('ssh_user'), s.get('ssh_key', '/home/backupx/.ssh/id_rsa'),
                      s.get('agent_port', 8090), s.get('agent_api_key'),
                      s.get('created_at', datetime.now().isoformat()), s.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(servers)} servers")
        except Exception as e:
            logger.error(f"  Error migrating servers: {e}")

    # Migrate S3 configs
    s3_file = data_path / 's3_configs.json'
    if s3_file.exists():
        try:
            with open(s3_file) as f:
                configs = json.load(f)
            for c in configs:
                db.execute('''
                    INSERT INTO s3_configs (id, name, endpoint, bucket, access_key, secret_key, region, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (c['id'], c['name'], c['endpoint'], c['bucket'], c['access_key'], c['secret_key'],
                      c.get('region', ''), c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(configs)} S3 configs")
        except Exception as e:
            logger.error(f"  Error migrating S3 configs: {e}")

    # Migrate database configs
    db_file = data_path / 'db_configs.json'
    if db_file.exists():
        try:
            with open(db_file) as f:
                configs = json.load(f)
            for c in configs:
                db.execute('''
                    INSERT INTO db_configs (id, name, type, host, port, username, password, databases, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (c['id'], c['name'], c.get('type', 'mysql'), c['host'], c.get('port', 3306),
                      c['username'], c['password'], c.get('databases', '*'),
                      c.get('created_at', datetime.now().isoformat()), c.get('updated_at')))
            db.commit()
            logger.info(f"  Migrated {len(configs)} database configs")
        except Exception as e:
            logger.error(f"  Error migrating database configs: {e}")

    # Migrate jobs
    jobs_file = data_path / 'jobs.json'
    if jobs_file.exists():
        try:
            with open(jobs_file) as f:
                jobs = json.load(f)
            for job_id, j in jobs.items():
                db.execute('''
                    INSERT INTO jobs (id, name, backup_type, server_id, s3_config_id, remote_host, ssh_port, ssh_key,
                        s3_endpoint, s3_bucket, s3_access_key, s3_secret_key, directories, excludes, database_config_id,
                        restic_password, backup_prefix, schedule_enabled, schedule_cron, retention_hourly, retention_daily,
                        retention_weekly, retention_monthly, timeout, status, created_at, updated_at, last_run, last_success)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (job_id, j['name'], j.get('backup_type', 'filesystem'), j.get('server_id'), j.get('s3_config_id'),
                      j.get('remote_host'), j.get('ssh_port', 22), j.get('ssh_key'),
                      j.get('s3_endpoint'), j.get('s3_bucket'), j.get('s3_access_key'), j.get('s3_secret_key'),
                      json.dumps(j.get('directories', [])), json.dumps(j.get('excludes', [])), j.get('database_config_id'),
                      j.get('restic_password'), j.get('backup_prefix'), j.get('schedule_enabled', False),
                      j.get('schedule_cron', '0 2 * * *'), j.get('retention_hourly', 24), j.get('retention_daily', 7),
                      j.get('retention_weekly', 4), j.get('retention_monthly', 12), j.get('timeout', 7200),
                      j.get('status', 'pending'), j.get('created_at', datetime.now().isoformat()), j.get('updated_at'),
                      j.get('last_run'), j.get('last_success')))
            db.commit()
            logger.info(f"  Migrated {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"  Error migrating jobs: {e}")

    # Migrate history
    history_file = data_path / 'history.json'
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
            for h in history:
                db.execute('''
                    INSERT INTO history (timestamp, job_id, job_name, status, message, duration)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (h['timestamp'], h['job_id'], h['job_name'], h['status'], h.get('message', ''), h.get('duration', 0)))
            db.commit()
            logger.info(f"  Migrated {len(history)} history entries")
        except Exception as e:
            logger.error(f"  Error migrating history: {e}")

    logger.info("JSON migration complete!")
