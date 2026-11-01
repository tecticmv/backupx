import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  Download,
  ListChecks,
  Loader2,
  RotateCcw,
  XCircle,
  Clock,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { downloadTaskFile, useTasks } from "@/hooks/useTasks";
import { apiJson } from "@/lib/api";
import { cn } from "@/lib/utils";
import { isActiveTask, type Task, type TaskLogLine, type TaskStatus } from "@/types/task";

const LOG_POLL_MS = 1500;

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  rolled_back: "Rolled back",
};

function StatusIcon({ status }: { status: TaskStatus }) {
  switch (status) {
    case "queued":
      return <Clock className="h-4 w-4 text-muted-foreground" />;
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
    case "succeeded":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
    case "cancelled":
      return <CircleSlash className="h-4 w-4 text-muted-foreground" />;
    case "rolled_back":
      return <RotateCcw className="h-4 w-4 text-amber-500" />;
    default:
      return <XCircle className="h-4 w-4 text-destructive" />;
  }
}

function statusVariant(status: TaskStatus): "default" | "secondary" | "destructive" | "outline" {
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  if (status === "succeeded") return "secondary";
  return "outline";
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleString();
}

/** Live log of one task, polled while it runs. */
function TaskLog({ task }: { task: Task }) {
  const [lines, setLines] = useState<TaskLogLine[]>([]);
  const lastSeq = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const active = isActiveTask(task);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const data = await apiJson<{ logs: TaskLogLine[]; last_seq: number }>(
          `/api/tasks/${task.id}/logs?after=${lastSeq.current}&limit=2000`
        );
        if (!stopped && data.logs.length) {
          lastSeq.current = data.last_seq;
          setLines((prev) => [...prev, ...data.logs].slice(-5000));
        }
      } catch {
        // retry on the next tick
      }
      if (!stopped && active) timer = window.setTimeout(load, LOG_POLL_MS);
    };
    load();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [task.id, active]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);

  return (
    <ScrollArea className="h-56 rounded-md border bg-muted/30">
      <pre className="p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all">
        {lines.length === 0 ? (
          <span className="text-muted-foreground">{active ? "Waiting for output..." : "No output"}</span>
        ) : (
          lines.map((l) => (
            <div
              key={l.seq}
              className={cn(
                l.stream === "stderr" && "text-destructive",
                l.stream === "system" && "text-muted-foreground"
              )}
            >
              {l.line}
            </div>
          ))
        )}
        <div ref={bottom} />
      </pre>
    </ScrollArea>
  );
}

function TaskItem({ task, expanded, onToggle }: { task: Task; expanded: boolean; onToggle: () => void }) {
  const { cancel } = useTasks();
  const active = isActiveTask(task);
  const detail = task.error || task.result?.message || task.step;

  return (
    <div className="rounded-lg border p-3">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-start gap-2 text-left"
        aria-expanded={expanded}
      >
        <span className="mt-0.5">
          <StatusIcon status={task.status} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{task.title}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {detail || STATUS_LABEL[task.status]}
          </span>
        </span>
        <Badge variant={statusVariant(task.status)} className="shrink-0">
          {STATUS_LABEL[task.status]}
        </Badge>
        {expanded ? (
          <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        )}
      </button>

      {task.status === "running" && (
        <div className="mt-2 flex items-center gap-2">
          <Progress value={task.progress} className="h-1.5" />
          <span className="w-9 text-right text-xs tabular-nums text-muted-foreground">{task.progress}%</span>
        </div>
      )}

      {expanded && (
        <div className="mt-3 grid gap-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>Started {timeAgo(task.started_at || task.created_at)}</span>
            {task.finished_at && <span>Finished {timeAgo(task.finished_at)}</span>}
            {task.created_by && <span>by {task.created_by}</span>}
          </div>
          <TaskLog key={task.id} task={task} />
          <div className="flex justify-end gap-2">
            {task.status === "succeeded" && task.result?.download_url && (
              <Button size="sm" variant="outline" onClick={() => downloadTaskFile(task)}>
                <Download className="mr-2 h-4 w-4" />
                Download {task.result.filename}
              </Button>
            )}
            {active && task.cancellable && (
              <Button
                size="sm"
                variant="outline"
                disabled={task.cancel_requested}
                onClick={() => cancel(task.id)}
              >
                {task.cancel_requested ? "Cancelling..." : "Cancel"}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** Header button + sheet listing running and recent background tasks. */
export function TaskTray() {
  const { tasks, activeCount, trayOpen, setTrayOpen, focusedTaskId, setFocusedTaskId } = useTasks();
  const expanded = focusedTaskId;
  const setExpanded = setFocusedTaskId;

  const active = tasks.filter(isActiveTask);
  const recent = tasks.filter((t) => !isActiveTask(t));

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        className="relative gap-2"
        onClick={() => setTrayOpen(true)}
        aria-label={`Background tasks${activeCount ? `, ${activeCount} running` : ""}`}
      >
        {activeCount > 0 ? <Loader2 className="h-4 w-4 animate-spin" /> : <ListChecks className="h-4 w-4" />}
        <span className="hidden sm:inline">Tasks</span>
        {activeCount > 0 && <Badge className="h-5 min-w-5 px-1.5 tabular-nums">{activeCount}</Badge>}
      </Button>
      <Sheet open={trayOpen} onOpenChange={setTrayOpen}>
        <SheetContent className="flex w-full flex-col gap-0 sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>Background tasks</SheetTitle>
            <SheetDescription>
              Backups, restores and maintenance started from this app. Closing the page doesn't stop them.
            </SheetDescription>
          </SheetHeader>
          <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
            <div className="grid gap-4">
              <section className="grid gap-2">
                <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Running ({active.length})
                </h3>
                {active.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Nothing running.</p>
                ) : (
                  active.map((t) => (
                    <TaskItem
                      key={t.id}
                      task={t}
                      expanded={expanded === t.id}
                      onToggle={() => setExpanded(expanded === t.id ? null : t.id)}
                    />
                  ))
                )}
              </section>
              {recent.length > 0 && (
                <section className="grid gap-2">
                  <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Last hour
                  </h3>
                  {recent.map((t) => (
                    <TaskItem
                      key={t.id}
                      task={t}
                      expanded={expanded === t.id}
                      onToggle={() => setExpanded(expanded === t.id ? null : t.id)}
                    />
                  ))}
                </section>
              )}
            </div>
          </ScrollArea>
        </SheetContent>
      </Sheet>
    </>
  );
}
