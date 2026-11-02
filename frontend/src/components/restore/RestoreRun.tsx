import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { TaskLog } from "@/components/TaskTray";
import RestoreStatusBadge from "@/components/restore/RestoreStatusBadge";
import { downloadTaskFile, useTasks } from "@/hooks/useTasks";
import { formatDuration } from "@/lib/format";
import { currentTask, describeBase, describePoint } from "@/lib/restore";
import { isActiveTask } from "@/types/task";
import type { Restore } from "@/types/restore";

interface RestoreRunProps {
  restore: Restore;
  onShowVerify: () => void;
}

/** Step 4: progress, current step, live log and Cancel of the restore task. */
export default function RestoreRun({ restore, onShowVerify }: RestoreRunProps) {
  const { cancel } = useTasks();
  const task = currentTask(restore);
  const active = !!task && isActiveTask(task);
  const failed = restore.status === "failed" || task?.status === "failed" || task?.status === "rolled_back";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {active && <Loader2 className="h-4 w-4 animate-spin text-primary" />}
          {task?.title ?? "Restore"}
        </CardTitle>
        <CardDescription>
          {restore.job_name} · {describePoint(restore)} · {restore.target_type}
          {describeBase(restore) && ` · ${describeBase(restore)}`}
        </CardDescription>
        <CardAction>
          <RestoreStatusBadge status={restore.status} />
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {task ? (
          <>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="min-w-0 truncate text-muted-foreground">
                  {task.step || (active ? "Starting..." : task.result?.message || task.status)}
                </span>
                <span className="tabular-nums text-muted-foreground">{task.progress}%</span>
              </div>
              <Progress value={task.progress} />
              <p className="text-xs text-muted-foreground">
                {task.started_at
                  ? active
                    ? `Running for ${formatDuration(task.started_at, new Date().toISOString())}`
                    : `Took ${formatDuration(task.started_at, task.finished_at)}`
                  : "Queued"}
              </p>
            </div>
            <TaskLog key={task.id} task={task} className="h-80" />
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No task attached to this restore.</p>
        )}

        {failed && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>{task?.status === "rolled_back" ? "Rolled back" : "Restore failed"}</AlertTitle>
            <AlertDescription className="break-words">
              {restore.error || task?.error || "See the log above."}
            </AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap justify-end gap-2">
          {task?.status === "succeeded" && task.result?.download_url && (
            <Button variant="outline" onClick={() => downloadTaskFile(task)}>
              Download {task.result.filename}
            </Button>
          )}
          {active && task?.cancellable && (
            <Button variant="outline" disabled={task.cancel_requested} onClick={() => cancel(task.id)}>
              {task.cancel_requested ? "Cancelling..." : "Cancel"}
            </Button>
          )}
          {!active && !failed && restore.status !== "cancelled" && (
            <Button onClick={onShowVerify}>
              <CheckCircle2 />
              Go to Verify
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
