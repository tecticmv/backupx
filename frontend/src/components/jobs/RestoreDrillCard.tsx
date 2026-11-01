import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FlaskConical, Loader2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useTasks } from "@/hooks/useTasks";
import { apiJson, ApiError } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { Task } from "@/types/task";

export interface DrillResult {
  ok: boolean | null;
  skipped?: boolean | null;
  warning?: string | null;
  started_at: string | null;
  finished_at: string | null;
  target_time: string | null;
  summary: string | null;
  method: string | null;
  storage: string | null;
  duration_s: number | null;
  task_id: string | null;
  error: string | null;
  tables_compared: number | null;
  tables: number | null;
  mismatches: { table: string; production: string; restored: string | null }[];
  mode?: string | null;
  latest?: { ok: boolean; tables: number; errors: string[]; target_time: string | null } | null;
}

interface DrillInfo {
  last: DrillResult | null;
  enabled: boolean;
  cron: string;
}

export function DrillBadge({ result }: { result: DrillResult | null | undefined }) {
  if (!result) return <Badge variant="outline">never</Badge>;
  if (result.skipped)
    return (
      <Badge variant="outline" className="gap-1">
        <AlertTriangle className="h-3 w-3" /> skipped
      </Badge>
    );
  return result.ok ? (
    <Badge variant="secondary" className="gap-1">
      <CheckCircle2 className="h-3 w-3" /> verified
    </Badge>
  ) : (
    <Badge variant="destructive" className="gap-1">
      <XCircle className="h-3 w-3" /> failed
    </Badge>
  );
}

/** Job page: the last restore drill (scratch restore + checksum comparison) and "Run drill now". */
export default function RestoreDrillCard({ jobId }: { jobId: string }) {
  const { track } = useTasks();
  const [info, setInfo] = useState<DrillInfo | null>(null);
  const [starting, setStarting] = useState(false);
  const url = `/api/jobs/${encodeURIComponent(jobId)}/restore-drill`;

  const load = useCallback(async () => {
    try {
      setInfo(await apiJson<DrillInfo>(url));
    } catch {
      setInfo(null);
    }
  }, [url]);

  useEffect(() => {
    void load();
  }, [load]);

  async function start() {
    setStarting(true);
    try {
      const res = await apiJson<{ task: Task }>(url, { method: "POST", json: {} });
      track(res.task, { onDone: () => void load() });
      toast.success("Restore drill started");
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Could not start the drill");
    } finally {
      setStarting(false);
    }
  }

  const last = info?.last;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FlaskConical className="h-5 w-5" />
          Restore drill <DrillBadge result={last} />
        </CardTitle>
        <CardDescription>
          Backups only, never production: restores the newest nightly backup alone and the point-in-time chain to
          the same point into scratch copies on the backupx server and compares every table (checksums), then checks
          that a restore to the latest point applies cleanly.{" "}
          {info?.enabled ? `Runs weekly (cron ${info.cron}).` : "Weekly drills are off (RESTORE_DRILL_ENABLED)."}
        </CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" onClick={() => void start()} disabled={starting}>
            {starting ? <Loader2 className="animate-spin" /> : <FlaskConical />}
            Run drill now
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        {last ? (
          <>
            <p>
              {last.skipped ? "Last drill (skipped)" : "Last verified restore"}: {formatDateTime(last.finished_at)} (
              {formatRelative(last.finished_at)})
            </p>
            <p className={last.ok || last.skipped ? "text-muted-foreground" : "text-destructive"}>{last.summary}</p>
            {last.mismatches.length > 0 && (
              <ul className="list-disc pl-5 text-destructive">
                {last.mismatches.map((m) => (
                  <li key={m.table}>
                    {m.table}: {last.mode === "backup-vs-backup" ? "backup" : "production"} {m.production},{" "}
                    {last.mode === "backup-vs-backup" ? "point-in-time" : "restored"} {m.restored ?? "missing"}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="text-muted-foreground">No drill has run yet.</p>
        )}
      </CardContent>
    </Card>
  );
}
