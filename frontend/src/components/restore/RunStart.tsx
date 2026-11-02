import { AlertTriangle, Download, Info, Loader2, Play } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import PlanSummary from "@/components/restore/PlanSummary";
import { formatUtc } from "@/lib/restore";
import type { PlanResponse, RestoreTarget } from "@/types/restore";

interface RunStartProps {
  target: RestoreTarget;
  /** Download of a time point: the PITR restore script, no plan. */
  scriptTime: string | null;
  plan: PlanResponse | null;
  planLoading: boolean;
  planError: string | null;
  notice: string | null;
  error: string | null;
  starting: boolean;
  onStart: () => void;
  /** Scratch database restores: compare with production afterwards (reads production row counts; off by default). */
  compareProduction?: boolean;
  onCompareProductionChange?: (value: boolean) => void;
}

/** Step 4 before anything runs: the final plan and the Start button. */
export default function RunStart(props: RunStartProps) {
  const { target, scriptTime, plan, planLoading, planError, notice, error, starting, onStart } = props;
  const { compareProduction, onCompareProductionChange } = props;

  if (target === "download" && scriptTime) {
    return (
      <div className="space-y-4">
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>Restore script</AlertTitle>
          <AlertDescription>
            Downloads a script that rebuilds the database as of {formatUtc(scriptTime)} in a new local docker
            container. It never touches production.
          </AlertDescription>
        </Alert>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div className="flex justify-end">
          <Button onClick={onStart} disabled={starting}>
            {starting ? <Loader2 className="animate-spin" /> : <Download />}
            Download restore script
          </Button>
        </div>
      </div>
    );
  }

  const canStart = !!plan?.ok && !planLoading && !starting;
  return (
    <div className="space-y-4">
      {notice && (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>Plan updated</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      <PlanSummary
        plan={plan?.plan ?? null}
        loading={planLoading}
        error={planError ?? (plan && !plan.ok ? plan.error ?? "This restore can't run." : null)}
      />
      {error && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Could not start</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {target === "scratch" && (plan?.plan || planLoading) && plan?.plan?.engine !== "files" && onCompareProductionChange && (
        <div className="flex items-start gap-2">
          <Checkbox
            id="restore-compare-production"
            checked={compareProduction ?? false}
            onCheckedChange={(v) => onCompareProductionChange(v === true)}
          />
          <div className="grid gap-1">
            <Label htmlFor="restore-compare-production">Compare row counts with production afterwards</Label>
            <p className="text-xs text-muted-foreground">
              Off by default: the restored copy is checked on its own. Tick to also run read-only count queries on the production database.
            </p>
          </div>
        </div>
      )}
      <div className="flex justify-end">
        <Button onClick={onStart} disabled={!canStart}>
          {starting ? <Loader2 className="animate-spin" /> : target === "download" ? <Download /> : <Play />}
          {notice ? "Confirm and start" : target === "download" ? "Build dump and download" : "Start scratch restore"}
        </Button>
      </div>
    </div>
  );
}
