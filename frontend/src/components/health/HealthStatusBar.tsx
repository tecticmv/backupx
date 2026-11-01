import { useState } from "react";
import { AlertOctagon, AlertTriangle, CheckCircle2, Loader2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import FixAllSafeButton from "@/components/health/FixAllSafeButton";
import { formatDateTime, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useHealthActions } from "@/hooks/useHealth";
import { useTasks } from "@/hooks/useTasks";
import { isActiveTask } from "@/types/task";
import type { HealthSummary } from "@/types/health";

/** Top of the dashboard: overall state, last sweep, Re-check and Fix-all-safe. */
export default function HealthStatusBar({
  summary,
  onChanged,
}: {
  summary: HealthSummary | null;
  onChanged: () => void;
}) {
  const { recheck } = useHealthActions(onChanged);
  const { tasks } = useTasks();
  const [recheckTaskId, setRecheckTaskId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const recheckTask = recheckTaskId ? tasks.find((t) => t.id === recheckTaskId) : undefined;
  const sweeping = starting || !!summary?.sweep_running || (!!recheckTask && isActiveTask(recheckTask));

  const onRecheck = async () => {
    setStarting(true);
    try {
      const task = await recheck();
      if (task) setRecheckTaskId(task.id);
    } finally {
      setStarting(false);
    }
  };

  if (!summary) {
    return <Skeleton className="h-[76px] w-full rounded-xl" />;
  }

  const ok = summary.total === 0;
  const tone = ok ? "ok" : summary.critical > 0 ? "critical" : "warning";
  const Icon = tone === "ok" ? CheckCircle2 : tone === "critical" ? AlertOctagon : AlertTriangle;

  return (
    <Card
      className={cn(
        "py-0",
        tone === "ok" && "border-emerald-500/30 bg-emerald-500/5",
        tone === "warning" && "border-amber-500/30 bg-amber-500/5",
        tone === "critical" && "border-destructive/40 bg-destructive/5"
      )}
    >
      <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3" role="status" aria-live="polite">
          <div
            className={cn(
              "rounded-full p-2",
              tone === "ok" && "bg-emerald-500/15 text-emerald-500",
              tone === "warning" && "bg-amber-500/15 text-amber-500",
              tone === "critical" && "bg-destructive/15 text-destructive"
            )}
          >
            <Icon className="h-6 w-6" />
          </div>
          <div>
            <p className="text-lg font-semibold leading-tight">
              {ok
                ? "All good"
                : `${summary.total} ${summary.total === 1 ? "issue" : "issues"}${
                    summary.critical > 0 ? ` (${summary.critical} critical)` : ""
                  }`}
            </p>
            <p className="text-xs text-muted-foreground">
              {sweeping ? (
                "Checking now…"
              ) : summary.last_sweep_at ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span tabIndex={0}>Last checked {formatRelative(summary.last_sweep_at)}</span>
                  </TooltipTrigger>
                  <TooltipContent>{formatDateTime(summary.last_sweep_at)}</TooltipContent>
                </Tooltip>
              ) : (
                "Not checked yet"
              )}
              {summary.snoozed > 0 && ` · ${summary.snoozed} snoozed`}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={onRecheck} disabled={sweeping}>
            {sweeping ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            Re-check
          </Button>
          {summary.safe_fixable > 0 && <FixAllSafeButton count={summary.safe_fixable} onChanged={onChanged} />}
        </div>
      </CardContent>
    </Card>
  );
}
