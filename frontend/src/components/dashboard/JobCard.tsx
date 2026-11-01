import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Camera,
  CheckCircle2,
  Clock,
  Database,
  ExternalLink,
  FolderSync,
  History,
  Loader2,
  MinusCircle,
  MoreHorizontal,
  Play,
  RotateCcw,
  RotateCw,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import PitrBadge from "@/components/PitrBadge";
import { formatWindow } from "@/lib/pitr";
import { formatDateTime, formatRelative } from "@/lib/format";
import { SEVERITY_META } from "@/lib/health";
import { cn } from "@/lib/utils";
import type { JobCardData, JobCardStorage } from "@/types/health";
import type { PitrState } from "@/types/job";

const STORAGE_STATUS: Record<string, { label: string; icon: typeof CheckCircle2; className: string }> = {
  success: { label: "OK", icon: CheckCircle2, className: "border-emerald-500/40 text-emerald-600 dark:text-emerald-400" },
  failed: { label: "Failed", icon: XCircle, className: "border-destructive/50 text-destructive dark:text-red-400" },
  error: { label: "Error", icon: XCircle, className: "border-destructive/50 text-destructive dark:text-red-400" },
  timeout: { label: "Timed out", icon: Clock, className: "border-amber-500/50 text-amber-600 dark:text-amber-400" },
  not_run: { label: "Not run", icon: MinusCircle, className: "text-muted-foreground" },
};

const RETENTION_LABEL: Record<JobCardData["retention"]["state"], { text: string; className: string }> = {
  ok: { text: "OK", className: "text-emerald-600 dark:text-emerald-400" },
  failed: { text: "Failed", className: "text-destructive" },
  skipped: { text: "Skipped", className: "text-amber-600 dark:text-amber-400" },
  off: { text: "Off", className: "text-muted-foreground" },
  never: { text: "Not run yet", className: "text-muted-foreground" },
};

function StorageChip({ storage }: { storage: JobCardStorage }) {
  const meta = storage.status ? STORAGE_STATUS[storage.status] : undefined;
  const Icon = meta?.icon ?? MinusCircle;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className={cn("max-w-[11rem] gap-1", meta?.className ?? "text-muted-foreground")} tabIndex={0}>
          <Icon />
          <span className="truncate">{storage.name}</span>
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm">
        <p className="font-medium">
          {storage.name}: {meta?.label ?? "No runs yet"}
        </p>
        {storage.finished_at && <p>Finished {formatDateTime(storage.finished_at)}</p>}
        {storage.error && <p className="mt-1 break-words">{storage.error}</p>}
      </TooltipContent>
    </Tooltip>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right">{children}</dd>
    </div>
  );
}

interface JobCardProps {
  job: JobCardData;
  onRun: (jobId: string, storageIds?: string[]) => Promise<unknown>;
  /** Hide the name link / "Open details" (when already on the job's page). */
  onDetailPage?: boolean;
}

export default function JobCard({ job, onRun, onDetailPage = false }: JobCardProps) {
  const [starting, setStarting] = useState(false);
  const running = starting || job.running;
  const issueTotal = job.issues.critical + job.issues.warning + job.issues.info;
  const worst = job.issues.critical ? "critical" : job.issues.warning ? "warning" : job.issues.info ? "info" : null;

  const run = async (storageIds?: string[]) => {
    setStarting(true);
    try {
      await onRun(job.id, storageIds);
    } finally {
      setStarting(false);
    }
  };

  const isDb = job.backup_type === "database";
  const pitrSetupHref = `/jobs/${encodeURIComponent(job.id)}/pitr-setup`;
  const pitr = job.pitr?.enabled
    ? { ...job.pitr, state: job.pitr.state as PitrState, last_error: job.pitr.last_error ?? null }
    : null;

  return (
    <Card className={cn("gap-4", worst && `border-l-4 ${SEVERITY_META[worst].border}`)}>
      <CardHeader className="gap-1">
        <CardTitle className="flex min-w-0 items-center gap-2 text-base">
          {job.backup_type === "database" ? (
            <Database className="h-4 w-4 shrink-0 text-muted-foreground" aria-label="Database job" />
          ) : (
            <FolderSync className="h-4 w-4 shrink-0 text-muted-foreground" aria-label="Files job" />
          )}
          {onDetailPage ? (
            <span className="truncate">{job.name}</span>
          ) : (
            <Link to={`/jobs/${job.id}`} className="truncate hover:underline underline-offset-4">
              {job.name}
            </Link>
          )}
          {running && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" aria-label="Running" />}
        </CardTitle>
        <p className="truncate text-xs text-muted-foreground">{job.server_name ?? "No server"}</p>
        <CardAction>
          {issueTotal > 0 && worst ? (
            <Badge variant="outline" className={SEVERITY_META[worst].badge}>
              {issueTotal} {issueTotal === 1 ? "issue" : "issues"}
            </Badge>
          ) : (
            <Badge variant="outline" className="border-emerald-500/40 text-emerald-600 dark:text-emerald-400">
              Healthy
            </Badge>
          )}
        </CardAction>
      </CardHeader>

      <CardContent className="space-y-3 text-sm">
        {job.storages.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {job.storages.map((s) => (
              <StorageChip key={s.id} storage={s} />
            ))}
          </div>
        )}
        <dl className="space-y-1.5 text-xs">
          <Row label="Last backup">
            <span title={formatDateTime(job.last_success)}>{formatRelative(job.last_success)}</span>
            {job.last_run && job.last_run !== job.last_success && job.status !== "success" && (
              <span className="text-muted-foreground"> · last try {formatRelative(job.last_run)}</span>
            )}
          </Row>
          <Row label="Next run">
            {job.overdue_since ? (
              <span className="text-destructive">Overdue since {formatRelative(job.overdue_since)}</span>
            ) : job.schedule_enabled && job.next_run ? (
              <span title={job.schedule_cron ?? undefined}>{formatRelative(job.next_run)}</span>
            ) : (
              <span className="text-muted-foreground">Not scheduled</span>
            )}
          </Row>
          <Row label="Retention">
            <span
              className={RETENTION_LABEL[job.retention.state]?.className}
              title={job.retention.message ?? undefined}
            >
              {RETENTION_LABEL[job.retention.state]?.text ?? job.retention.state}
              {job.retention.last_at && ` · ${formatRelative(job.retention.last_at)}`}
            </span>
          </Row>
          {pitr && (
            <Row label="Point-in-time">
              <span className="inline-flex items-center gap-1.5">
                <span className="hidden truncate text-muted-foreground lg:inline" title={formatWindow(pitr.window_start, pitr.window_end)}>
                  {pitr.window_start ? `from ${formatRelative(pitr.window_start)}` : ""}
                  {pitr.lag_minutes !== null && ` · lag ${Math.round(pitr.lag_minutes)}m`}
                </span>
                <PitrBadge pitr={pitr} />
              </span>
            </Row>
          )}
        </dl>
      </CardContent>

      <CardFooter className="mt-auto gap-2">
        <Button size="sm" variant="outline" onClick={() => run()} disabled={running}>
          {running ? <Loader2 className="animate-spin" /> : <Play />}
          Run now
        </Button>
        <Button size="sm" variant="ghost" asChild>
          <Link to={`/restore?job=${encodeURIComponent(job.id)}`}>
            <RotateCcw />
            Restore
          </Link>
        </Button>
        {isDb && !pitr && (
          <Button size="sm" variant="ghost" asChild>
            <Link to={pitrSetupHref}>
              <History />
              Enable PITR
            </Link>
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              size="icon-sm"
              variant="ghost"
              className="ml-auto"
              aria-label={`More actions for ${job.name}`}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {!onDetailPage && (
              <DropdownMenuItem asChild>
                <Link to={`/jobs/${job.id}`}>
                  <ExternalLink className="mr-2 h-4 w-4" />
                  Open details
                </Link>
              </DropdownMenuItem>
            )}
            <DropdownMenuItem asChild>
              <Link to={onDetailPage ? `/jobs/${job.id}?tab=snapshots` : `/jobs/${job.id}/snapshots`}>
                <Camera className="mr-2 h-4 w-4" />
                Snapshots
              </Link>
            </DropdownMenuItem>
            {isDb && (
              <DropdownMenuItem asChild>
                <Link to={pitrSetupHref}>
                  <History className="mr-2 h-4 w-4" />
                  PITR setup
                </Link>
              </DropdownMenuItem>
            )}
            {job.failed_storage_ids.length > 0 && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => run(job.failed_storage_ids)} disabled={running}>
                  <RotateCw className="mr-2 h-4 w-4" />
                  Retry failed storages ({job.failed_storage_ids.length})
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </CardFooter>
    </Card>
  );
}
