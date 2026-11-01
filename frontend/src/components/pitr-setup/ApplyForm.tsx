import { AlertTriangle, Info, KeyRound, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import CopyButton from "@/components/health/CopyButton";
import PlanStepsSummary from "@/components/pitr-setup/PlanStepsSummary";
import RestartWindowPicker from "@/components/pitr-setup/RestartWindowPicker";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { PitrPlanResponse, RestartMode } from "@/types/pitrSetup";

export type AdminMode = "socket" | "password" | "self";

export interface ApplyFormProps {
  plan: PitrPlanResponse;
  planLoading: boolean;
  planError: string | null;
  selectedCount: number;

  adminMode: AdminMode;
  onAdminMode: (mode: AdminMode) => void;
  username: string;
  onUsername: (v: string) => void;
  password: string;
  onPassword: (v: string) => void;
  /** Items still needing admin although the user runs the grant SQL himself. */
  adminStillNeeded: string[];

  restart: RestartMode;
  onRestart: (mode: RestartMode) => void;
  restartAtMs: number;
  onRestartAt: (ms: number) => void;

  freshForce: boolean;
  onFreshForce: (v: boolean) => void;

  typed: string;
  onTyped: (v: string) => void;

  notice: string | null;
  error: string | null;
  staleCheck: boolean;
  onRecheck: () => void;
  rechecking: boolean;

  submitting: boolean;
  canSubmit: boolean;
  onSubmit: () => void;
}

function Choice({
  id,
  value,
  title,
  help,
  disabled,
}: {
  id: string;
  value: string;
  title: string;
  help: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <Label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60"
    >
      <RadioGroupItem value={value} id={id} className="mt-0.5" disabled={disabled} />
      <div className="grid gap-1">
        <span className="font-medium">{title}</span>
        <span className="text-sm text-muted-foreground">{help}</span>
      </div>
    </Label>
  );
}

/** Apply step (before the run): admin credential, restart window, fresh backup, typed confirmation. */
export default function ApplyForm(props: ApplyFormProps) {
  const { plan, adminMode } = props;
  const showAdmin = plan.admin.needed || adminMode === "self";
  const confirmMatches = props.typed === plan.confirm_text;
  const sel = plan.selection;

  return (
    <div className="space-y-4">
      {props.notice && (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>Plan updated</AlertTitle>
          <AlertDescription>{props.notice}</AlertDescription>
        </Alert>
      )}

      <PlanStepsSummary steps={plan.steps} downtime={plan.downtime} loading={props.planLoading} error={props.planError} />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4" />
            Apply to {plan.target.database}
          </CardTitle>
          <CardDescription>
            Needs your password again (step-up). Admin credentials are used for this run only and never stored.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6 text-sm">
          {showAdmin && (
            <section className="space-y-3">
              <h4 className="flex items-center gap-2 font-medium">
                <KeyRound className="h-4 w-4" />
                One-time admin access
              </h4>
              <p className="text-muted-foreground">
                Some changes need a database admin
                {plan.admin.account && (
                  <>
                    {" "}
                    (the backup account <code className="font-mono text-xs">{plan.admin.account}</code> can't grant
                    itself privileges)
                  </>
                )}
                .
              </p>
              <RadioGroup
                value={adminMode}
                onValueChange={(v) => props.onAdminMode(v as AdminMode)}
                className="gap-3"
                disabled={props.submitting}
              >
                {plan.admin.socket_available && (
                  <Choice
                    id="pitr-admin-socket"
                    value="socket"
                    title="Use the server's root via the local socket"
                    help="backupx connects as root over SSH + sudo on the database host; no password needed."
                  />
                )}
                <Choice
                  id="pitr-admin-password"
                  value="password"
                  title="Admin username and password"
                  help="Held in memory for this run only; never saved, logged or put in the task."
                />
                <Choice
                  id="pitr-admin-self"
                  value="self"
                  title="I'll run the SQL myself"
                  help="Run the statement below as an admin first; the grant step is then left out of the plan."
                />
              </RadioGroup>

              {adminMode === "password" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="grid gap-2">
                    <Label htmlFor="pitr-admin-user">Admin username</Label>
                    <Input
                      id="pitr-admin-user"
                      value={props.username}
                      onChange={(e) => props.onUsername(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                      disabled={props.submitting}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="pitr-admin-password-input">Admin password</Label>
                    <Input
                      id="pitr-admin-password-input"
                      type="password"
                      value={props.password}
                      onChange={(e) => props.onPassword(e.target.value)}
                      autoComplete="new-password"
                      disabled={props.submitting}
                    />
                  </div>
                </div>
              )}

              {adminMode === "self" && plan.admin.sql && (
                <div className="flex items-start gap-1 rounded-md border bg-muted/40">
                  <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre p-2 font-mono text-xs">{plan.admin.sql}</pre>
                  <CopyButton text={plan.admin.sql} label="Copy SQL" />
                </div>
              )}
              {adminMode === "self" && props.adminStillNeeded.length > 0 && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>Other selected changes also need admin access</AlertTitle>
                  <AlertDescription>
                    {props.adminStillNeeded.join(", ")}. Pick another option above, or deselect them in step 2.
                  </AlertDescription>
                </Alert>
              )}
            </section>
          )}

          {plan.needs_restart && (
            <>
              {showAdmin && <Separator />}
              <section className="space-y-3">
                <h4 className="font-medium">When to restart</h4>
                {!plan.target.restart_supported && plan.target.restart_unsupported_reason && (
                  <Alert variant="destructive">
                    <AlertTriangle className="h-4 w-4" />
                    <AlertTitle>backupx can't restart this database</AlertTitle>
                    <AlertDescription>{plan.target.restart_unsupported_reason}</AlertDescription>
                  </Alert>
                )}
                <RadioGroup
                  value={props.restart}
                  onValueChange={(v) => props.onRestart(v as RestartMode)}
                  className="gap-3"
                  disabled={props.submitting}
                >
                  <Choice
                    id="pitr-restart-now"
                    value="now"
                    title="Now"
                    help={`Restart right after the changes are applied. ${plan.downtime}.`}
                  />
                  <Choice
                    id="pitr-restart-at"
                    value="at"
                    title="In a maintenance window"
                    help="Apply the no-downtime changes now and restart at the time you pick. You can cancel the restart until it starts."
                  />
                </RadioGroup>
                {props.restart === "at" && (
                  <RestartWindowPicker value={props.restartAtMs} onChange={props.onRestartAt} disabled={props.submitting} />
                )}
              </section>
            </>
          )}

          {(showAdmin || plan.needs_restart) && <Separator />}

          <section className="space-y-3">
            <h4 className="font-medium">Fresh backup</h4>
            <p className="text-muted-foreground">
              {sel.fresh_backup === "run"
                ? "A fresh full backup runs before anything changes, so there's a restore point from just before the setup."
                : `The last backup (${formatRelative(sel.last_backup_at)}, ${formatDateTime(sel.last_backup_at)}) is less than an hour old and is used as the restore point.`}
              {sel.fresh_backup === "run" && sel.last_backup_at && (
                <> Last backup: {formatRelative(sel.last_backup_at)}.</>
              )}
            </p>
            {(sel.fresh_backup === "skip" || props.freshForce) && (
              <div className="flex items-center gap-3">
                <Switch
                  id="pitr-fresh-backup"
                  checked={props.freshForce}
                  onCheckedChange={props.onFreshForce}
                  disabled={props.submitting}
                />
                <Label htmlFor="pitr-fresh-backup" className="font-normal">
                  Take a fresh backup anyway
                </Label>
              </div>
            )}
          </section>

          <Separator />

          <section className="grid gap-2">
            <Label htmlFor="pitr-confirm-text">
              Type <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{plan.confirm_text}</code> to
              confirm
            </Label>
            <Input
              id="pitr-confirm-text"
              value={props.typed}
              onChange={(e) => props.onTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="max-w-md"
              disabled={props.submitting}
              aria-invalid={props.typed.length > 0 && !confirmMatches ? true : undefined}
              onKeyDown={(e) => {
                if (e.key === "Enter" && props.canSubmit) props.onSubmit();
              }}
            />
          </section>

          {props.error && (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>{props.staleCheck ? "The check results are too old" : "Could not apply"}</AlertTitle>
              <AlertDescription className="space-y-2">
                <p>{props.error}</p>
                {props.staleCheck && (
                  <Button size="sm" variant="outline" onClick={props.onRecheck} disabled={props.rechecking}>
                    {props.rechecking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                    Re-run checks
                  </Button>
                )}
              </AlertDescription>
            </Alert>
          )}

          <div className="flex flex-wrap items-center justify-end gap-3">
            {props.selectedCount === 0 && <span className="text-xs text-muted-foreground">Select at least one change in step 2.</span>}
            <Button variant="destructive" onClick={props.onSubmit} disabled={!props.canSubmit}>
              {props.submitting && <Loader2 className="animate-spin" />}
              Apply
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
