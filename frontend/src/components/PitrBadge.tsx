import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { PitrState, PitrSummary } from "@/types/job";
import { History } from "lucide-react";
import { PITR_STATE_HELP, formatWindow } from "@/lib/pitr";

const STATE_STYLE: Record<PitrState, { label: string; className: string }> = {
  ok: { label: "PITR ok", className: "border-emerald-500/50 text-emerald-600" },
  lagging: { label: "PITR lagging", className: "border-amber-500/50 text-amber-500" },
  broken: { label: "PITR broken", className: "border-destructive/60 text-destructive" },
  unanchored: { label: "PITR unanchored", className: "border-amber-500/50 text-amber-500" },
  pending: { label: "PITR starting", className: "text-muted-foreground" },
  disabled: { label: "PITR off", className: "text-muted-foreground" },
};

export default function PitrBadge({ pitr }: { pitr?: PitrSummary | null }) {
  if (!pitr || !pitr.enabled) return null;
  const style = STATE_STYLE[pitr.state] ?? STATE_STYLE.pending;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className={`gap-1 ${style.className}`}>
          <History className="h-3 w-3" />
          {style.label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm">
        <div className="space-y-1 text-xs">
          <p>{PITR_STATE_HELP[pitr.state]}</p>
          <p>Restorable: {formatWindow(pitr.window_start, pitr.window_end)}</p>
          {pitr.lag_minutes !== null && <p>Lag: {Math.round(pitr.lag_minutes)} min</p>}
          {pitr.last_error && <p className="text-destructive">Last error: {pitr.last_error}</p>}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
