import { Link } from "react-router-dom";
import { Archive, Clock, History, ListChecks, Scissors, ShieldCheck, type LucideIcon } from "lucide-react";
import { formatDateTime, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useTasks } from "@/hooks/useTasks";
import type { ActivityEvent } from "@/types/health";

const TYPE_ICON: Record<ActivityEvent["type"], LucideIcon> = {
  backup: Archive,
  retention: Scissors,
  task: ListChecks,
  pitr: History,
  audit: ShieldCheck,
};

const STATUS_STYLE: Record<ActivityEvent["status"], { dot: string; label: string }> = {
  success: { dot: "bg-emerald-500/15 text-emerald-600 ring-emerald-500/30 dark:text-emerald-400", label: "Succeeded" },
  failed: { dot: "bg-destructive/15 text-destructive ring-destructive/30", label: "Failed" },
  warning: { dot: "bg-amber-500/15 text-amber-600 ring-amber-500/30 dark:text-amber-400", label: "Warning" },
  info: { dot: "bg-muted text-muted-foreground ring-border", label: "Info" },
  running: { dot: "bg-primary/15 text-primary ring-primary/30", label: "Running" },
};

/** Newest-first list of what happened (backups, retention, tasks, PITR, audit). */
export default function ActivityTimeline({ events, max = 30 }: { events: ActivityEvent[]; max?: number }) {
  const { openTray } = useTasks();

  if (events.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-center">
        <Clock className="mb-3 h-10 w-10 text-muted-foreground/50" />
        <h3 className="font-medium">No activity in the last 7 days</h3>
        <p className="mt-1 text-sm text-muted-foreground">Backups, fixes and retention runs show up here.</p>
      </div>
    );
  }

  return (
    <ol className="relative space-y-4">
      {events.slice(0, max).map((event, i) => {
        const Icon = TYPE_ICON[event.type] ?? ListChecks;
        const style = STATUS_STYLE[event.status] ?? STATUS_STYLE.info;
        const last = i === Math.min(events.length, max) - 1;
        return (
          <li key={`${event.ts}-${i}`} className="relative flex gap-3">
            {!last && <span className="absolute left-4 top-9 -bottom-4 w-px bg-border" aria-hidden />}
            <span
              className={cn("relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full ring-1", style.dot)}
              title={`${event.type} · ${style.label}`}
            >
              <Icon className={cn("h-4 w-4", event.status === "running" && "animate-pulse")} />
              <span className="sr-only">
                {event.type} {style.label}
              </span>
            </span>
            <div className="min-w-0 flex-1 pt-0.5">
              <div className="flex items-baseline justify-between gap-2">
                <p className="min-w-0 truncate text-sm font-medium">
                  {event.job_id ? (
                    <Link to={`/jobs/${event.job_id}`} className="hover:underline underline-offset-4">
                      {event.title}
                    </Link>
                  ) : (
                    event.title
                  )}
                </p>
                <time className="shrink-0 text-xs text-muted-foreground" dateTime={event.ts} title={formatDateTime(event.ts)}>
                  {formatRelative(event.ts)}
                </time>
              </div>
              {event.detail && <p className="line-clamp-2 break-words text-xs text-muted-foreground">{event.detail}</p>}
              {event.task_id && (
                <button
                  type="button"
                  onClick={() => openTray(event.task_id!)}
                  className="text-xs text-primary hover:underline"
                >
                  Task details
                </button>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
