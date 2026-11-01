import { useState } from "react";
import { CalendarIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { formatUtc } from "@/lib/restore";
import { localTimeValue } from "@/lib/pitrSetup";

interface RestartWindowPickerProps {
  /** Restart time in epoch ms (local date + time picked here). */
  value: number;
  onChange: (ms: number) => void;
  disabled?: boolean;
}

function startOfToday() {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Date (Calendar in a Popover) + time Input in local time; shows the UTC value sent to the server. */
export default function RestartWindowPicker({ value, onChange, disabled = false }: RestartWindowPickerProps) {
  const [open, setOpen] = useState(false);
  const [today] = useState(startOfToday);
  const current = new Date(value);

  const setDate = (day: Date | undefined) => {
    if (!day) return;
    onChange(new Date(day.getFullYear(), day.getMonth(), day.getDate(), current.getHours(), current.getMinutes()).getTime());
    setOpen(false);
  };

  const setTime = (text: string) => {
    const [h, m] = text.split(":");
    if (h === undefined || m === undefined) return;
    const d = new Date(value);
    d.setHours(Number(h), Number(m), 0, 0);
    onChange(d.getTime());
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-2">
          <Label>Date</Label>
          <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger asChild>
              <Button variant="outline" className="w-48 justify-start font-normal" disabled={disabled}>
                <CalendarIcon />
                {current.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" })}
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-0" align="start">
              <Calendar
                mode="single"
                selected={current}
                onSelect={setDate}
                defaultMonth={current}
                disabled={{ before: today }}
              />
            </PopoverContent>
          </Popover>
        </div>
        <div className="grid gap-2">
          <Label htmlFor="pitr-restart-time">Time (your local time)</Label>
          <Input
            id="pitr-restart-time"
            type="time"
            value={localTimeValue(value)}
            onChange={(e) => setTime(e.target.value)}
            className="w-32"
            disabled={disabled}
          />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Restart at{" "}
        <span className="text-foreground">
          {current.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
        </span>{" "}
        local · <span className="font-mono">{formatUtc(current.toISOString())}</span>
      </p>
    </div>
  );
}
