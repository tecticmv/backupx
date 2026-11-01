import { useEffect, useState } from "react";
import { AlertTriangle, Info, Loader2, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { useStepUp } from "@/hooks/useStepUp";
import { useTasks } from "@/hooks/useTasks";
import { formatBytes } from "@/lib/format";
import { planFileRestore, startFileRestore } from "@/lib/restore";
import type { FilePlanResponse, FileRestoreRequest, Restore, RestoreStartResponse } from "@/types/restore";

const TARGET_TITLE: Record<FileRestoreRequest["target"], string> = {
  original: "Restore files to their original location",
  alternate: "Restore files to another folder",
  scratch: "Extract files on the backupx server",
};

interface FileRestoreDialogProps {
  request: FileRestoreRequest | null;
  onOpenChange: (open: boolean) => void;
  onStarted: (restore: Restore | null) => void;
}

/** File restore: server plan -> (typed "overwrite" for original) -> step-up -> start with plan_hash. */
export default function FileRestoreDialog({ request, onOpenChange, onStarted }: FileRestoreDialogProps) {
  return (
    <AlertDialog open={!!request} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-h-[90vh] overflow-y-auto data-[size=default]:sm:max-w-2xl">
        {request && (
          <Body
            key={JSON.stringify(request)}
            request={request}
            onClose={() => onOpenChange(false)}
            onStarted={onStarted}
          />
        )}
      </AlertDialogContent>
    </AlertDialog>
  );
}

function Body({
  request,
  onClose,
  onStarted,
}: {
  request: FileRestoreRequest;
  onClose: () => void;
  onStarted: (restore: Restore | null) => void;
}) {
  const { ensureStepUp, withStepUp } = useStepUp();
  const { track } = useTasks();
  const [preview, setPreview] = useState<FilePlanResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    planFileRestore(request)
      .then((data) => !cancelled && setPreview(data))
      .catch((err) => !cancelled && setLoadError(err instanceof Error ? err.message : "Could not load the plan"));
    return () => {
      cancelled = true;
    };
  }, [request]);

  const confirmText = preview?.confirm_text ?? "";
  const matches = !confirmText || typed === confirmText;
  const plan = preview?.plan;
  const needsStepUp = request.target !== "scratch";
  const reason = request.target === "original" ? "Restoring overwrites files on the server" : "Restoring writes files on the server";

  const submit = async () => {
    if (!preview || !matches) return;
    setSubmitting(true);
    setError(null);
    try {
      if (needsStepUp && !(await ensureStepUp(reason))) return;
      const body = { ...request, plan_hash: preview.plan_hash, confirm: confirmText ? typed : undefined };
      const response = needsStepUp
        ? await withStepUp(() => startFileRestore(body), reason)
        : await startFileRestore(body);
      const data = (await response.json().catch(() => ({}))) as RestoreStartResponse & { confirm_text?: string };
      if (response.ok && data.task) {
        track(data.task);
        toast.success(data.message || "File restore started");
        onStarted(data.restore ?? null);
        onClose();
        return;
      }
      switch (data.code) {
        case "plan_changed":
          if (data.plan && data.plan_hash) {
            const { plan: newPlan, plan_hash } = data;
            setPreview((prev) => ({
              plan: newPlan,
              plan_hash,
              confirm_text: data.confirm_text ?? prev?.confirm_text,
            }));
          }
          setTyped("");
          setNotice("The snapshot or target changed since the plan was made. Review it and confirm again.");
          break;
        case "confirmation_mismatch":
          setTyped("");
          setError("The confirmation text didn't match. Type it again exactly as shown.");
          break;
        case "step_up_required":
          setError("Your password is needed for this restore.");
          break;
        default:
          setError(data.error || `Request failed (${response.status})`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the restore");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <AlertDialogHeader>
        <AlertDialogTitle className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 shrink-0" />
          {TARGET_TITLE[request.target]}
        </AlertDialogTitle>
        <AlertDialogDescription>
          {request.paths.length} path{request.paths.length === 1 ? "" : "s"} from snapshot{" "}
          <span className="font-mono">{request.snapshot_id.slice(0, 8)}</span>
          {request.target_path && (
            <>
              {" "}into <span className="font-mono text-foreground">{request.target_path}</span>
            </>
          )}
        </AlertDialogDescription>
      </AlertDialogHeader>

      <div className="min-w-0 space-y-4 text-sm">
        <div>
          <h4 className="mb-1.5 font-medium">
            {request.target === "original" ? "These paths will be overwritten" : "Paths"}
          </h4>
          <ScrollArea className="max-h-40 rounded-md border bg-muted/30">
            <ul className="p-2 font-mono text-xs">
              {(plan?.paths ?? request.paths).map((p) => (
                <li key={p} className="break-all">
                  {p}
                </li>
              ))}
            </ul>
          </ScrollArea>
        </div>

        {request.target === "original" && (
          <Alert>
            <Info className="h-4 w-4" />
            <AlertTitle>A safety backup is taken first</AlertTitle>
            <AlertDescription>
              Before anything is overwritten, backupx backs up exactly these paths on the host (tagged pre-restore), so
              you can get the current versions back.
            </AlertDescription>
          </Alert>
        )}

        {loadError ? (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>Could not load the plan</AlertTitle>
            <AlertDescription>{loadError}</AlertDescription>
          </Alert>
        ) : !plan ? (
          <div className="space-y-2" aria-busy="true">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : (
          <>
            {notice && (
              <Alert>
                <Info className="h-4 w-4" />
                <AlertTitle>Plan updated</AlertTitle>
                <AlertDescription>{notice}</AlertDescription>
              </Alert>
            )}
            <p className="text-muted-foreground">
              {plan.summary}
              {plan.estimated_restored_bytes > 0 && ` (~${formatBytes(plan.estimated_restored_bytes)})`}
            </p>
            {plan.steps.length > 0 && (
              <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
                {plan.steps.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            )}
            {plan.disk && !plan.disk.ok && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>Not enough disk space</AlertTitle>
                <AlertDescription>
                  Needs {formatBytes(plan.disk.needed_bytes)}, {formatBytes(plan.disk.free_bytes)} free on{" "}
                  {plan.disk.path}.
                </AlertDescription>
              </Alert>
            )}
            {plan.warnings.length > 0 && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>Before you continue</AlertTitle>
                <AlertDescription>
                  <ul className="list-disc space-y-1 pl-4">
                    {plan.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </AlertDescription>
              </Alert>
            )}
            {confirmText && (
              <div className="grid gap-2">
                <Label htmlFor="file-restore-confirm">
                  Type <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{confirmText}</code> to
                  confirm
                </Label>
                <Input
                  id="file-restore-confirm"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={typed.length > 0 && !matches ? true : undefined}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && matches && !submitting) submit();
                  }}
                />
              </div>
            )}
          </>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </div>

      <AlertDialogFooter>
        <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
        <Button
          variant={request.target === "original" ? "destructive" : "default"}
          onClick={submit}
          disabled={!plan || !matches || submitting}
        >
          {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
          {request.target === "original" ? "Overwrite" : "Restore"}
        </Button>
      </AlertDialogFooter>
    </>
  );
}
