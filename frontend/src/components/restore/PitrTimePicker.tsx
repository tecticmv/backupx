import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Calendar } from "@/components/ui/calendar";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { formatLocal, formatUtc, inWindows } from "@/lib/restore";
import type { RestorePoints } from "@/types/restore";

interface PitrTimePickerProps {
  pitr: RestorePoints["pitr"];
  /** Chosen time in epoch ms. */
  value: number | null;
  onChange: (ms: number) => void;
}

const pad = (n: number) => String(n).padStart(2, "0");

function timeInputValue(ms: number) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function startOfDay(ms: number) {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Step 2 "Exact time": date (Calendar) + time (seconds) + a slider across the PITR range. */
export default function PitrTimePicker({ pitr, value, onChange }: PitrTimePickerProps) {
  const min = pitr.min ? new Date(pitr.min).getTime() : null;
  const max = pitr.max ? new Date(pitr.max).getTime() : null;
  if (min === null || max === null) return null;

  const current = value ?? max;
  const clamp = (ms: number) => Math.min(max, Math.max(min, ms));
  const inGap = pitr.gaps.find((g) => current > new Date(g.from).getTime() && current < new Date(g.to).getTime());
  const restorable = inWindows(current, pitr.windows.filter((w) => w.anchored));

  const setDate = (day: Date | undefined) => {
    if (!day) return;
    const t = new Date(current);
    onChange(clamp(new Date(day.getFullYear(), day.getMonth(), day.getDate(), t.getHours(), t.getMinutes(), t.getSeconds()).getTime()));
  };

  const setTime = (text: string) => {
    const [h, m, s = "0"] = text.split(":");
    if (h === undefined || m === undefined) return;
    const d = new Date(current);
    d.setHours(Number(h), Number(m), Number(s), 0);
    onChange(clamp(d.getTime()));
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-4 md:flex-row">
        <Calendar
          mode="single"
          selected={new Date(current)}
          onSelect={setDate}
          defaultMonth={new Date(current)}
          disabled={{ before: startOfDay(min), after: startOfDay(max) }}
          className="rounded-md border"
        />
        <div className="min-w-0 flex-1 space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="pitr-time">Time (your local time)</Label>
            <Input
              id="pitr-time"
              type="time"
              step={1}
              value={timeInputValue(current)}
              onChange={(e) => setTime(e.target.value)}
              className="w-40"
            />
          </div>
          <div className="grid gap-2">
            <Label>Anywhere in the range</Label>
            <Slider
              min={min}
              max={max}
              step={1000}
              value={[current]}
              onValueChange={([ms]) => onChange(ms)}
              aria-label="Restore time"
            />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{formatLocal(pitr.min)}</span>
              <span>{formatLocal(pitr.max)}</span>
            </div>
          </div>
          <div className="rounded-md border bg-muted/40 p-3 text-sm">
            <p className="font-medium">{formatLocal(new Date(current).toISOString())}</p>
            <p className="font-mono text-xs text-muted-foreground">{formatUtc(new Date(current).toISOString())}</p>
          </div>
        </div>
      </div>

      <div className="space-y-1.5 text-xs">
        {pitr.windows.map((w, i) => (
          <div key={`w${i}`} className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className={w.anchored ? "border-emerald-500/50 text-emerald-600 dark:text-emerald-400" : "text-muted-foreground"}
            >
              {w.anchored ? "restorable" : "not anchored"}
            </Badge>
            <span className="font-mono">
              {formatUtc(w.from)} → {formatUtc(w.to)}
            </span>
            {w.reason && <span className="text-muted-foreground">({w.reason})</span>}
          </div>
        ))}
        {pitr.gaps.map((g, i) => (
          <div key={`g${i}`} className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="border-destructive/50 text-destructive">
              gap
            </Badge>
            <span className="font-mono">
              {formatUtc(g.from)} → {formatUtc(g.to)}
            </span>
            {g.reason && <span className="text-muted-foreground">({g.reason})</span>}
          </div>
        ))}
      </div>

      {(inGap || !restorable) && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>This time can't be restored</AlertTitle>
          <AlertDescription>
            {inGap
              ? `It falls into a gap (${inGap.reason || "binlog chain broken"}). Pick a time inside a restorable range.`
              : "It is outside every restorable range. Pick a time inside a green range."}
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
