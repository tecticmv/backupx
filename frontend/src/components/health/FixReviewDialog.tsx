import { useEffect, useState } from "react";
import { AlertTriangle, Info, Loader2, ShieldCheck, Timer } from "lucide-react";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import { apiJson, ApiError } from "@/lib/api";
import { formatValue, humanizeKey, newIdempotencyKey } from "@/lib/format";
import { resourceLabel } from "@/lib/health";
import { useStepUp } from "@/hooks/useStepUp";
import { useTasks } from "@/hooks/useTasks";
import type { FixPreviewResponse, Issue, PlanChangedError } from "@/types/health";
import type { TaskStartResponse } from "@/types/task";

interface FixReviewDialogProps {
  issue: Issue | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}

/**
 * Confirm-tier fix: show the server's plan (steps, downtime, details), make the
 * user type the confirmation phrase, re-authenticate (step-up), then execute
 * with the plan hash so the server refuses if the plan changed meanwhile.
 */
export default function FixReviewDialog({ issue, open, onOpenChange, onChanged }: FixReviewDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-h-[90vh] overflow-y-auto data-[size=default]:sm:max-w-2xl">
        {issue && (
          <ReviewBody key={issue.id} issue={issue} onClose={() => onOpenChange(false)} onChanged={onChanged} />
        )}
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ReviewBody({ issue, onClose, onChanged }: { issue: Issue; onClose: () => void; onChanged: () => void }) {
  const { ensureStepUp, withStepUp } = useStepUp();
  const { track } = useTasks();
  const [preview, setPreview] = useState<FixPreviewResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiJson<FixPreviewResponse>("/api/fixes", { method: "POST", json: { issue_id: issue.id, preview: true } })
      .then((data) => {
        if (!cancelled) setPreview(data);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "issue_resolved") {
          toast.info("This issue is already resolved");
          onChanged();
          onClose();
          return;
        }
        setLoadError(err instanceof Error ? err.message : "Could not load the plan");
      });
    return () => {
      cancelled = true;
    };
    // Load once per issue (the component is keyed by issue id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [issue.id]);

  const confirmText = preview?.confirm_text ?? "";
  const matches = !confirmText || typed === confirmText;
  const plan = preview?.plan;
  const reason = plan ? `"${plan.title}" changes ${resourceLabel(issue)}` : undefined;

  const submit = async () => {
    if (!preview || !matches) return;
    setSubmitting(true);
    setError(null);
    try {
      if (!(await ensureStepUp(reason))) return;
      const body = JSON.stringify({
        issue_id: issue.id,
        plan_hash: preview.plan_hash,
        confirm: typed,
        idempotency_key: newIdempotencyKey(),
      });
      const response = await withStepUp(
        () => fetch("/api/fixes", { method: "POST", headers: { "Content-Type": "application/json" }, body }),
        reason
      );
      const data = (await response.json().catch(() => ({}))) as Partial<TaskStartResponse> &
        Partial<Omit<PlanChangedError, "code">> & { code?: string; error?: string };

      if (response.ok && data.task) {
        track(data.task, { onDone: () => onChanged() });
        toast.success(data.message || `${plan?.title ?? "Fix"} started`);
        onChanged();
        onClose();
        return;
      }
      switch (data.code) {
        case "plan_changed":
          if (data.plan && data.plan_hash) {
            const { plan: newPlan, plan_hash } = data;
            setPreview((prev) =>
              prev
                ? { ...prev, plan: newPlan, plan_hash, confirm_text: data.confirm_text ?? prev.confirm_text }
                : prev
            );
          }
          setTyped("");
          setNotice("The situation changed since you opened this. Review the updated plan and confirm again.");
          break;
        case "issue_resolved":
          toast.info("This issue is already resolved");
          onChanged();
          onClose();
          break;
        case "confirmation_mismatch":
          setTyped("");
          setError("The confirmation text didn't match. Type it again exactly as shown.");
          break;
        case "step_up_required":
          setError("Your password is needed to run this fix.");
          break;
        case "manual":
          setError("This issue has to be fixed by hand.");
          break;
        default:
          setError(data.error || `Request failed (${response.status})`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the fix");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <AlertDialogHeader>
        <AlertDialogTitle className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 shrink-0" />
          {plan?.title ?? issue.fix?.label ?? "Review fix"}
        </AlertDialogTitle>
        <AlertDialogDescription>
          {issue.fix?.description || issue.title} <span className="text-foreground">({resourceLabel(issue)})</span>
        </AlertDialogDescription>
      </AlertDialogHeader>

      {loadError ? (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Could not load the plan</AlertTitle>
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      ) : !plan ? (
        <div className="space-y-3" aria-busy="true" aria-label="Loading plan">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : (
        <div className="min-w-0 space-y-4 text-sm">
          {notice && (
            <Alert>
              <Info className="h-4 w-4" />
              <AlertTitle>Plan updated</AlertTitle>
              <AlertDescription>{notice}</AlertDescription>
            </Alert>
          )}

          <div>
            <h4 className="mb-2 font-medium">What will happen</h4>
            <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
              {plan.steps.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          </div>

          <div className="flex items-start gap-2 rounded-md border bg-muted/40 p-3">
            <Timer className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            <div>
              <span className="font-medium">Expected downtime: </span>
              <span className="text-muted-foreground">{plan.downtime}</span>
            </div>
          </div>

          {Object.keys(plan.details ?? {}).length > 0 && (
            <div className="rounded-md border">
              <Table>
                <TableBody>
                  {Object.entries(plan.details).map(([key, value]) => (
                    <TableRow key={key}>
                      <TableCell className="w-1/3 align-top font-medium">{humanizeKey(key)}</TableCell>
                      <TableCell className="whitespace-normal break-all font-mono text-xs">
                        {formatValue(value)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}

          {!!plan.warnings?.length && (
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
              <Label htmlFor="fix-confirm-text">
                Type <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{confirmText}</code> to
                confirm
              </Label>
              <Input
                id="fix-confirm-text"
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

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
      )}

      <AlertDialogFooter>
        <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
        <Button variant="destructive" onClick={submit} disabled={!plan || !matches || submitting}>
          {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
          Confirm
        </Button>
      </AlertDialogFooter>
    </>
  );
}
