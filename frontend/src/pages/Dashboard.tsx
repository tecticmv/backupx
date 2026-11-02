import { useCallback, useMemo, useState } from "react";
import { AlertTriangle, FolderSync, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import HealthStatusBar from "@/components/health/HealthStatusBar";
import IssueList from "@/components/health/IssueList";
import JobCard from "@/components/dashboard/JobCard";
import ActivityTimeline from "@/components/dashboard/ActivityTimeline";
import StorageUsageChart from "@/components/dashboard/StorageUsageChart";
import JobFormModal from "@/components/JobFormModal";
import { useHealth, usePolledJson, useRunJob } from "@/hooks/useHealth";
import { formatDateTime, formatRelative } from "@/lib/format";
import { jobBusy, type DashboardOverview, type JobCardData } from "@/types/health";

type JobFilter = "all" | "issues" | "running";

function hasProblems(job: JobCardData) {
  return (
    job.issues.critical + job.issues.warning + job.issues.info > 0 ||
    job.failed_storage_ids.length > 0 ||
    !!job.overdue_since ||
    ["failed", "error", "timeout"].includes(job.status)
  );
}

export default function Dashboard() {
  const [showSnoozed, setShowSnoozed] = useState(false);
  const [filter, setFilter] = useState<JobFilter>("all");
  const [jobModalOpen, setJobModalOpen] = useState(false);

  const overview = usePolledJson<DashboardOverview>("/api/dashboard/overview");
  const health = useHealth({ include_snoozed: showSnoozed });
  const refreshOverview = overview.refresh;
  const refreshHealth = health.refresh;

  const refreshAll = useCallback(() => {
    refreshHealth();
    refreshOverview();
  }, [refreshHealth, refreshOverview]);

  const runJob = useRunJob(refreshAll);

  const jobs = useMemo(() => overview.data?.jobs ?? [], [overview.data]);
  const counts = useMemo(
    () => ({
      all: jobs.length,
      issues: jobs.filter(hasProblems).length,
      running: jobs.filter(jobBusy).length,
    }),
    [jobs]
  );
  const visibleJobs = useMemo(() => {
    const list = filter === "issues" ? jobs.filter(hasProblems) : filter === "running" ? jobs.filter(jobBusy) : jobs;
    // Problems first, then by name
    return [...list].sort((a, b) => Number(hasProblems(b)) - Number(hasProblems(a)) || a.name.localeCompare(b.name));
  }, [jobs, filter]);

  const summary = health.summary;
  const showIssues = health.issues.length > 0 || (summary?.snoozed ?? 0) > 0 || showSnoozed;

  return (
    <div className="space-y-6">
      <HealthStatusBar summary={summary} onChanged={refreshAll} />

      {health.error && !summary && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Could not load health checks</AlertTitle>
          <AlertDescription>{health.error}</AlertDescription>
        </Alert>
      )}

      {showIssues && (
        <Card>
          <CardHeader>
            <CardTitle>Issues</CardTitle>
            <CardDescription>Most urgent first. Safe fixes run with one click; others show the plan first.</CardDescription>
            {(summary?.snoozed ?? 0) > 0 || showSnoozed ? (
              <CardAction className="flex items-center gap-2">
                <Switch id="show-snoozed" checked={showSnoozed} onCheckedChange={setShowSnoozed} />
                <Label htmlFor="show-snoozed" className="text-sm font-normal">
                  Show snoozed{summary?.snoozed ? ` (${summary.snoozed})` : ""}
                </Label>
              </CardAction>
            ) : null}
          </CardHeader>
          <CardContent>
            <IssueList
              issues={health.issues}
              onChanged={refreshAll}
              loading={health.loading}
              emptyTitle="No open issues"
              emptyDescription={summary?.snoozed ? "Some issues are snoozed." : "Every check passed."}
            />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Backup jobs</CardTitle>
          <CardDescription>
            {counts.all} {counts.all === 1 ? "job" : "jobs"}
            {counts.running > 0 && ` · ${counts.running} running`}
          </CardDescription>
          <CardAction>
            <Button size="sm" onClick={() => setJobModalOpen(true)}>
              <Plus />
              New job
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4">
          {jobs.length > 0 && (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={filter}
              onValueChange={(v) => v && setFilter(v as JobFilter)}
              aria-label="Filter jobs"
            >
              <ToggleGroupItem value="all" className="px-3">
                All ({counts.all})
              </ToggleGroupItem>
              <ToggleGroupItem value="issues" className="px-3">
                Issues ({counts.issues})
              </ToggleGroupItem>
              <ToggleGroupItem value="running" className="px-3">
                Running ({counts.running})
              </ToggleGroupItem>
            </ToggleGroup>
          )}

          {overview.loading && !overview.data ? (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-60 w-full rounded-xl" />
              ))}
            </div>
          ) : overview.error && !overview.data ? (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Could not load the dashboard</AlertTitle>
              <AlertDescription>{overview.error}</AlertDescription>
            </Alert>
          ) : jobs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <FolderSync className="mb-4 h-12 w-12 text-muted-foreground/50" />
              <h3 className="font-medium">No backup jobs yet</h3>
              <p className="mb-4 mt-1 text-sm text-muted-foreground">Create your first backup job to get started</p>
              <Button onClick={() => setJobModalOpen(true)}>
                <Plus />
                Create job
              </Button>
            </div>
          ) : visibleJobs.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {filter === "running" ? "Nothing is running right now." : "No jobs with problems."}
            </p>
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {visibleJobs.map((job) => (
                <JobCard key={job.id} job={job} onRun={runJob} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Activity</CardTitle>
            <CardDescription>Last 7 days, newest first</CardDescription>
          </CardHeader>
          <CardContent>
            {overview.data ? (
              <ActivityTimeline events={overview.data.activity} />
            ) : (
              <Skeleton className="h-48 w-full" />
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Storage usage</CardTitle>
            <CardDescription>
              Sizes are refreshed daily
              {overview.data?.storage_usage_checked_at && (
                <span title={formatDateTime(overview.data.storage_usage_checked_at)}>
                  {" "}
                  · checked {formatRelative(overview.data.storage_usage_checked_at)}
                </span>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {overview.data ? (
              <StorageUsageChart usage={overview.data.storage_usage} />
            ) : (
              <Skeleton className="h-48 w-full" />
            )}
          </CardContent>
        </Card>
      </div>

      <JobFormModal open={jobModalOpen} onOpenChange={setJobModalOpen} onSuccess={refreshAll} />
    </div>
  );
}
