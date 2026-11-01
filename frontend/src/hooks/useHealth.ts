import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { apiJson, ApiError } from "@/lib/api";
import { healthUrl, recheckTarget } from "@/lib/health";
import { newIdempotencyKey } from "@/lib/format";
import { useTasks } from "@/hooks/useTasks";
import type { HealthQuery, HealthResponse, Issue } from "@/types/health";
import type { TaskStartResponse } from "@/types/task";

export const HEALTH_POLL_MS = 30000;

/**
 * Generic "fetch JSON now and every pollMs" hook. Keeps the last good data on
 * errors (server restarting, logged out) and exposes refresh() for callers
 * that just changed something.
 */
export function usePolledJson<T>(url: string | null, pollMs = HEALTH_POLL_MS) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Ignore answers for a URL we no longer show (query changed mid-request)
  const latestUrl = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    if (!url) return;
    try {
      const result = await apiJson<T>(url);
      if (latestUrl.current === url) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (latestUrl.current === url) setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      if (latestUrl.current === url) setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    latestUrl.current = url;
    if (!url) return;
    refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, pollMs);
    return () => window.clearInterval(timer);
  }, [url, pollMs, refresh]);

  return { data, error, loading, refresh };
}

/** GET /api/health (polled every 30 s). */
export function useHealth(query: HealthQuery = {}, enabled = true) {
  const url = enabled ? healthUrl(query) : null;
  const { data, error, loading, refresh } = usePolledJson<HealthResponse>(url);
  return {
    summary: data?.summary ?? null,
    issues: data?.issues ?? [],
    error,
    loading,
    refresh,
  };
}

/**
 * Actions on issues that start tasks or change issue state. `onChanged` runs
 * right after a state change and again when a started task finishes, so the
 * caller can re-fetch /api/health (and anything else that depends on it).
 */
export function useHealthActions(onChanged: () => void) {
  const { track } = useTasks();

  const recheck = useCallback(
    async (resource: { resource_type?: "job" | "server" | "storage"; resource_id?: string } = {}) => {
      try {
        const res = await apiJson<TaskStartResponse>("/api/health/recheck", { method: "POST", json: resource });
        track(res.task, { onDone: () => onChanged() });
        toast.success(res.message || "Re-check started");
        onChanged();
        return res.task;
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not start the re-check");
        return null;
      }
    },
    [track, onChanged]
  );

  const recheckIssue = useCallback((issue: Issue) => recheck(recheckTarget(issue)), [recheck]);

  /** One-click fix for a safe-tier issue. */
  const fixSafe = useCallback(
    async (issue: Issue) => {
      try {
        const res = await apiJson<TaskStartResponse>("/api/fixes", {
          method: "POST",
          json: { issue_id: issue.id, idempotency_key: newIdempotencyKey() },
        });
        track(res.task, { onDone: () => onChanged() });
        toast.success(res.message || `${issue.fix?.label ?? "Fix"} started`);
        onChanged();
        return res.task;
      } catch (err) {
        if (err instanceof ApiError && err.code === "issue_resolved") {
          toast.info("This issue is already resolved");
          onChanged();
        } else {
          toast.error(err instanceof Error ? err.message : "Could not start the fix");
        }
        return null;
      }
    },
    [track, onChanged]
  );

  /** hours = 0 unsnoozes. */
  const snooze = useCallback(
    async (issue: Issue, hours: number) => {
      try {
        await apiJson<{ issue: Issue }>(`/api/issues/${encodeURIComponent(issue.id)}/snooze`, {
          method: "POST",
          json: { hours },
        });
        toast.success(hours === 0 ? "Issue unsnoozed" : `Snoozed for ${hours >= 24 ? `${hours / 24} d` : `${hours} h`}`);
        onChanged();
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not snooze the issue");
      }
    },
    [onChanged]
  );

  return { recheck, recheckIssue, fixSafe, snooze };
}

/** Start a backup run (optionally only some storages) and track it. */
export function useRunJob(onChanged: () => void) {
  const { track } = useTasks();
  return useCallback(
    async (jobId: string, storageIds?: string[]) => {
      try {
        const res = await apiJson<TaskStartResponse>(`/api/jobs/${encodeURIComponent(jobId)}/run`, {
          method: "POST",
          json: storageIds?.length ? { storage_ids: storageIds } : {},
        });
        track(res.task, { onDone: () => onChanged() });
        toast.success(
          res.deduplicated
            ? "A backup of this job is already queued"
            : storageIds?.length
              ? "Retrying failed storages"
              : "Backup started"
        );
        onChanged();
        return res.task;
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Failed to start backup");
        return null;
      }
    },
    [track, onChanged]
  );
}
