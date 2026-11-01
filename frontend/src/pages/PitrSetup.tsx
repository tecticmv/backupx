import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, ArrowRight, History, Info, Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import PitrPrereqTable from "@/components/PitrPrereqTable";
import ApplyForm, { type AdminMode } from "@/components/pitr-setup/ApplyForm";
import DonePanel from "@/components/pitr-setup/DonePanel";
import EnableRunPanel from "@/components/pitr-setup/EnableRunPanel";
import PlanItems from "@/components/pitr-setup/PlanItems";
import PlanStepsSummary from "@/components/pitr-setup/PlanStepsSummary";
import TargetCard from "@/components/pitr-setup/TargetCard";
import { useStepUp } from "@/hooks/useStepUp";
import { useTasks } from "@/hooks/useTasks";
import { ApiError } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { applyPitr, defaultWindowMs, getPitrEnable, isRunActive, planPitr, stepForRun } from "@/lib/pitrSetup";
import { formatLocal } from "@/lib/restore";
import type { Task } from "@/types/task";
import {
  PITR_SETUP_STEPS,
  type PitrAdminCredential,
  type PitrApplyRequest,
  type PitrApplyResponse,
  type PitrEnableResponse,
  type PitrPlanResponse,
  type PitrSelectionRequest,
  type PitrSetupStep,
  type RestartMode,
} from "@/types/pitrSetup";

const RUN_POLL_MS = 3000;
const SCHEDULED_POLL_MS = 15000;
const PLAN_DEBOUNCE_MS = 500;

const STEP_LABEL: Record<PitrSetupStep, string> = {
  check: "1. Check",
  plan: "2. Plan",
  apply: "3. Apply",
  restart: "4. Restart",
  stream: "5. Stream",
  anchor: "6. Anchor",
  done: "7. Done",
};

const CHECK_ERROR_TITLE: Record<string, string> = {
  ssh_failed: "Could not reach the server over SSH",
  not_database: "Not a database job",
  no_server: "The job has no server",
};

/** Keys of the plan response that /apply may send back with 409 plan_changed. */
const NON_PLAN_KEYS = new Set(["code", "error", "success", "task", "message"]);

function selectionRequest(items: string[], restart: RestartMode, restartAtMs: number, fresh: boolean): PitrSelectionRequest {
  return {
    items: [...items].sort(),
    restart,
    ...(restart === "at" ? { restart_at: new Date(restartAtMs).toISOString() } : {}),
    fresh_backup: fresh,
  };
}

function errorInfo(err: unknown, fallback: string) {
  return {
    message: err instanceof Error ? err.message : fallback,
    code: err instanceof ApiError ? err.code : undefined,
  };
}

/** Run the wizard fresh for each job (all state is per job). */
export default function PitrSetupPage() {
  const { jobId = "" } = useParams<{ jobId: string }>();
  return <PitrSetup key={jobId} jobId={jobId} />;
}

function PitrSetup({ jobId }: { jobId: string }) {
  const { track, cancel } = useTasks();
  const { ensureStepUp, withStepUp } = useStepUp();
  const [params, setParams] = useSearchParams();
  const stepParam = params.get("step") as PitrSetupStep | null;

  const setStep = useCallback(
    (step: PitrSetupStep | null) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (step) next.set("step", step);
          else next.delete("step");
          return next;
        },
        { replace: true }
      ),
    [setParams]
  );

  // ---------- selection ----------
  const [selected, setSelected] = useState<string[] | null>(null);
  const [restart, setRestart] = useState<RestartMode>("now");
  const [restartAtMs, setRestartAtMs] = useState<number>(defaultWindowMs);
  const [freshForce, setFreshForce] = useState(false);

  // ---------- check (refresh) + plan ----------
  const [planState, setPlanState] = useState<{ key: string; data: PitrPlanResponse } | null>(null);
  const [checking, setChecking] = useState(true);
  const [checkError, setCheckError] = useState<{ message: string; code?: string } | null>(null);
  const [replanError, setReplanError] = useState<{ key: string; message: string; code?: string } | null>(null);

  const acceptCheck = useCallback((data: PitrPlanResponse, fresh: boolean) => {
    const items = data.selection.items;
    const mode = data.selection.restart ?? "now";
    const at = data.selection.restart_at ? Date.parse(data.selection.restart_at) : null;
    setSelected(items);
    setRestart(mode);
    let atMs: number | null = null;
    if (at !== null && !Number.isNaN(at)) {
      atMs = at;
      setRestartAtMs(at);
    }
    setPlanState({ key: JSON.stringify(selectionRequest(items, mode, atMs ?? 0, fresh)), data });
    setCheckError(null);
    setReplanError(null);
  }, []);

  // First check on load (SSH probe: can take a couple of minutes)
  useEffect(() => {
    const controller = new AbortController();
    planPitr(jobId, { refresh: true }, controller.signal)
      .then((data) => acceptCheck(data, false))
      .catch((err) => {
        if (!controller.signal.aborted) setCheckError(errorInfo(err, "Could not check the server"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setChecking(false);
      });
    return () => controller.abort();
  }, [jobId, acceptCheck]);

  const recheck = useCallback(async () => {
    setChecking(true);
    setCheckError(null);
    try {
      // The host may have changed (fixed by hand...): let the server pick the default selection again
      const data = await planPitr(jobId, { refresh: true, fresh_backup: freshForce });
      acceptCheck(data, freshForce);
    } catch (err) {
      setCheckError(errorInfo(err, "Could not check the server"));
    } finally {
      setChecking(false);
    }
  }, [jobId, freshForce, acceptCheck]);

  const plan = planState?.data ?? null;
  const planKey = planState?.key ?? null;
  const selKey = plan && selected ? JSON.stringify(selectionRequest(selected, restart, restartAtMs, freshForce)) : null;

  // Selection changed: re-plan from the cached facts (debounced; the previous plan stays shown meanwhile)
  useEffect(() => {
    if (!selKey || !planKey || selKey === planKey) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      planPitr(jobId, { ...(JSON.parse(selKey) as PitrSelectionRequest), refresh: false }, controller.signal)
        .then((data) => {
          setPlanState({ key: selKey, data });
          setReplanError(null);
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setReplanError({ key: selKey, ...errorInfo(err, "Could not update the plan") });
        });
    }, PLAN_DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [jobId, selKey, planKey]);

  const planLoading = !!selKey && !!planKey && selKey !== planKey && replanError?.key !== selKey;
  const planError = replanError && replanError.key === selKey ? replanError : null;

  // ---------- the enable run (resumable, polled while active) ----------
  const [enable, setEnable] = useState<PitrEnableResponse | null>(null);
  const [attached, setAttached] = useState(false);
  const [enableTick, setEnableTick] = useState(0);
  const lastSeen = useRef<{ key: string | null; active: boolean } | undefined>(undefined);
  // Task just started here: ignore answers that still show an older run
  const expectedTaskId = useRef<string | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const loop = async () => {
      try {
        const data = await getPitrEnable(jobId);
        if (stopped) return;
        if (expectedTaskId.current && data.run?.task.id !== expectedTaskId.current) {
          timer = window.setTimeout(loop, RUN_POLL_MS);
          return;
        }
        expectedTaskId.current = null;
        const run = data.run;
        const active = isRunActive(run);
        const key = run ? `${run.task.id}:${run.phase}:${run.restart_task?.id ?? ""}` : null;
        const prev = lastSeen.current;
        lastSeen.current = { key, active };
        if (prev?.key !== key && (active || prev?.active)) {
          // An active run (found on load, started here or elsewhere) or one that just finished:
          // show it and let the tab follow its phase
          setAttached(true);
          setStep(null);
        }
        setEnable(data);
        if (active) timer = window.setTimeout(loop, run?.phase === "scheduled" ? SCHEDULED_POLL_MS : RUN_POLL_MS);
      } catch {
        if (!stopped && lastSeen.current?.active) timer = window.setTimeout(loop, RUN_POLL_MS * 2);
      }
    };
    loop();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, enableTick, setStep]);

  const refreshEnable = () => setEnableTick((n) => n + 1);

  const attachTask = (task: Task) => {
    expectedTaskId.current = task.id;
    lastSeen.current = { key: `${task.id}:apply:`, active: true };
    setAttached(true);
    setEnable((prev) => ({
      pitr: prev?.pitr ?? null,
      run: { task, restart_task: null, phase: "apply", scheduled_for: null, active_task_id: task.id, result: null },
    }));
    setStep(null);
    refreshEnable();
  };

  const run = enable?.run ?? null;
  const shownRun = attached ? run : null;
  const runActive = isRunActive(shownRun);

  // ---------- apply form ----------
  const [adminModeState, setAdminModeState] = useState<AdminMode | null>(null);
  const [adminUser, setAdminUser] = useState("root");
  const [adminPassword, setAdminPassword] = useState("");
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [staleCheck, setStaleCheck] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const socketOk = !!plan?.admin.socket_available;
  const adminMode: AdminMode =
    adminModeState === "socket" && !socketOk ? "password" : (adminModeState ?? (socketOk ? "socket" : "password"));

  const grantIds = useMemo(() => (plan?.items ?? []).filter((i) => i.action === "grant").map((i) => i.id), [plan]);

  const onAdminMode = (mode: AdminMode) => {
    const wasSelf = adminMode === "self";
    setAdminModeState(mode);
    if (mode === "self") {
      // The user runs the GRANT himself: leave the grant step out
      setSelected((prev) => (prev ? prev.filter((id) => !grantIds.includes(id)) : prev));
    } else if (wasSelf && plan) {
      const restore = plan.items.filter((i) => i.action === "grant" && i.auto && i.default_selected).map((i) => i.id);
      setSelected((prev) => (prev ? [...prev, ...restore.filter((id) => !prev.includes(id))] : prev));
    }
    if (mode !== "password") setAdminPassword("");
  };

  const toggleItem = (id: string, checked: boolean) =>
    setSelected((prev) => {
      const list = prev ?? [];
      return checked ? (list.includes(id) ? list : [...list, id]) : list.filter((x) => x !== id);
    });

  const selectedItems = useMemo(
    () => (plan && selected ? plan.items.filter((i) => i.auto && selected.includes(i.id)) : []),
    [plan, selected]
  );
  const adminStillNeeded =
    adminMode === "self" ? selectedItems.filter((i) => i.needs_admin && i.action !== "grant").map((i) => i.title) : [];

  const adminNeeded = !!plan?.admin.needed;
  const adminOk =
    !adminNeeded ||
    (adminMode === "socket" && socketOk) ||
    (adminMode === "password" && !!adminUser.trim() && !!adminPassword);

  const canSubmit =
    !!plan &&
    !!selected &&
    selectedItems.length > 0 &&
    !planLoading &&
    !planError &&
    !submitting &&
    !runActive &&
    typed === plan.confirm_text &&
    adminOk;

  const submit = async () => {
    if (!plan || !selected || !canSubmit) return;
    setSubmitting(true);
    setApplyError(null);
    setStaleCheck(false);
    const reason = `Enabling point-in-time recovery changes ${plan.target.database}`;
    try {
      if (!(await ensureStepUp(reason))) return;
      const admin: PitrAdminCredential = !adminNeeded
        ? null
        : adminMode === "socket"
          ? { mode: "socket" }
          : adminMode === "password"
            ? { mode: "password", username: adminUser.trim(), password: adminPassword }
            : null;
      const body: PitrApplyRequest = {
        ...selectionRequest(selected, restart, restartAtMs, freshForce),
        plan_hash: plan.plan_hash,
        confirm: typed,
        admin,
      };
      let response: Response;
      try {
        response = await withStepUp(() => applyPitr(jobId, body), reason);
      } finally {
        // Never keep the admin password around once it was sent
        setAdminPassword("");
        if (body.admin && body.admin.mode === "password") body.admin.password = "";
      }
      const data = (await response.json().catch(() => ({}))) as PitrApplyResponse;

      if (response.ok && data.task) {
        track(data.task);
        toast.success(data.message || "PITR setup started");
        setTyped("");
        setNotice(null);
        attachTask(data.task);
        return;
      }
      switch (data.code) {
        case "plan_changed": {
          const changes = Object.fromEntries(
            Object.entries(data).filter(([k, v]) => v !== undefined && !NON_PLAN_KEYS.has(k))
          ) as Partial<PitrPlanResponse>;
          // The server may have dropped items that no longer apply: follow its selection
          const newItems = changes.selection?.items;
          if (newItems) setSelected(newItems);
          const newKey = newItems
            ? JSON.stringify(selectionRequest(newItems, restart, restartAtMs, freshForce))
            : null;
          setPlanState((prev) => (prev ? { key: newKey ?? prev.key, data: { ...prev.data, ...changes } } : prev));
          setTyped("");
          setNotice("The server or the plan changed since it was made. Review the updated plan and confirm again.");
          break;
        }
        case "busy":
          if (data.task) {
            track(data.task);
            toast.info("A PITR setup is already running for this job; showing it");
            attachTask(data.task);
          } else {
            setApplyError(data.error || "A PITR setup is already running for this job.");
          }
          break;
        case "stale_check":
          setStaleCheck(true);
          setApplyError(data.error || "The prerequisite check is older than 15 minutes. Re-run the checks, then apply.");
          break;
        case "confirmation_mismatch":
          setTyped("");
          setApplyError("The confirmation text didn't match. Type it again exactly as shown.");
          break;
        case "admin_required":
          setApplyError(data.error || "A selected change needs admin access: pick how backupx gets it.");
          break;
        case "step_up_required":
          setApplyError("Your password is needed to apply these changes.");
          break;
        default:
          setApplyError(data.error || `Request failed (${response.status})`);
      }
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : "Could not start the setup");
    } finally {
      setSubmitting(false);
    }
  };

  const recheckFromApply = async () => {
    await recheck();
    setApplyError(null);
    setStaleCheck(false);
  };

  const cancelScheduled = async () => {
    const child = shownRun?.restart_task;
    if (!child) return;
    setCancelling(true);
    try {
      await cancel(child.id);
    } finally {
      setCancelling(false);
      refreshEnable();
    }
  };

  const startOver = () => {
    setAttached(false);
    setNotice(null);
    setApplyError(null);
    setStep("check");
    recheck();
  };

  // ---------- steps ----------
  const runStep = shownRun ? stepForRun(shownRun) : null;
  const reached = (s: PitrSetupStep) =>
    !!runStep && PITR_SETUP_STEPS.indexOf(runStep) >= PITR_SETUP_STEPS.indexOf(s);
  const enabled: Record<PitrSetupStep, boolean> = {
    check: true,
    plan: !!plan,
    apply: !!plan || !!shownRun,
    restart: reached("restart"),
    stream: reached("stream"),
    anchor: reached("anchor"),
    done: shownRun?.phase === "done",
  };
  const defaultStep: PitrSetupStep = runStep ?? "check";
  const step: PitrSetupStep =
    stepParam && (PITR_SETUP_STEPS as readonly string[]).includes(stepParam) && enabled[stepParam] ? stepParam : defaultStep;

  const jobName = plan?.job.name ?? null;
  const runPanel = shownRun && (
    <EnableRunPanel
      key={shownRun.task.id}
      run={shownRun}
      onCancelScheduled={cancelScheduled}
      cancelling={cancelling}
      onShowDone={() => setStep("done")}
      onStartOver={startOver}
    />
  );

  return (
    <div className="space-y-6">
      <div className="flex min-w-0 items-center gap-3">
        <Button variant="ghost" size="icon" asChild>
          <Link to={`/jobs/${encodeURIComponent(jobId)}?tab=recovery`} aria-label="Back to the job">
            <ArrowLeft />
          </Link>
        </Button>
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <History className="h-6 w-6 shrink-0" />
            <span className="truncate">Point-in-time recovery setup</span>
          </h1>
          <p className="truncate text-sm text-muted-foreground">
            {jobName ? `${jobName} · ` : ""}Check the database server, pick the changes, apply them with a safety backup
            first.
          </p>
        </div>
      </div>

      {run && !attached && (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertTitle>Last setup run: {run.phase.replace("_", " ")}</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-2">
            <span>{formatLocal(run.task.finished_at ?? run.task.created_at)}</span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setAttached(true);
                setStep(null);
              }}
            >
              Show it
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <Tabs value={step} onValueChange={(v) => setStep(v as PitrSetupStep)}>
        <div className="overflow-x-auto">
          <TabsList>
            {PITR_SETUP_STEPS.map((s) => (
              <TabsTrigger key={s} value={s} disabled={!enabled[s]}>
                {STEP_LABEL[s]}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="check" className="mt-4 space-y-4">
          {checking && !plan ? (
            <Card aria-busy="true">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  Checking the database server
                </CardTitle>
                <CardDescription>
                  backupx connects over SSH and reads the server's configuration. This can take up to 2 minutes.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-32 w-full" />
              </CardContent>
            </Card>
          ) : checkError && !plan ? (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>{CHECK_ERROR_TITLE[checkError.code ?? ""] ?? "Could not check the server"}</AlertTitle>
              <AlertDescription className="space-y-2">
                <p>{checkError.message}</p>
                <Button size="sm" variant="outline" onClick={recheck} disabled={checking}>
                  {checking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  Try again
                </Button>
              </AlertDescription>
            </Alert>
          ) : plan ? (
            <>
              {checkError && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>{CHECK_ERROR_TITLE[checkError.code ?? ""] ?? "Could not re-run the checks"}</AlertTitle>
                  <AlertDescription>{checkError.message} The results below are from the previous check.</AlertDescription>
                </Alert>
              )}
              <TargetCard target={plan.target} />
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Prerequisites</CardTitle>
                  <CardDescription>
                    {plan.checked_at ? `Checked ${formatRelative(plan.checked_at)}` : "What point-in-time recovery needs"}
                  </CardDescription>
                  <CardAction>
                    <Button size="sm" variant="outline" onClick={recheck} disabled={checking || runActive}>
                      {checking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                      Re-run checks
                    </Button>
                  </CardAction>
                </CardHeader>
                <CardContent>
                  <PitrPrereqTable
                    result={{
                      kind: plan.kind,
                      checks: plan.checks,
                      ok: plan.prereqs_ok,
                      needs_restart: plan.checks.some((c) => c.needs_restart && c.status !== "ok"),
                    }}
                  />
                </CardContent>
              </Card>
              <div className="flex justify-end">
                <Button onClick={() => setStep("plan")}>
                  Next
                  <ArrowRight />
                </Button>
              </div>
            </>
          ) : null}
        </TabsContent>

        <TabsContent value="plan" className="mt-4 space-y-4">
          {plan && (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Changes to make</CardTitle>
                  <CardDescription>
                    Pick what backupx should change. Items marked manual have to be done by hand; then re-run the checks.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <PlanItems items={plan.items} selected={selected ?? []} onToggle={toggleItem} disabled={runActive || submitting} />
                </CardContent>
              </Card>
              <PlanStepsSummary
                steps={plan.steps}
                downtime={plan.downtime}
                loading={planLoading}
                error={planError?.message ?? null}
              />
              <div className="flex justify-end">
                <Button onClick={() => setStep("apply")} disabled={!enabled.apply}>
                  Next
                  <ArrowRight />
                </Button>
              </div>
            </>
          )}
        </TabsContent>

        <TabsContent value="apply" className="mt-4">
          {shownRun ? (
            runPanel
          ) : plan ? (
            <ApplyForm
              plan={plan}
              planLoading={planLoading}
              planError={planError?.message ?? null}
              selectedCount={selectedItems.length}
              adminMode={adminMode}
              onAdminMode={onAdminMode}
              username={adminUser}
              onUsername={setAdminUser}
              password={adminPassword}
              onPassword={setAdminPassword}
              adminStillNeeded={adminStillNeeded}
              restart={restart}
              onRestart={setRestart}
              restartAtMs={restartAtMs}
              onRestartAt={setRestartAtMs}
              freshForce={freshForce}
              onFreshForce={setFreshForce}
              typed={typed}
              onTyped={setTyped}
              notice={notice}
              error={applyError}
              staleCheck={staleCheck}
              onRecheck={recheckFromApply}
              rechecking={checking}
              submitting={submitting}
              canSubmit={canSubmit}
              onSubmit={submit}
            />
          ) : (
            <Skeleton className="h-64 w-full" />
          )}
        </TabsContent>

        {(["restart", "stream", "anchor"] as const).map((s) => (
          <TabsContent key={s} value={s} className="mt-4">
            {runPanel}
          </TabsContent>
        ))}

        <TabsContent value="done" className="mt-4">
          {shownRun?.phase === "done" && (
            <DonePanel jobId={jobId} jobName={jobName} pitr={enable?.pitr ?? null} result={shownRun.result} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
