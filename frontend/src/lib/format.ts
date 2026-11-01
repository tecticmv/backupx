// Small display helpers shared by the dashboard, health and job detail views.

/** "3m ago", "in 2h", "Just now"; falls back to the date after a week. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "unknown";
  const diff = t - now;
  const future = diff > 0;
  const mins = Math.floor(Math.abs(diff) / 60000);
  if (mins < 1) return future ? "in under a minute" : "just now";
  let text: string;
  if (mins < 60) text = `${mins}m`;
  else if (mins < 60 * 24) text = `${Math.floor(mins / 60)}h ${mins % 60 ? `${mins % 60}m` : ""}`.trim();
  else if (mins < 60 * 24 * 7) text = `${Math.floor(mins / 1440)}d`;
  else return new Date(iso).toLocaleDateString();
  return future ? `in ${text}` : `${text} ago`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(Math.abs(bytes)) / Math.log(k)));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(i === 0 ? 0 : 1))} ${sizes[i]}`;
}

export function formatDuration(startIso: string | null, endIso: string | null): string {
  if (!startIso || !endIso) return "—";
  const secs = Math.max(0, (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000);
  if (secs < 60) return `${Math.round(secs)}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/** Evidence keys are snake_case: "lock_age_minutes" -> "Lock age minutes". */
export function humanizeKey(key: string): string {
  const text = key.replace(/[_-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(formatValue).join(", ");
  return String(value);
}

/**
 * Random UUID for idempotency keys. crypto.randomUUID() only exists in secure
 * contexts (HTTPS/localhost), and backupx is often opened over plain HTTP on
 * the LAN, so fall back to getRandomValues (available everywhere).
 */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function" && window.isSecureContext) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
