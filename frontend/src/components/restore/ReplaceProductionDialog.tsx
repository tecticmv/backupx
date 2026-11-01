import { useEffect, useState } from "react";
import { AlertTriangle, Info, Loader2, ShieldAlert, Timer, Undo2 } from "lucide-react";
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
import { useStepUp } from "@/hooks/useStepUp";
import { useTasks } from "@/hooks/useTasks";
import { ApiError } from "@/lib/api";
import { executeProductionAction, previewProductionAction, type ProductionAction } from "@/lib/restore";
import type { ReplacePreview, Restore } from "@/types/restore";
import type { TaskStartResponse } from "@/types/task";

interface ReplaceProductionDialogProps {
  restore: Restore;
  action: ProductionAction | null;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}

/**
 * Replace production from a ready scratch restore (or undo it): the server's plan
 * (target, source, steps, downtime, safety backup, rollback), typed DB name,
 * step-up, then execute with the plan hash so a changed situation is refused.
 */
export default function ReplaceProductionDialog({ restore, action, onOpenChange, onChanged }: ReplaceProductionDialogProps) {
  return (
    <AlertDialog open={!!action} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-h-[90vh] overflow-y-auto data-[size=default]:sm:max-w-2xl">
        {action && (
          <Body
            key={`${restore.id}:${action}`}
            restore={restore}
            action={action}
            onClose={() => onOpenChange(false)}
            onChanged={onChanged}
          />
        )}
      </AlertDialogContent>
    </AlertDialog>
  );
}

function Body({
  restore,
  action,
  onClose,
  onChanged,
}: {
  restore: Restore;
  action: ProductionAction;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { ensureStepUp, withStepUp } = useStepUp();
  const { track } = useTasks();
  const [preview, setPreview] = useState<ReplacePreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isUndo = action === "undo";

  useEffect(() => {
    let cancelled = false;
    previewProductionAction(restore.id, action)
      .then((data) => !cancelled && setPreview(data))
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "not_ready") {
          setLoadError(isUndo ? "Undo isn't available for this restore any more." : "This restore isn't ready to replace production.");
          return;
        }
        setLoadError(err instanceof Error ? err.message : "Could not load the plan");
      });
    return () => {
      cancelled = true;
    };
  }, [restore.id, action, isUndo]);

  const plan = preview?.plan;
  const confirmText = preview?.confirm_text ?? "";
  const matches = !!confirmText && typed === confirmText;
  const reason = isUndo
    ? `Undo puts ${restore.job_name}'s production database back as it was before the replace`
    : `Replacing production overwrites ${restore.job_name}'s live database`;

  const submit = async () => {
    if (!preview || !matches) return;
    setSubmitting(true);
    setError(null);
    try {
      if (!(await ensureStepUp(reason))) return;
      const response = await withStepUp(
        () => executeProductionAction(restore.id, action, preview.plan_hash, typed),
        reason
      );
      const data = (await response.json().catch(() => ({}))) as Partial<TaskStartResponse> &
        Partial<ReplacePreview> & { code?: string; error?: string };

      if (response.ok && data.task) {
        track(data.task, { onDone: () => onChanged() });
        toast.success(data.message || (isUndo ? "Undo started" : "Replacing production"));
        onChanged();
        onClose();
        return;
      }
      switch (data.code) {
        case "plan_changed":
          if (data.plan && data.plan_hash) {
            const next: ReplacePreview = {
              plan: data.plan,
              plan_hash: data.plan_hash,
              confirm_text: data.confirm_text ?? preview.confirm_text,
            };
            setPreview(next);
          }
          setTyped("");
          setNotice("Something changed since the plan was made. Review the updated plan and confirm again.");
          break;
        case "confirmation_mismatch":
          setTyped("");
          setError("The confirmation text didn't match. Type it again exactly as shown.");
          break;
        case "not_ready":
          setError(data.error || "This restore isn't in a state that allows this any more.");
          onChanged();
          break;
        case "step_up_required":
          setError("Your password is needed for this.");
          break;
        default:
          setError(data.error || `Request failed (${response.status})`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <AlertDialogHeader>
        <AlertDialogTitle className="flex items-center gap-2">
          {isUndo ? <Undo2 className="h-5 w-5 shrink-0" /> : <ShieldAlert className="h-5 w-5 shrink-0" />}
          {isUndo ? "Undo replace production" : "Replace production"}
        </AlertDialogTitle>
        <AlertDialogDescription>
          {isUndo
            ? "Imports the safety backup taken before production was replaced."
            : "Copies the checked scratch database over the live one."}{" "}
          <span className="text-foreground">({restore.job_name})</span>
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
          <Skeleton className="h-24 w-full" />
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

          <div className="rounded-md border">
            <Table>
              <TableBody>
                <TableRow>
                  <TableCell className="w-1/3 align-top font-medium">Production</TableCell>
                  <TableCell className="whitespace-normal break-all">
                    {plan.production.database_config} on <span className="font-mono">{plan.production.host}</span>
                    {plan.production.container && (
                      <> (container <span className="font-mono">{plan.production.container}</span>)</>
                    )}
                    <span className="block text-xs text-muted-foreground">via {plan.production.path}</span>
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="align-top font-medium">Databases</TableCell>
                  <TableCell className="whitespace-normal break-all font-mono text-xs">
                    {plan.production.databases.join(", ") || "—"}
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="align-top font-medium">Source</TableCell>
                  <TableCell className="whitespace-normal break-all">
                    <span className="font-mono text-xs">{plan.source.container}</span>
                    <span className="block text-xs text-muted-foreground">{plan.source.point}</span>
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>

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

          <dl className="space-y-2">
            <div>
              <dt className="font-medium">Safety backup</dt>
              <dd className="text-muted-foreground">{plan.pre_restore}</dd>
            </div>
            <div>
              <dt className="font-medium">If it fails</dt>
              <dd className="text-muted-foreground">{plan.rollback}</dd>
            </div>
          </dl>

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

          <div className="grid gap-2">
            <Label htmlFor="replace-confirm-text">
              Type <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{confirmText}</code> to confirm
            </Label>
            <Input
              id="replace-confirm-text"
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

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
      )}

      <AlertDialogFooter>
        <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
        <Button variant="destructive" onClick={submit} disabled={!plan || !matches || submitting}>
          {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
          {isUndo ? "Undo" : "Replace production"}
        </Button>
      </AlertDialogFooter>
    </>
  );
}
