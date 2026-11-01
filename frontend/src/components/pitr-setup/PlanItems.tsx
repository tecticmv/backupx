import { ChevronDown, KeyRound, ListChecks, RotateCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Label } from "@/components/ui/label";
import CopyButton from "@/components/health/CopyButton";
import { cn } from "@/lib/utils";
import type { PitrSetupItem } from "@/types/pitrSetup";

interface PlanItemsProps {
  items: PitrSetupItem[];
  selected: string[];
  onToggle: (id: string, checked: boolean) => void;
  disabled?: boolean;
}

function ManualSteps({ manual }: { manual: NonNullable<PitrSetupItem["manual"]> }) {
  return (
    <div className="space-y-2">
      {manual.steps.length > 0 && (
        <ol className="list-decimal space-y-1 pl-5">
          {manual.steps.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      )}
      {manual.commands.map((cmd, i) => (
        <div key={i} className="flex items-start gap-1 rounded-md border bg-background">
          <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre p-2 font-mono text-xs">{cmd}</pre>
          <CopyButton text={cmd} label="Copy command" />
        </div>
      ))}
    </div>
  );
}

function Preview({ item }: { item: PitrSetupItem }) {
  const settings = Object.entries(item.settings ?? {});
  if (!settings.length && !item.sql?.length) return null;
  const text = [...settings.map(([k, v]) => `${k} = ${v}`), ...(item.sql ?? []).map((s) => (s.endsWith(";") ? s : `${s};`))].join(
    "\n"
  );
  return (
    <div className="flex items-start gap-1 rounded-md border bg-muted/40">
      <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre p-2 font-mono text-xs">{text}</pre>
      <CopyButton text={text} label={item.sql?.length ? "Copy SQL" : "Copy settings"} />
    </div>
  );
}

/** Plan step: one Checkbox row per remediation item. Manual-only items are shown but can't be selected. */
export default function PlanItems({ items, selected, onToggle, disabled = false }: PlanItemsProps) {
  if (!items.length) {
    return <p className="text-sm text-muted-foreground">Nothing to change: every prerequisite already passes.</p>;
  }
  return (
    <div className="space-y-3">
      {items.map((item) => {
        const checked = item.auto && selected.includes(item.id);
        const id = `pitr-item-${item.id}`;
        return (
          <div
            key={item.id}
            className={cn(
              "rounded-md border p-3 text-sm",
              checked && "border-primary",
              !item.auto && "bg-muted/30"
            )}
          >
            <div className="flex items-start gap-3">
              <Checkbox
                id={id}
                className="mt-0.5"
                checked={checked}
                disabled={disabled || !item.auto}
                onCheckedChange={(v) => onToggle(item.id, v === true)}
              />
              <div className="min-w-0 flex-1 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Label htmlFor={id} className={cn("font-medium", item.auto && !disabled && "cursor-pointer")}>
                    {item.title}
                  </Label>
                  {item.downtime === "restart" ? (
                    <Badge variant="outline" className="gap-1 border-destructive/50 text-destructive">
                      <RotateCw />
                      restart
                    </Badge>
                  ) : (
                    <Badge variant="secondary">no downtime</Badge>
                  )}
                  {item.needs_admin && (
                    <Badge variant="outline" className="gap-1 border-amber-500/50 text-amber-600 dark:text-amber-400">
                      <KeyRound />
                      needs admin
                    </Badge>
                  )}
                  {!item.auto && (
                    <Badge variant="outline" className="gap-1 text-muted-foreground">
                      <ListChecks />
                      manual
                    </Badge>
                  )}
                </div>
                <p className="text-muted-foreground">{item.description}</p>
                <p className="text-xs text-muted-foreground">Fixes: {item.check}</p>
                <Preview item={item} />
                {item.manual &&
                  (item.auto ? (
                    <Collapsible>
                      <CollapsibleTrigger asChild>
                        <Button variant="ghost" size="sm" className="group h-7 px-2 text-xs text-muted-foreground">
                          Or do it yourself
                          <ChevronDown className="transition-transform group-data-[state=open]:rotate-180" />
                        </Button>
                      </CollapsibleTrigger>
                      <CollapsibleContent className="pt-2">
                        <ManualSteps manual={item.manual} />
                      </CollapsibleContent>
                    </Collapsible>
                  ) : (
                    <ManualSteps manual={item.manual} />
                  ))}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
