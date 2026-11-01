import { AlertOctagon, AlertTriangle, Info, type LucideIcon } from "lucide-react";
import type { HealthQuery, Issue, Severity } from "@/types/health";

export const SEVERITY_ORDER: Severity[] = ["critical", "warning", "info"];

export const SEVERITY_META: Record<
  Severity,
  { label: string; plural: string; icon: LucideIcon; text: string; badge: string; border: string }
> = {
  critical: {
    label: "Critical",
    plural: "Critical",
    icon: AlertOctagon,
    text: "text-destructive dark:text-red-400",
    badge: "border-destructive/50 bg-destructive/10 text-destructive dark:text-red-400",
    border: "border-l-destructive",
  },
  warning: {
    label: "Warning",
    plural: "Warnings",
    icon: AlertTriangle,
    text: "text-amber-500",
    badge: "border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-400",
    border: "border-l-amber-500",
  },
  info: {
    label: "Info",
    plural: "Info",
    icon: Info,
    text: "text-sky-500",
    badge: "border-sky-500/50 bg-sky-500/10 text-sky-600 dark:text-sky-400",
    border: "border-l-sky-500",
  },
};

export function healthUrl(query: HealthQuery = {}): string {
  const params = new URLSearchParams();
  if (query.severity) params.set("severity", query.severity);
  if (query.resource_type) params.set("resource_type", query.resource_type);
  if (query.resource_id) params.set("resource_id", query.resource_id);
  if (query.include_snoozed) params.set("include_snoozed", "1");
  const qs = params.toString();
  return qs ? `/api/health?${qs}` : "/api/health";
}

export function isSnoozed(issue: Issue, now: number = Date.now()): boolean {
  return !!issue.snoozed_until && new Date(issue.snoozed_until).getTime() > now;
}

/** Where the "Re-check" of a manual issue points: the narrowest resource the API accepts. */
export function recheckTarget(issue: Issue): { resource_type?: "job" | "server" | "storage"; resource_id?: string } {
  const r = issue.resource;
  if (r.type === "job" || r.type === "server" || r.type === "storage") return { resource_type: r.type, resource_id: r.id };
  if (r.job_id) return { resource_type: "job", resource_id: r.job_id };
  if (r.server_id) return { resource_type: "server", resource_id: r.server_id };
  if (r.storage_id) return { resource_type: "storage", resource_id: r.storage_id };
  return {};
}

/** "Job A · Storage B" style label for an issue's resource. */
export function resourceLabel(issue: Pick<Issue, "resource">): string {
  const r = issue.resource;
  if (r.job_name && r.storage_name) return `${r.job_name} · ${r.storage_name}`;
  return r.name || r.job_name || r.storage_name || r.id;
}
