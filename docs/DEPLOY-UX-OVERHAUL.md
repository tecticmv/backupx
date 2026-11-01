# Deploying `feat/ux-overhaul` (P0-P3)

One deploy of the whole branch to the backupx server (172.19.5.200,
`/root/backupx`). Pick a quiet time: not around 21:00 UTC (02:00 scheduler time,
the nightly backups) and not Saturday 03:30 / Sunday 04:00 scheduler time
(drills / prune). Check no backup is running first (Jobs list, or
`docker exec backup-ui ps aux | grep restic`).

## 1. Before

- [ ] Rollback point: `cd /root/backupx && git rev-parse HEAD > /root/backupx-predeploy-ux.rev`;
      `docker compose exec -T postgres pg_dump -U backupx backupx | gzip > /root/backupx-predeploy-ux.sql.gz`;
      `cp .env /root/backupx-predeploy-ux.env`
- [ ] Note the live PITR jobs' state on the dashboard (E-Petition, Koha, DSpace: all *ok*).
- [ ] `DOCKER_GID`: `stat -c %g /var/run/docker.sock` on the host -> add `DOCKER_GID=<gid>` to `.env`
      (compose `group_add`; the backupx user needs the socket for scratch restores and drills).
- [ ] Free disk under the `backup-data` volume for scratch restores (dump size x 2.5; the plan refuses
      otherwise): `df -h /var/lib/docker`.

## 2. Environment (`.env`, all optional; defaults shown)

| Variable | Default | Why |
|---|---|---|
| `DOCKER_GID` | 999 | **set it** (above) |
| `TASK_RUNNER_ENABLED` | true | background tasks run in every gunicorn worker |
| `STEP_UP_TTL` | 300 | password re-entry window for confirm/maintenance actions |
| `RESTORE_DRILL_ENABLED` / `RESTORE_DRILL_CRON` | true / `30 3 * * 6` | weekly drills (read-only on production, scratch copy here). Set `false` to defer stage 1 |
| `PITR_MAINT_HEALTH_TIMEOUT` / `PITR_MAINT_RECOVERY_MAX_WAIT` | 300 / 7200 | restart health check / crash-recovery wait |
| `PITR_MANAGED_HOSTS` | empty | extra managed-DB host regex (DigitalOcean is built in) |
| `HEALTH_ALERT_CHANNELS` | telegram | health alerts |
| `RESTORE_TTL_HOURS` / `PRE_RESTORE_KEEP_DAYS` | 24 / 30 | scratch copies / pre-restore snapshots |
| `ALLOW_DIRECT_DB_RESTORE` | false | keep false |

## 3. Deploy

```bash
cd /root/backupx
git fetch origin && git checkout feat/ux-overhaul && git pull --ff-only   # or main after the merge
docker compose -f docker-compose.yml up -d --build
docker compose logs -f backup-ui | grep -iE 'migrat|error|scheduler|runner' &   # watch the start
```

Migrations run automatically at start under the schema lock (no manual step):
tables `tasks`, `task_logs`, `run_results`, `run_logs`, `health_findings`,
`health_probes`, `restores`; columns `history.run_id`,
`notification_channels.last_*`, `jobs.restore_verify`,
`jobs.pitr_client_auto` (false for every existing job), `jobs.pitr_client_image_digest`,
`jobs.pitr_client_server_version`, `tasks.pinned_worker`.

## 4. Smoke checks (in order, stop at the first failure)

- [ ] `curl -fsS http://localhost:9090/health` -> ok; UI loads; log in.
- [ ] `docker exec backup-ui docker ps` works as the backupx user (DOCKER_GID right).
- [ ] Dashboard: status bar, job cards, no unexpected critical issue; *Re-check* runs a health sweep task (task tray).
- [ ] Live PITR jobs unchanged: E-Petition (host mysql 8.4.13 client), Koha (host MariaDB 10.6 client,
      its manual `99-backupx-pitr.cnf` untouched), DSpace (WAL-G): states stay *ok* across two ticks
      (5 min); `systemctl status backupx-pitr-binlog@<job>` on epetition-do / Koha host still active.
      `pitr_client_auto` is false for all of them, so nothing about their client changed.
- [ ] A file job: *Run now* -> task succeeds, per-storage results shown.
- [ ] Restore wizard: scratch restore of a small DB snapshot -> Verify -> Remove now (container gone:
      `docker ps -a --filter label=backupx.restore`).
- [ ] Restore drill on Koha from its job page (*Run drill now*): passes, "Verified restore" shows on the card.
- [ ] PITR setup wizard on a PITR job: *Check* only (shows the plan, applies nothing). Do **not** apply
      anything on production during the smoke test.

## 5. Staged rollout of the new PITR features

1. **Week 0 (this deploy)**: automatic client matching OFF for existing jobs (default); weekly drills ON.
2. **Week 1**: every PITR job has a passing drill (card shows *verified*); investigate any failure first.
3. **Then, one job at a time**: run the PITR setup wizard on the job; select *Match the client tools to
   the server automatically* only; apply; watch the next tick, the next nightly anchored dump and the
   next drill. Only then the next job. E-Petition first is not recommended (managed DB with ~15 min of
   binlogs: a streamer outage breaks the chain until the next dump).
4. Database config changes/restarts through the wizard only in an agreed window, with the scheduled-window
   option, never during the nightly backups.

## 6. Rollback

```bash
cd /root/backupx
git checkout "$(cat /root/backupx-predeploy-ux.rev)"
docker compose -f docker-compose.yml up -d --build
```

The new tables/columns are additive and ignored by the old code, so a code rollback is enough; restore
the database dump only if data was damaged:
`gunzip -c /root/backupx-predeploy-ux.sql.gz | docker compose exec -T postgres psql -U backupx backupx`
(stop backup-ui first). Remove leftover scratch containers: `docker rm -f $(docker ps -aq --filter
label=backupx.restore)`. The PITR streamers keep running under either version.
