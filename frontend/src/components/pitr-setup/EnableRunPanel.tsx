import { AlertTriangle, CalendarClock, CheckCircle2, Loader2, RotateCcw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { TaskLog } from "@/components/TaskTray";
import { formatDuration } from "@/lib/format";
import { activeRunTask, FAILED_PHASES } from "@/lib/pitrSetup";
import { formatLocal, formatUtc } from "@/lib/restore";
import { isActiveTask } from "@/types/task";
import type { PitrEnablePhase, PitrEnableRun } from "@/types/pitrSetup";

const PHASE_LABEL: Record<PitrEnablePhase, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  apply: { label: "Applying", variant: "default" },
  scheduled: { label: "Restart scheduled", variant: "outline" },
  restart: { label: "Restarting", variant: "default" },
  stream: { label: "Starting the streamer", variant: "default" },
  anchor: { label: "Anchoring", variant: "default" },
  done: { label: "Done", variant: "secondary" },
  failed: { label: "Failed", variant: "destructive" },
  rolled_back: { label: "Rolled back", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "outline" },
};

interface EnableRunPanelProps {
  run: PitrEnableRun;
  onCancelScheduled: () => void;
  cancelling: boolean;
  onShowDone: () => void;
  onStartOver: () => void;
}

function rolledBackText(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string" && value) return [value];
  return [];
}

/** Apply / Restart / Stream / Anchor steps: progress, current step and live log of the running setup. */
export default function EnableRunPanel({ run, onCancelScheduled, cancelling, onShowDone, onStartOver }: EnableRunPanelProps) {
  const task = activeRunTask(run);
  const active = isActiveTask(task);
  const failed = FAILED_PHASES.includes(run.phase) && run.phase !== "cancelled";
  const phase = PHASE_LABEL[run.phase] ?? { label: run.phase, variant: "outline" as const };
  const scheduled = run.phase === "scheduled";
  const rolledBack = rolledBackText(run.result?.rolled_back ?? task.result?.rolled_back);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {active && !scheduled && <Loader2 className="h-4 w-4 animate-spin text-primary" />}
          {task.title || "Enable point-in-time recovery"}
        </CardTitle>
        <CardDescription>
          {task.started_at
            ? active
              ? `Started ${formatLocal(task.started_at)}`
              : `Took ${formatDuration(task.started_at, task.finished_at)}`
            : "Queued"}
        </CardDescription>
        <CardAction>
          <Badge variant={phase.variant}>{phase.label}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {scheduled && (
          <Alert>
            <CalendarClock className="h-4 w-4" />
            <AlertTitle>Restart scheduled for {formatLocal(run.scheduled_for)}</AlertTitle>
            <AlertDescription className="space-y-2">
              <p>
                <span className="font-mono text-xs">{formatUtc(run.scheduled_for)}</span>. The changes that need no
                restart are applied; the restart, streamer start and anchoring run then. You can cancel until it starts.
              </p>
              {run.restart_task && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onCancelScheduled}
                  disabled={cancelling || run.restart_task.cancel_requested}
                >
                  {cancelling && <Loader2 className="animate-spin" />}
                  {run.restart_task.cancel_requested ? "Cancelling..." : "Cancel the scheduled restart"}
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2 text-sm">
            <span className="min-w-0 truncate text-muted-foreground">
              {task.step || (active ? "Starting..." : task.result?.message || task.status)}
            </span>
            <span className="tabular-nums text-muted-foreground">{task.progress}%</span>
          </div>
          <Progress value={task.progress} />
        </div>
        <TaskLog key={task.id} task={task} className="h-80" />

        {failed && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>{run.phase === "rolled_back" ? "Setup failed and was rolled back" : "Setup failed"}</AlertTitle>
            <AlertDescription className="space-y-1 break-words">
              <p>{task.error || run.result?.message || "See the log above."}</p>
              {rolledBack.length > 0 && (
                <>
                  <p className="font-medium">Rolled back:</p>
                  <ul className="list-disc pl-4">
                    {rolledBack.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </>
              )}
              {run.phase === "rolled_back" && !rolledBack.length && (
                <p>The configuration changes were reverted; the database runs as before.</p>
              )}
            </AlertDescription>
          </Alert>
        )}
        {run.phase === "cancelled" && (
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>Cancelled</AlertTitle>
            <AlertDescription>{run.result?.message || task.error || "The setup was cancelled."}</AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap justify-end gap-2">
          {(failed || run.phase === "cancelled") && (
            <Button variant="outline" onClick={onStartOver}>
              <RotateCcw />
              Start over
            </Button>
          )}
          {run.phase === "done" && (
            <Button onClick={onShowDone}>
              <CheckCircle2 />
              Show result
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
