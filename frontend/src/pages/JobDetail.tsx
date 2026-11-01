import { useCallback, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Database, FolderSync, History, Loader2, Pencil, Play, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import IssueList from "@/components/health/IssueList";
import JobCard from "@/components/dashboard/JobCard";
import JobRuns from "@/components/jobs/JobRuns";
import JobFormModal from "@/components/JobFormModal";
import PitrBadge from "@/components/PitrBadge";
import JobRestoresList from "@/components/restore/JobRestoresList";
import RestoreDrillCard from "@/components/jobs/RestoreDrillCard";
import Snapshots from "@/pages/Snapshots";
import { useHealth, usePolledJson, useRunJob } from "@/hooks/useHealth";
import { formatWindow } from "@/lib/pitr";
import type { DashboardOverview } from "@/types/health";
import type { Jobs } from "@/types/job";

const TABS = ["overview", "runs", "snapshots", "recovery", "settings"] as const;
type Tab = (typeof TABS)[number];

export default function JobDetail() {
  const { jobId = "" } = useParams<{ jobId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const tab: Tab = (TABS as readonly string[]).includes(tabParam ?? "") ? (tabParam as Tab) : "overview";
  const [editOpen, setEditOpen] = useState(false);

  const jobs = usePolledJson<Jobs>("/api/jobs");
  const overview = usePolledJson<DashboardOverview>(
    `/api/dashboard/overview?job_id=${encodeURIComponent(jobId ?? "")}`,
  );
  const health = useHealth({ resource_type: "job", resource_id: jobId }, !!jobId);
  const refreshJobs = jobs.refresh;
  const refreshOverview = overview.refresh;
  const refreshHealth = health.refresh;

  const refreshAll = useCallback(() => {
    refreshJobs();
    refreshOverview();
    refreshHealth();
  }, [refreshJobs, refreshOverview, refreshHealth]);

  const runJob = useRunJob(refreshAll);
  const [starting, setStarting] = useState(false);

  const job = jobs.data?.[jobId] ?? null;
  const card = overview.data?.jobs.find((j) => j.id === jobId) ?? null;
  const running = starting || !!card?.running || job?.status === "running";

  const setTab = (value: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === "overview") next.delete("tab");
        else next.set("tab", value);
        // The storage picker belongs to the Snapshots tab
        if (value !== "snapshots") next.delete("storage");
        return next;
      },
      { replace: true }
    );
  };

  const runNow = async () => {
    setStarting(true);
    try {
      await runJob(jobId);
    } finally {
      setStarting(false);
    }
  };

  if (jobs.loading && !jobs.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-9 w-96" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (!job) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <FolderSync className="mb-4 h-12 w-12 text-muted-foreground/50" />
        <h3 className="font-medium">Job not found</h3>
        <p className="mb-4 mt-1 text-sm text-muted-foreground">
          {jobs.error ?? "The requested backup job does not exist"}
        </p>
        <Button asChild>
          <Link to="/jobs">Go to Jobs</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <Button variant="ghost" size="icon" asChild>
            <Link to="/" aria-label="Back to dashboard">
              <ArrowLeft />
            </Link>
          </Button>
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 truncate text-2xl font-bold">
              {job.backup_type === "database" ? (
                <Database className="h-5 w-5 shrink-0 text-muted-foreground" />
              ) : (
                <FolderSync className="h-5 w-5 shrink-0 text-muted-foreground" />
              )}
              <span className="truncate">{job.name}</span>
            </h1>
            <p className="truncate text-sm text-muted-foreground">
              {card?.server_name ?? job.remote_host ?? "—"}
              {job.schedule_enabled && job.schedule_cron && (
                <span className="font-mono"> · {job.schedule_cron}</span>
              )}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
            <Pencil />
            Edit
          </Button>
          <Button size="sm" onClick={runNow} disabled={running}>
            {running ? <Loader2 className="animate-spin" /> : <Play />}
            Run now
          </Button>
        </div>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <div className="overflow-x-auto">
          <TabsList>
            <TabsTrigger value="overview">
              Overview
              {health.issues.length > 0 && (
                <Badge variant="secondary" className="ml-1 h-5 px-1.5">
                  {health.issues.length}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value="runs">Runs</TabsTrigger>
            <TabsTrigger value="snapshots">Snapshots</TabsTrigger>
            <TabsTrigger value="recovery">Recovery</TabsTrigger>
            <TabsTrigger value="settings">Settings</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="overview" className="mt-4">
          <div className="grid gap-6 lg:grid-cols-3">
            <div>
              {card ? (
                <JobCard job={card} onRun={runJob} onDetailPage />
              ) : (
                <Skeleton className="h-60 w-full rounded-xl" />
              )}
            </div>
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>Issues</CardTitle>
                <CardDescription>Health checks for this job and its storages</CardDescription>
              </CardHeader>
              <CardContent>
                <IssueList
                  issues={health.issues}
                  onChanged={refreshAll}
                  loading={health.loading}
                  hideResource
                  emptyTitle="No issues with this job"
                />
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="runs" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Runs</CardTitle>
              <CardDescription>Each run's result per storage. Expand a run to read its logs.</CardDescription>
            </CardHeader>
            <CardContent>
              <JobRuns jobId={jobId} onRetry={runJob} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="snapshots" className="mt-4">
          <Snapshots embedded />
        </TabsContent>

        <TabsContent value="recovery" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <RotateCcw className="h-5 w-5" />
                Restore
              </CardTitle>
              <CardDescription>
                Restore into a scratch copy first and check it; then download it or replace production with an
                automatic safety backup and Undo.
              </CardDescription>
              <CardAction>
                <Button asChild>
                  <Link to={`/restore?job=${encodeURIComponent(jobId)}`}>
                    <RotateCcw />
                    Start a restore
                  </Link>
                </Button>
              </CardAction>
            </CardHeader>
            <CardContent>
              <JobRestoresList jobId={jobId} />
            </CardContent>
          </Card>
          {job.pitr?.enabled ? (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  Point-in-time recovery <PitrBadge pitr={job.pitr} />
                </CardTitle>
                <CardDescription>Restorable to any second inside this window</CardDescription>
                <CardAction>
                  <Button variant="outline" size="sm" asChild>
                    <Link to={`/jobs/${encodeURIComponent(jobId)}/pitr-setup`}>
                      <History />
                      Re-run PITR setup
                    </Link>
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent className="space-y-1 text-sm">
                <p>{formatWindow(job.pitr.window_start, job.pitr.window_end)}</p>
                {job.pitr.lag_minutes !== null && (
                  <p className="text-muted-foreground">Lag: {Math.round(job.pitr.lag_minutes)} min</p>
                )}
                {job.pitr.last_error && <p className="text-destructive">Last error: {job.pitr.last_error}</p>}
              </CardContent>
            </Card>
          ) : (
            job.backup_type === "database" && (
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <History className="h-5 w-5" />
                    Point-in-time recovery
                  </CardTitle>
                  <CardDescription>
                    Off. Turn it on to restore this database to any second, not just to the last backup. The setup
                    checks the server, shows every change and its downtime, and takes a fresh backup first.
                  </CardDescription>
                  <CardAction>
                    <Button asChild>
                      <Link to={`/jobs/${encodeURIComponent(jobId)}/pitr-setup`}>
                        <History />
                        Set up point-in-time recovery
                      </Link>
                    </Button>
                  </CardAction>
                </CardHeader>
              </Card>
            )
          )}
          {job.pitr?.enabled && <RestoreDrillCard jobId={jobId} />}
        </TabsContent>

        <TabsContent value="settings" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Settings</CardTitle>
              <CardDescription>Source, storages, schedule, retention and PITR</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Type</dt>
                  <dd>{job.backup_type === "database" ? "Database" : "Files"}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Schedule</dt>
                  <dd className="font-mono">{job.schedule_enabled ? job.schedule_cron : "Off"}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Storages</dt>
                  <dd>{job.s3_config_ids?.length ?? 1}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Retention</dt>
                  <dd>
                    {job.retention_hourly}h / {job.retention_daily}d / {job.retention_weekly}w / {job.retention_monthly}m
                  </dd>
                </div>
              </dl>
              <Button onClick={() => setEditOpen(true)}>
                <Pencil />
                Edit job settings
              </Button>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <JobFormModal open={editOpen} onOpenChange={setEditOpen} jobId={jobId} onSuccess={refreshAll} />
    </div>
  );
}
