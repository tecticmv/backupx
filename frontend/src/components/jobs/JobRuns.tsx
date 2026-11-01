import { Fragment, useEffect, useState } from "react";
import { ChevronRight, FileText, History, Loader2, RotateCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { apiJson } from "@/lib/api";
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import { usePolledJson } from "@/hooks/useHealth";
import { useTasks } from "@/hooks/useTasks";
import type { JobRun, RunLog, RunStorageResult } from "@/types/health";

const RUN_STATUS: Record<JobRun["status"], { label: string; className: string }> = {
  success: { label: "Success", className: "border-emerald-500/40 text-emerald-600 dark:text-emerald-400" },
  partial: { label: "Partial", className: "border-amber-500/50 text-amber-600 dark:text-amber-400" },
  failed: { label: "Failed", className: "border-destructive/50 text-destructive dark:text-red-400" },
};

const STORAGE_STATUS: Record<string, string> = {
  success: "border-emerald-500/40 text-emerald-600 dark:text-emerald-400",
  failed: "border-destructive/50 text-destructive dark:text-red-400",
  error: "border-destructive/50 text-destructive dark:text-red-400",
  timeout: "border-amber-500/50 text-amber-600 dark:text-amber-400",
  not_run: "text-muted-foreground",
};

function StorageLog({ runId, storageId }: { runId: string; storageId: string }) {
  const [log, setLog] = useState<RunLog | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiJson<RunLog>(`/api/runs/${encodeURIComponent(runId)}/log?storage_id=${encodeURIComponent(storageId)}`)
      .then((data) => !cancelled && setLog(data))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : "Could not load the log"));
    return () => {
      cancelled = true;
    };
  }, [runId, storageId]);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (!log) return <Skeleton className="h-24 w-full" />;
  return (
    <div className="space-y-1">
      <ScrollArea className="h-80 rounded-md border bg-muted/40">
        <pre className="whitespace-pre-wrap break-all p-3 font-mono text-xs">{log.content || "(empty log)"}</pre>
      </ScrollArea>
      {log.truncated && <p className="text-xs text-muted-foreground">Log truncated (only the first part is kept).</p>}
    </div>
  );
}

function StorageResult({ runId, storage }: { runId: string; storage: RunStorageResult }) {
  const [showLog, setShowLog] = useState(false);
  const hasLog = storage.has_log !== false;
  return (
    <div className="space-y-2 rounded-md border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{storage.storage_name ?? storage.storage_id}</span>
        <Badge variant="outline" className={STORAGE_STATUS[storage.status] ?? ""}>
          {storage.status.replace("_", " ")}
        </Badge>
        {storage.exit_code !== null && storage.exit_code !== undefined && (
          <span className="text-xs text-muted-foreground">exit {storage.exit_code}</span>
        )}
        {storage.snapshot_id && (
          <Badge variant="secondary" className="font-mono text-[10px]">
            {storage.snapshot_id.slice(0, 8)}
          </Badge>
        )}
        {hasLog && (
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto h-7 text-xs"
            onClick={() => setShowLog((v) => !v)}
            aria-expanded={showLog}
          >
            <FileText />
            {showLog ? "Hide log" : "View log"}
          </Button>
        )}
      </div>
      {storage.error && <p className="break-words text-xs text-destructive">{storage.error}</p>}
      {showLog && <StorageLog runId={runId} storageId={storage.storage_id} />}
    </div>
  );
}

/** Recent runs of a job with per-storage results, logs and "Retry failed storages". */
export default function JobRuns({
  jobId,
  onRetry,
}: {
  jobId: string;
  onRetry: (jobId: string, storageIds: string[]) => Promise<unknown>;
}) {
  const { data, error, loading, refresh } = usePolledJson<{ runs: JobRun[] }>(
    `/api/jobs/${encodeURIComponent(jobId)}/runs`
  );
  const { openTray } = useTasks();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);

  const retry = async (run: JobRun) => {
    setRetrying(run.run_id);
    try {
      await onRetry(jobId, run.failed_storage_ids);
      refresh();
    } finally {
      setRetrying(null);
    }
  };

  if (loading && !data) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }
  if (error && !data) return <p className="text-sm text-destructive">{error}</p>;

  const runs = data?.runs ?? [];
  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-center">
        <History className="mb-3 h-10 w-10 text-muted-foreground/50" />
        <h3 className="font-medium">No runs recorded yet</h3>
        <p className="mt-1 text-sm text-muted-foreground">Per-storage results appear after the next backup.</p>
      </div>
    );
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-8" />
            <TableHead>Started</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="hidden md:table-cell">Storages</TableHead>
            <TableHead className="hidden sm:table-cell">Trigger</TableHead>
            <TableHead className="hidden sm:table-cell">Duration</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => {
            const open = expanded === run.run_id;
            const status = RUN_STATUS[run.status] ?? RUN_STATUS.failed;
            return (
              <Fragment key={run.run_id}>
                <TableRow
                  className="cursor-pointer"
                  onClick={() => setExpanded(open ? null : run.run_id)}
                  data-state={open ? "selected" : undefined}
                >
                  <TableCell>
                    <button
                      type="button"
                      aria-label={open ? "Collapse run" : "Expand run"}
                      aria-expanded={open}
                      className="flex items-center"
                      onClick={(e) => {
                        e.stopPropagation();
                        setExpanded(open ? null : run.run_id);
                      }}
                    >
                      <ChevronRight className={cn("h-4 w-4 transition-transform", open && "rotate-90")} />
                    </button>
                  </TableCell>
                  <TableCell>
                    <span title={formatDateTime(run.started_at)}>{formatRelative(run.started_at ?? run.finished_at)}</span>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className={status.className}>
                      {status.label}
                    </Badge>
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <div className="flex flex-wrap gap-1">
                      {run.storages.map((s) => (
                        <Tooltip key={s.storage_id}>
                          <TooltipTrigger asChild>
                            <Badge variant="outline" className={cn("max-w-[10rem]", STORAGE_STATUS[s.status] ?? "")}>
                              <span className="truncate">{s.storage_name ?? s.storage_id}</span>
                            </Badge>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-sm">
                            {s.status}
                            {s.error ? `: ${s.error}` : ""}
                          </TooltipContent>
                        </Tooltip>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="hidden capitalize text-muted-foreground sm:table-cell">
                    {run.trigger ?? "—"}
                  </TableCell>
                  <TableCell className="hidden text-muted-foreground sm:table-cell">
                    {formatDuration(run.started_at, run.finished_at)}
                  </TableCell>
                  <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
                    <div className="flex justify-end gap-1">
                      {run.task_id && (
                        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => openTray(run.task_id!)}>
                          Task
                        </Button>
                      )}
                      {run.failed_storage_ids.length > 0 && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          onClick={() => retry(run)}
                          disabled={retrying === run.run_id}
                        >
                          {retrying === run.run_id ? <Loader2 className="animate-spin" /> : <RotateCw />}
                          Retry failed ({run.failed_storage_ids.length})
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
                {open && (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={7} className="whitespace-normal bg-muted/20 p-3">
                      <div className="space-y-2">
                        {run.storages.map((s) => (
                          <StorageResult key={s.storage_id} runId={run.run_id} storage={s} />
                        ))}
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
