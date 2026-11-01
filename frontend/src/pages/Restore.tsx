import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowRight, Plus, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import DbTargetPicker from "@/components/restore/DbTargetPicker";
import FileRestorePanel from "@/components/restore/FileRestorePanel";
import JobPicker from "@/components/restore/JobPicker";
import PlanSummary from "@/components/restore/PlanSummary";
import PointPicker from "@/components/restore/PointPicker";
import RestoreRun from "@/components/restore/RestoreRun";
import RestoreVerify from "@/components/restore/RestoreVerify";
import RunStart from "@/components/restore/RunStart";
import { useTasks } from "@/hooks/useTasks";
import {
  currentTask,
  downloadPitrScript,
  getRestore,
  getRestorePoints,
  inWindows,
  isRestoreActive,
  planRestore,
  startRestore,
} from "@/lib/restore";
import { isActiveTask } from "@/types/task";
import {
  WIZARD_STEPS,
  type PlanResponse,
  type PointType,
  type Restore as RestoreRecord,
  type RestorePlanRequest,
  type RestorePoints,
  type RestoreStartResponse,
  type RestoreTarget,
  type WizardStep,
} from "@/types/restore";

const RESTORE_POLL_MS = 2500;
const PLAN_DEBOUNCE_MS = 400;

const STEP_LABEL: Record<WizardStep, string> = {
  what: "1. What",
  when: "2. When",
  where: "3. Where",
  run: "4. Run",
  verify: "5. Verify",
};

type Params = Partial<Record<"job" | "snapshot" | "storage" | "restore" | "step", string | null>>;

function restoreBusy(r: RestoreRecord) {
  return isRestoreActive(r.status) || [r.task, r.replace_task, r.undo_task].some((t) => t && isActiveTask(t));
}

export default function Restore() {
  const { track } = useTasks();
  const [params, setParams] = useSearchParams();
  const jobParam = params.get("job");
  const storageParam = params.get("storage");
  const snapshotParam = params.get("snapshot");
  const restoreId = params.get("restore");
  const stepParam = params.get("step") as WizardStep | null;

  const update = useCallback(
    (changes: Params) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(changes)) {
            if (v) next.set(k, v);
            else next.delete(k);
          }
          return next;
        },
        { replace: true }
      ),
    [setParams]
  );

  const [pointType, setPointType] = useState<PointType>(snapshotParam ? "snapshot" : "latest");
  const [timeMs, setTimeMs] = useState<number | null>(null);
  const [target, setTarget] = useState<RestoreTarget>("scratch");

  // ---------- the loaded restore (polled while it runs) ----------
  const [restoreState, setRestoreState] = useState<{ id: string; data: RestoreRecord | null; error: string | null } | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const lastSeen = useRef<{ id: string; status: string } | null>(null);
  const refreshRestore = useCallback(() => setRefreshTick((n) => n + 1), []);

  useEffect(() => {
    if (!restoreId) return;
    let stopped = false;
    let timer: number | undefined;
    const loop = async () => {
      try {
        const r = await getRestore(restoreId);
        if (stopped) return;
        const prev = lastSeen.current;
        lastSeen.current = { id: restoreId, status: r.status };
        if (prev?.id !== restoreId) {
          // Opened by id: the restore defines job / point / storage
          setPointType(r.point_type);
          setTimeMs(r.target_time ? new Date(r.target_time).getTime() : null);
          setTarget(r.target_type === "download" ? "download" : "scratch");
          update({ job: r.job_id, snapshot: r.snapshot_id, storage: r.storage_id });
        } else if (isRestoreActive(prev.status as RestoreRecord["status"]) && !isRestoreActive(r.status)) {
          // Finished: let the step fall back to its default (Verify on success, Run on failure)
          update({ step: null });
        }
        setRestoreState({ id: restoreId, data: r, error: null });
        if (restoreBusy(r)) timer = window.setTimeout(loop, RESTORE_POLL_MS);
      } catch (err) {
        if (stopped) return;
        setRestoreState((s) => ({
          id: restoreId,
          data: s?.id === restoreId ? s.data : null,
          error: err instanceof Error ? err.message : "Could not load the restore",
        }));
      }
    };
    loop();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [restoreId, refreshTick, update]);

  const restore = restoreState?.id === restoreId ? restoreState.data : null;
  const restoreError = restoreState?.id === restoreId ? restoreState.error : null;
  const jobId = jobParam ?? restore?.job_id ?? null;

  // ---------- restore points of the chosen job / storage ----------
  const pointsKey = jobId ? `${jobId}|${storageParam ?? ""}` : null;
  const [pointsState, setPointsState] = useState<{ key: string; data: RestorePoints | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!pointsKey || !jobId) return;
    let cancelled = false;
    getRestorePoints(jobId, storageParam)
      .then((data) => !cancelled && setPointsState({ key: pointsKey, data, error: null }))
      .catch(
        (err) =>
          !cancelled &&
          setPointsState({ key: pointsKey, data: null, error: err instanceof Error ? err.message : "Request failed" })
      );
    return () => {
      cancelled = true;
    };
  }, [pointsKey, jobId, storageParam]);
  const pointsCurrent = pointsState?.key === pointsKey ? pointsState : null;
  const points = pointsCurrent?.data ?? null;
  const pointsLoading = !!pointsKey && !pointsCurrent;

  const isFiles = points ? points.backup_type === "filesystem" || points.engine === "files" : restore?.engine === "files";
  const storageId = storageParam ?? points?.storage_id ?? null;
  const pitrMax = points?.pitr.max ? new Date(points.pitr.max).getTime() : null;
  const effectiveTimeMs = timeMs ?? pitrMax;
  const timeIso = effectiveTimeMs != null ? new Date(effectiveTimeMs).toISOString() : null;

  const pointValid = useMemo(() => {
    if (!points) return false;
    if (pointType === "latest") return !!points.latest;
    if (pointType === "snapshot") return !!snapshotParam && points.snapshots.some((s) => s.id === snapshotParam);
    return effectiveTimeMs != null && inWindows(effectiveTimeMs, points.pitr.windows.filter((w) => w.anchored));
  }, [points, pointType, snapshotParam, effectiveTimeMs]);

  // Snapshot a file restore reads from
  const fileSnapshotId = pointType === "snapshot" ? snapshotParam : (points?.latest?.snapshot_id ?? null);

  // Download of a time point goes through the PITR restore script, not a plan
  const scriptTime =
    pointType === "time" ? timeIso : pointType === "latest" && points?.latest?.point_type === "time" ? points.latest.time : null;
  const useScript = target === "download" && !!scriptTime;

  // ---------- plan (debounced) ----------
  const planRequest: RestorePlanRequest | null = useMemo(() => {
    if (!jobId || !points || isFiles || !pointValid || useScript) return null;
    return {
      job_id: jobId,
      point_type: pointType,
      snapshot_id: pointType === "snapshot" ? (snapshotParam ?? undefined) : undefined,
      target_time: pointType === "time" ? (timeIso ?? undefined) : undefined,
      storage_id: storageId,
      target,
    };
  }, [jobId, points, isFiles, pointValid, useScript, pointType, snapshotParam, timeIso, storageId, target]);
  const planKey = planRequest ? JSON.stringify(planRequest) : null;

  const [planState, setPlanState] = useState<{ key: string; data: PlanResponse | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!planKey) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      planRestore(JSON.parse(planKey) as RestorePlanRequest, controller.signal)
        .then((data) => setPlanState({ key: planKey, data, error: null }))
        .catch((err) => {
          if (controller.signal.aborted) return;
          setPlanState({ key: planKey, data: null, error: err instanceof Error ? err.message : "Could not plan" });
        });
    }, PLAN_DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [planKey]);
  const planCurrent = planKey && planState?.key === planKey ? planState : null;
  const planLoading = !!planKey && !planCurrent;

  // ---------- starting ----------
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const clearRun = () => {
    setStartError(null);
    setNotice(null);
  };

  const start = async () => {
    if (!jobId) return;
    setStarting(true);
    setStartError(null);
    try {
      if (useScript && scriptTime) {
        await downloadPitrScript(jobId, scriptTime, storageId);
        toast.success("Restore script downloaded");
        return;
      }
      const plan = planCurrent?.data;
      if (!planRequest || !plan) return;
      const response = await startRestore({ ...planRequest, plan_hash: plan.plan_hash });
      const data = (await response.json().catch(() => ({}))) as RestoreStartResponse;
      if (response.ok && data.task) {
        track(data.task, { autoDownload: target === "download" });
        toast.success(data.message || (target === "download" ? "Building the dump" : "Scratch restore started"));
        setNotice(null);
        if (data.restore) update({ restore: data.restore.id, step: null });
        return;
      }
      if (data.code === "plan_changed" && data.plan && data.plan_hash && planKey) {
        setPlanState({ key: planKey, data: { plan: data.plan, plan_hash: data.plan_hash, ok: true }, error: null });
        setNotice("The restore points changed since the plan was made (new backup, binlogs shipped...). Review the plan and confirm again.");
      } else if (data.code === "insufficient_disk") {
        setStartError(data.error || "Not enough free disk space on the backupx server for this restore.");
      } else {
        setStartError(data.error || `Request failed (${response.status})`);
      }
    } catch (err) {
      setStartError(err instanceof Error ? err.message : "Could not start the restore");
    } finally {
      setStarting(false);
    }
  };

  // ---------- step handling ----------
  // Changing what/when/where while an old restore is shown starts a new one
  const detach = (extra: Params = {}) => {
    clearRun();
    update({ restore: null, ...extra });
  };

  const enabled: Record<WizardStep, boolean> = {
    what: true,
    when: !!jobId,
    where: !!points && (isFiles ? !!fileSnapshotId : pointValid),
    run: !!restore || (!!points && !isFiles && pointValid),
    verify: !!restore && !["running", "failed", "cancelled"].includes(restore.status),
  };

  const defaultStep: WizardStep = restore
    ? restoreBusy(restore) || restore.status === "failed" || restore.status === "cancelled"
      ? "run"
      : "verify"
    : jobId
      ? "when"
      : "what";
  const step: WizardStep =
    stepParam && (WIZARD_STEPS as readonly string[]).includes(stepParam) && enabled[stepParam] ? stepParam : defaultStep;
  const goto = (s: WizardStep) => update({ step: s });

  const selectJob = (id: string) => {
    setPointType("latest");
    setTimeMs(null);
    setTarget("scratch");
    detach({ job: id, snapshot: null, storage: null, step: null });
  };

  const activeTask = restore ? currentTask(restore) : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <RotateCcw className="h-6 w-6" />
            Restore
          </h1>
          <p className="text-sm text-muted-foreground">
            Restore into a scratch copy first, check it, then download it or replace production with a safety backup and
            Undo.
          </p>
        </div>
        {restore && (
          <Button variant="outline" size="sm" onClick={() => detach({ step: "when" })}>
            <Plus />
            New restore of this job
          </Button>
        )}
      </div>

      {restoreError && !restore && (
        <Alert variant="destructive">
          <AlertTitle>Could not load the restore</AlertTitle>
          <AlertDescription>{restoreError}</AlertDescription>
        </Alert>
      )}

      <Tabs value={step} onValueChange={(v) => goto(v as WizardStep)}>
        <div className="overflow-x-auto">
          <TabsList>
            {WIZARD_STEPS.map((s) => (
              <TabsTrigger key={s} value={s} disabled={!enabled[s]}>
                {STEP_LABEL[s]}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="what" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">What do you want to restore?</CardTitle>
              <CardDescription>Pick the backup job.</CardDescription>
            </CardHeader>
            <CardContent>
              <JobPicker value={jobId} onSelect={selectJob} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="when" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                When? {points && <span className="font-normal text-muted-foreground">· {points.job_name}</span>}
              </CardTitle>
              <CardDescription>Times are shown in your local time with UTC alongside.</CardDescription>
            </CardHeader>
            <CardContent>
              <PointPicker
                points={points}
                loading={pointsLoading}
                error={pointsCurrent?.error ?? null}
                storageId={storageParam}
                onStorage={(id) => detach({ storage: id, snapshot: null })}
                pointType={pointType}
                onPointType={(t) => {
                  setPointType(t);
                  detach();
                }}
                snapshotId={snapshotParam}
                onSnapshot={(id) => {
                  setPointType("snapshot");
                  detach({ snapshot: id });
                }}
                timeMs={effectiveTimeMs}
                onTime={(ms) => {
                  setTimeMs(ms);
                  if (restoreId) detach();
                }}
              />
            </CardContent>
          </Card>
          {!isFiles && points && !useScript && (
            <PlanSummary
              plan={planCurrent?.data?.plan ?? null}
              loading={planLoading}
              error={planCurrent?.error ?? (planCurrent?.data && !planCurrent.data.ok ? planCurrent.data.error ?? null : null)}
            />
          )}
          <div className="flex justify-end">
            <Button onClick={() => goto("where")} disabled={!enabled.where}>
              Next
              <ArrowRight />
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="where" className="mt-4 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Where should it go?</CardTitle>
              <CardDescription>
                {isFiles ? "Pick files and folders, then where they go." : "Production is never touched in this step."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {!points ? (
                <Skeleton className="h-40 w-full" />
              ) : isFiles ? (
                jobId && fileSnapshotId ? (
                  <FileRestorePanel
                    jobId={jobId}
                    jobName={points.job_name}
                    snapshotId={fileSnapshotId}
                    storageId={storageId}
                    onStarted={(r) => {
                      if (r) update({ restore: r.id, step: null });
                    }}
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">Pick a backup in step 2 first.</p>
                )
              ) : (
                <DbTargetPicker
                  value={target}
                  onChange={(t) => {
                    setTarget(t);
                    if (restoreId) detach();
                  }}
                  timePoint={!!scriptTime}
                  canReplace={!!restore?.can_replace}
                  onReplace={() => goto("verify")}
                />
              )}
            </CardContent>
          </Card>
          {!isFiles && (
            <div className="flex justify-end">
              <Button onClick={() => goto("run")} disabled={!enabled.run}>
                Next
                <ArrowRight />
              </Button>
            </div>
          )}
        </TabsContent>

        <TabsContent value="run" className="mt-4">
          {restore ? (
            <RestoreRun key={activeTask?.id ?? restore.id} restore={restore} onShowVerify={() => goto("verify")} />
          ) : restoreId ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <RunStart
              target={target}
              scriptTime={useScript ? scriptTime : null}
              plan={planCurrent?.data ?? null}
              planLoading={planLoading}
              planError={planCurrent?.error ?? null}
              notice={notice}
              error={startError}
              starting={starting}
              onStart={start}
            />
          )}
        </TabsContent>

        <TabsContent value="verify" className="mt-4">
          {restore && <RestoreVerify restore={restore} onChanged={refreshRestore} />}
        </TabsContent>
      </Tabs>
    </div>
  );
}
