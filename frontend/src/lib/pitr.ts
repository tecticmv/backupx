import type { PitrState } from "@/types/job";

export const PITR_STATE_HELP: Record<PitrState, string> = {
  ok: "Restorable to any second in the window shown.",
  lagging: "The newest restorable time is falling behind (streamer or shipping problem).",
  broken: "Binlogs are missing since the last full backup; times after the break become restorable after the next full backup.",
  unanchored: "Binlogs are shipped, but no full backup with a binlog position exists yet (runs with the next backup).",
  pending: "Waiting for the first shipment.",
  disabled: "Point-in-time recovery is off.",
};

export function formatWindow(start: string | null, end: string | null) {
  if (!start || !end) return "nothing restorable yet";
  const fmt = (v: string) => new Date(v).toLocaleString();
  return `${fmt(start)} → ${fmt(end)}`;
}
