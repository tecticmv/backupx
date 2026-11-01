// API helpers and display helpers for the restore wizard (P2).
import { apiJson } from "@/lib/api";
import type {
  FileRestoreRequest,
  FilePlanResponse,
  FindMatch,
  PlanResponse,
  ReplacePreview,
  Restore,
  RestorePlanRequest,
  RestorePoints,
  RestoreStatus,
  VerifyConfig,
} from "@/types/restore";
import type { SnapshotFile } from "@/types/job";
import type { TaskStartResponse } from "@/types/task";

const enc = encodeURIComponent;

function storageQuery(storageId: string | null | undefined, prefix = "&"): string {
  return storageId ? `${prefix}storage=${enc(storageId)}` : "";
}

export function getRestorePoints(jobId: string, storageId?: string | null) {
  return apiJson<RestorePoints>(`/api/jobs/${enc(jobId)}/restore-points${storageQuery(storageId, "?")}`);
}

export function planRestore(body: RestorePlanRequest, signal?: AbortSignal) {
  return apiJson<PlanResponse>("/api/restores/plan", { method: "POST", json: body, signal });
}

/** Raw POST (the caller handles 409 plan_changed / 400 insufficient_disk from the body). */
export function startRestore(body: RestorePlanRequest) {
  return fetch("/api/restores", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listRestores(params: { job_id?: string; active?: boolean; limit?: number } = {}) {
  const q = new URLSearchParams();
  if (params.job_id) q.set("job_id", params.job_id);
  if (params.active) q.set("active", "1");
  if (params.limit) q.set("limit", String(params.limit));
  return apiJson<{ restores: Restore[] }>(`/api/restores?${q.toString()}`);
}

export function getRestore(id: string) {
  return apiJson<Restore>(`/api/restores/${enc(id)}`);
}

export function extendRestore(id: string, hours = 24) {
  return apiJson<Restore>(`/api/restores/${enc(id)}/extend`, { method: "POST", json: { hours } });
}

export function removeRestore(id: string) {
  return apiJson<TaskStartResponse>(`/api/restores/${enc(id)}`, { method: "DELETE" });
}

export function reverifyRestore(id: string) {
  return apiJson<TaskStartResponse>(`/api/restores/${enc(id)}/verify`, { method: "POST" });
}

export function dumpRestore(id: string) {
  return apiJson<TaskStartResponse>(`/api/restores/${enc(id)}/dump`, { method: "POST" });
}

export type ProductionAction = "replace" | "undo";

function actionPath(id: string, action: ProductionAction) {
  return `/api/restores/${enc(id)}/${action === "replace" ? "replace-production" : "undo"}`;
}

export function previewProductionAction(id: string, action: ProductionAction) {
  return apiJson<ReplacePreview>(actionPath(id, action), { method: "POST", json: { preview: true } });
}

/** Raw POST so it can be wrapped in withStepUp(). */
export function executeProductionAction(id: string, action: ProductionAction, planHash: string, confirm: string) {
  return fetch(actionPath(id, action), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_hash: planHash, confirm }),
  });
}

export function getVerifyConfig(jobId: string) {
  return apiJson<VerifyConfig>(`/api/jobs/${enc(jobId)}/restore-verify`);
}

export function putVerifyConfig(jobId: string, config: VerifyConfig) {
  return apiJson<VerifyConfig>(`/api/jobs/${enc(jobId)}/restore-verify`, { method: "PUT", json: config });
}

export function listSnapshotFiles(jobId: string, snapshotId: string, path: string, storageId?: string | null) {
  return apiJson<{ files: SnapshotFile[] }>(
    `/api/jobs/${enc(jobId)}/snapshots/${enc(snapshotId)}/files?path=${enc(path)}${storageQuery(storageId)}`
  );
}

export function findInSnapshot(
  jobId: string,
  snapshotId: string,
  q: string,
  storageId?: string | null,
  signal?: AbortSignal
) {
  return apiJson<{ matches: FindMatch[]; truncated: boolean }>(
    `/api/jobs/${enc(jobId)}/snapshots/${enc(snapshotId)}/find?q=${enc(q)}${storageQuery(storageId)}`,
    { signal }
  );
}

export function planFileRestore(body: FileRestoreRequest) {
  return apiJson<FilePlanResponse>("/api/restores/files/plan", { method: "POST", json: body });
}

/** Raw POST so it can be wrapped in withStepUp(). */
export function startFileRestore(body: FileRestoreRequest & { plan_hash: string; confirm?: string }) {
  return fetch("/api/restores/files", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function startSnapshotZip(jobId: string, snapshotId: string, path: string, storageId?: string | null) {
  return apiJson<TaskStartResponse>(`/api/jobs/${enc(jobId)}/snapshots/${enc(snapshotId)}/zip`, {
    method: "POST",
    json: { path, storage: storageId || undefined },
  });
}

/** Existing PITR restore-script endpoint: downloads a shell script that restores into a local container. */
export async function downloadPitrScript(jobId: string, timeIso: string, storageId: string | null) {
  const res = await fetch(`/api/jobs/${enc(jobId)}/pitr/restore-script`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ time: timeIso, storage_id: storageId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "Can't build a restore script for that time");
  }
  const blob = await res.blob();
  const name = res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `restore-${jobId}.sh`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------- display helpers ----------

export const ACTIVE_RESTORE_STATUSES: RestoreStatus[] = ["running", "replacing", "undoing"];

export function isRestoreActive(status: RestoreStatus) {
  return ACTIVE_RESTORE_STATUSES.includes(status);
}

export const RESTORE_STATUS_META: Record<
  RestoreStatus,
  { label: string; variant: "default" | "secondary" | "destructive" | "outline"; className?: string }
> = {
  running: { label: "Running", variant: "default" },
  ready: { label: "Ready", variant: "outline", className: "border-emerald-500/50 text-emerald-600 dark:text-emerald-400" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "outline", className: "text-muted-foreground" },
  removed: { label: "Removed", variant: "outline", className: "text-muted-foreground" },
  expired: { label: "Expired", variant: "outline", className: "text-muted-foreground" },
  replacing: { label: "Replacing production", variant: "default" },
  replaced: { label: "Production replaced", variant: "secondary" },
  undoing: { label: "Undoing", variant: "default" },
  undone: { label: "Undone", variant: "secondary" },
  done: { label: "Done", variant: "secondary" },
};

/** "2026-10-31 21:00:05 UTC" */
export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

/** Local time with seconds, e.g. "31/10/2026, 21:00:05". */
export function formatLocal(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Short point description of a restore: "snapshot ab12cd34" / "2026-10-31 21:00:05 UTC". */
export function describePoint(r: Pick<Restore, "point_type" | "snapshot_id" | "target_time">): string {
  if (r.point_type === "time" && r.target_time) return formatUtc(r.target_time);
  if (r.snapshot_id) return `snapshot ${r.snapshot_id.slice(0, 8)}`;
  if (r.target_time) return formatUtc(r.target_time);
  return "latest";
}

/** True when `iso` falls inside a restorable window (inclusive). */
export function inWindows(ms: number, windows: { from: string | null; to: string | null }[]): boolean {
  return windows.some((w) => {
    if (!w.from || !w.to) return false;
    return ms >= new Date(w.from).getTime() && ms <= new Date(w.to).getTime();
  });
}

export function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "job";
}

/** "20261101-135501" in local time. */
export function compactTimestamp(date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}-${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`;
}
