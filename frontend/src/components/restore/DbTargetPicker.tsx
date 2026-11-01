import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import type { RestoreTarget } from "@/types/restore";

export type DbChoice = RestoreTarget | "replace";

interface DbTargetPickerProps {
  value: RestoreTarget;
  onChange: (target: RestoreTarget) => void;
  /** The point resolves to a time on a PITR job: Download gives a restore script instead of a dump. */
  timePoint: boolean;
  /** The loaded restore is a ready scratch copy that may replace production. */
  canReplace: boolean;
  onReplace: () => void;
}

/** Step 3 for database jobs: scratch copy (default), download, or replace production (gated). */
export default function DbTargetPicker({ value, onChange, timePoint, canReplace, onReplace }: DbTargetPickerProps) {
  const options: { value: DbChoice; title: string; help: string; disabled?: boolean }[] = [
    {
      value: "scratch",
      title: "Scratch copy on the backupx server",
      help: "Restored into a throwaway database container on the backupx server: no published ports, removed after 24 h (you can extend it). Production is not touched. Check the data, then download it or replace production from there.",
    },
    {
      value: "download",
      title: "Download",
      help: timePoint
        ? "Downloads a script that rebuilds the database as of this second in a local docker container (needs docker and restic where you run it)."
        : "Builds a compressed dump file of this backup and downloads it.",
    },
    {
      value: "replace",
      title: "Replace production",
      help: canReplace
        ? "Your scratch copy of this point is ready. Continue in Verify, where production can be replaced with an automatic safety backup and Undo."
        : "Available after a successful scratch restore of the same point.",
      disabled: !canReplace,
    },
  ];

  return (
    <RadioGroup
      value={value}
      onValueChange={(v) => (v === "replace" ? onReplace() : onChange(v as RestoreTarget))}
      className="gap-3"
    >
      {options.map((o) => (
        <Label
          key={o.value}
          htmlFor={`db-target-${o.value}`}
          className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60"
        >
          <RadioGroupItem value={o.value} id={`db-target-${o.value}`} className="mt-0.5" disabled={o.disabled} />
          <div className="grid gap-1">
            <span className="font-medium">{o.title}</span>
            <span className="text-sm text-muted-foreground">{o.help}</span>
          </div>
        </Label>
      ))}
    </RadioGroup>
  );
}
