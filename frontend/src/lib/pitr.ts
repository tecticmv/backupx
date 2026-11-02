import type { PitrState } from "@/types/job";
import { formatRelative } from "@/lib/format";

export const PITR_STATE_HELP: Record<PitrState, string> = {
  ok: "Restorable to any second in the window shown.",
  lagging: "The newest restorable time is falling behind (streamer or shipping problem).",
  broken: "Binlogs are missing since the last full backup; times after the break become restorable after the next full backup.",
  unanchored: "Binlogs are shipped, but no full backup with a binlog position exists yet (runs with the next backup).",
  pending: "Waiting for the first shipment.",
  disabled: "Point-in-time recovery is off.",
};

/** Fields that say how current point-in-time recovery is. */
interface PitrFreshness {
  lag_minutes: number | null;
  idle_since?: string | null;
  last_ship_at?: string | null;
}

const shortTime = (iso: string) =>
  new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

/**
 * "Lag: 3 min", or for an idle PostgreSQL server (lag 0, old window end)
 * "idle — nothing written since <time>"; null when unknown.
 */
export function pitrLagText(p: PitrFreshness, compact = false): string | null {
  if (p.idle_since) return `idle — nothing written since ${shortTime(p.idle_since)}`;
  if (p.lag_minutes === null || p.lag_minutes === undefined) return null;
  return compact ? `lag ${Math.round(p.lag_minutes)}m` : `Lag: ${Math.round(p.lag_minutes)} min`;
}

/** "Last shipped 4m ago" (closed binlogs, open-file copies or WAL), null when never. */
export function pitrLastShipText(p: PitrFreshness): string | null {
  return p.last_ship_at ? `Last shipped ${formatRelative(p.last_ship_at)}` : null;
}

export function formatWindow(start: string | null, end: string | null) {
  if (!start || !end) return "nothing restorable yet";
  const fmt = (v: string) => new Date(v).toLocaleString();
  return `${fmt(start)} → ${fmt(end)}`;
}
