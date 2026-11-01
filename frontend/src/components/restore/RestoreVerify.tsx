import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Download,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Trash2,
  Undo2,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import CopyButton from "@/components/health/CopyButton";
import ReplaceProductionDialog from "@/components/restore/ReplaceProductionDialog";
import RestoreStatusBadge from "@/components/restore/RestoreStatusBadge";
import VerifyConfigForm from "@/components/restore/VerifyConfigForm";
import VerifyTable from "@/components/restore/VerifyTable";
import { useTasks } from "@/hooks/useTasks";
import { formatDateTime, formatRelative } from "@/lib/format";
import {
  describePoint,
  dumpRestore,
  extendRestore,
  isRestoreActive,
  removeRestore,
  reverifyRestore,
  type ProductionAction,
} from "@/lib/restore";
import type { Restore } from "@/types/restore";

type Busy = "dump" | "verify" | "extend" | "remove" | null;

interface RestoreVerifyProps {
  restore: Restore;
  onChanged: () => void;
}

/** Step 5: compare the restored copy with production, then keep / download / remove / replace production. */
export default function RestoreVerify({ restore, onChanged }: RestoreVerifyProps) {
  const { track } = useTasks();
  const [busy, setBusy] = useState<Busy>(null);
  const [action, setAction] = useState<ProductionAction | null>(null);
  const verify = restore.verify;
  const isDb = restore.engine !== "files";
  const scratchUp = restore.status === "ready" || restore.status === "replaced" || restore.status === "undone";
  const gone = ["removed", "expired", "cancelled", "failed"].includes(restore.status);

  const run = async (kind: Exclude<Busy, null>, fn: () => Promise<unknown>) => {
    setBusy(kind);
    try {
      await fn();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(null);
    }
  };

  const downloadDump = () =>
    run("dump", async () => {
      const data = await dumpRestore(restore.id);
      track(data.task, { autoDownload: true });
      toast.success("Preparing the dump - the download starts when it's ready");
    });

  const reverify = () =>
    run("verify", async () => {
      const data = await reverifyRestore(restore.id);
      track(data.task, { onDone: onChanged });
      toast.success("Re-running the checks");
    });

  const extend = () =>
    run("extend", async () => {
      const r = await extendRestore(restore.id, 24);
      toast.success(`Kept until ${formatDateTime(r.cleanup_at)}`);
      onChanged();
    });

  const remove = () =>
    run("remove", async () => {
      const data = await removeRestore(restore.id);
      track(data.task, { onDone: onChanged });
      toast.success("Removing the scratch copy");
      onChanged();
    });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {restore.job_name} · {describePoint(restore)}
          </CardTitle>
          <CardDescription>
            {restore.container ? (
              <>
                Container <span className="font-mono">{restore.container}</span> on {restore.server}
              </>
            ) : restore.work_dir ? (
              <>
                Extracted to <span className="font-mono">{restore.work_dir}</span> on {restore.server}
              </>
            ) : (
              restore.plan?.where
            )}
          </CardDescription>
          <CardAction>
            <RestoreStatusBadge status={restore.status} />
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          {restore.cleanup_at && !gone && (
            <p className="flex items-center gap-2 text-muted-foreground">
              <Clock className="h-4 w-4" />
              Removed automatically {formatRelative(restore.cleanup_at)} ({formatDateTime(restore.cleanup_at)})
            </p>
          )}

          {restore.connect_hint && !gone && (
            <div className="flex items-center gap-2 rounded-md border bg-muted/40 p-2 pl-3">
              <code className="min-w-0 flex-1 break-all font-mono text-xs">{restore.connect_hint}</code>
              <CopyButton text={restore.connect_hint} label="Copy command" />
            </div>
          )}

          {restore.error && (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Error</AlertTitle>
              <AlertDescription className="break-words">{restore.error}</AlertDescription>
            </Alert>
          )}

          <div className="flex flex-wrap gap-2">
            {isDb && scratchUp && (
              <Button variant="outline" size="sm" onClick={downloadDump} disabled={!!busy}>
                {busy === "dump" ? <Loader2 className="animate-spin" /> : <Download />}
                Download dump
              </Button>
            )}
            {isDb && scratchUp && (
              <Button variant="outline" size="sm" onClick={reverify} disabled={!!busy}>
                {busy === "verify" ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                Re-run checks
              </Button>
            )}
            {restore.cleanup_at && !gone && (
              <Button variant="outline" size="sm" onClick={extend} disabled={!!busy}>
                {busy === "extend" ? <Loader2 className="animate-spin" /> : <Clock />}
                Keep 24 h longer
              </Button>
            )}
            {!gone && !isRestoreActive(restore.status) && (restore.container || restore.work_dir) && (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button variant="outline" size="sm" disabled={!!busy}>
                    {busy === "remove" ? <Loader2 className="animate-spin" /> : <Trash2 />}
                    Remove now
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Remove the scratch copy?</AlertDialogTitle>
                    <AlertDialogDescription>
                      {restore.container
                        ? `The container ${restore.container} and its data are deleted from the backupx server.`
                        : `${restore.work_dir} is deleted from ${restore.server}.`}{" "}
                      Production is not touched.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={remove}>Remove</AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}
            {restore.can_replace && (
              <Button variant="destructive" size="sm" className="ml-auto" onClick={() => setAction("replace")}>
                <ShieldAlert />
                Replace production...
              </Button>
            )}
            {restore.can_undo && (
              <Button variant="destructive" size="sm" className="ml-auto" onClick={() => setAction("undo")}>
                <Undo2 />
                Undo replace...
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {restore.pre_restore_snapshot && restore.pre_restore_snapshot.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Safety backup of production</CardTitle>
            <CardDescription>
              Taken before production was replaced
              {restore.pre_restore_expires_at && `; kept until ${formatDateTime(restore.pre_restore_expires_at)}`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            {restore.pre_restore_snapshot.map((s) => (
              <p key={`${s.storage_id}:${s.snapshot_id}`}>
                <Badge variant="outline" className="mr-2 font-mono text-xs">
                  {s.snapshot_id.slice(0, 8)}
                </Badge>
                {s.storage_name}
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      {isDb && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Checks</CardTitle>
            <CardDescription>
              {verify
                ? `${verify.phase === "after_replace" ? "After replacing production" : "Scratch copy vs production"} · checked ${formatRelative(verify.checked_at)} · ${verify.summary.tables} tables, ${verify.summary.key_tables} key, ${verify.summary.key_mismatches} key mismatches`
                : "No checks yet."}
            </CardDescription>
            {verify && (
              <CardAction>
                {verify.summary.key_mismatches === 0 ? (
                  <Badge variant="outline" className="border-emerald-500/50 text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 /> Key tables match
                  </Badge>
                ) : (
                  <Badge variant="destructive">
                    <XCircle /> {verify.summary.key_mismatches} mismatches
                  </Badge>
                )}
              </CardAction>
            )}
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            {verify?.prod_error && (
              <Alert>
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>Production couldn't be queried</AlertTitle>
                <AlertDescription className="break-words">
                  {verify.prod_error} The restored copy is still usable; only the comparison is missing.
                </AlertDescription>
              </Alert>
            )}
            {verify && <VerifyTable tables={verify.tables} />}
            {verify?.check_sql && (
              <div className="space-y-2 rounded-md border p-3">
                <div className="flex items-center justify-between">
                  <span className="font-medium">Check SQL</span>
                  {verify.check_sql.match === null ? (
                    <Badge variant="outline">n/a</Badge>
                  ) : verify.check_sql.match ? (
                    <Badge variant="outline" className="border-emerald-500/50 text-emerald-600 dark:text-emerald-400">
                      same output
                    </Badge>
                  ) : (
                    <Badge variant="destructive">different output</Badge>
                  )}
                </div>
                {verify.check_sql.error && <p className="text-destructive">{verify.check_sql.error}</p>}
                <div className="grid gap-2 md:grid-cols-2">
                  <div>
                    <p className="text-xs text-muted-foreground">Production</p>
                    <pre className="max-h-40 overflow-auto rounded bg-muted/40 p-2 font-mono text-xs whitespace-pre-wrap">
                      {verify.check_sql.prod ?? "—"}
                    </pre>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Restored</p>
                    <pre className="max-h-40 overflow-auto rounded bg-muted/40 p-2 font-mono text-xs whitespace-pre-wrap">
                      {verify.check_sql.restored ?? "—"}
                    </pre>
                  </div>
                </div>
              </div>
            )}
            {verify?.health_url && (
              <p className={verify.health_url.ok ? "text-muted-foreground" : "text-destructive"}>
                Health URL <span className="font-mono">{verify.health_url.url}</span>:{" "}
                {verify.health_url.ok ? `OK (${verify.health_url.status})` : verify.health_url.error || `HTTP ${verify.health_url.status ?? "?"}`}
              </p>
            )}
            <VerifyConfigForm jobId={restore.job_id} />
          </CardContent>
        </Card>
      )}

      <ReplaceProductionDialog
        restore={restore}
        action={action}
        onOpenChange={(o) => !o && setAction(null)}
        onChanged={onChanged}
      />
    </div>
  );
}
