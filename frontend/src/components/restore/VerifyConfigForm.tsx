import { useState } from "react";
import { ChevronDown, Loader2, Settings2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { getVerifyConfig, putVerifyConfig } from "@/lib/restore";
import type { VerifyConfig } from "@/types/restore";

interface Draft {
  keyTables: string;
  checkSql: string;
  healthUrl: string;
}

function toDraft(c: VerifyConfig): Draft {
  return { keyTables: c.key_tables.join("\n"), checkSql: c.check_sql ?? "", healthUrl: c.health_url ?? "" };
}

/** Collapsible editor for a job's restore checks (key tables, check SQL, health URL). */
export default function VerifyConfigForm({ jobId, onSaved }: { jobId: string; onSaved?: () => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next && !draft) {
      getVerifyConfig(jobId)
        .then((c) => setDraft(toDraft(c)))
        .catch((err) => setError(err instanceof Error ? err.message : "Could not load the checks"));
    }
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await putVerifyConfig(jobId, {
        key_tables: draft.keyTables
          .split(/[\n,]+/)
          .map((t) => t.trim())
          .filter(Boolean),
        check_sql: draft.checkSql.trim(),
        health_url: draft.healthUrl.trim(),
      });
      setDraft(toDraft(saved));
      toast.success("Restore checks saved");
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Collapsible open={open} onOpenChange={onOpenChange} className="rounded-md border">
      <CollapsibleTrigger asChild>
        <Button variant="ghost" className="w-full justify-between">
          <span className="flex items-center gap-2">
            <Settings2 />
            Key tables and checks
          </span>
          <ChevronDown className={open ? "rotate-180 transition-transform" : "transition-transform"} />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        {!draft ? (
          <div className="p-4 text-sm text-muted-foreground">
            {error ? <span className="text-destructive">{error}</span> : <Loader2 className="h-4 w-4 animate-spin" />}
          </div>
        ) : (
          <form onSubmit={save} className="grid gap-4 p-4 pt-2">
            <div className="grid gap-2">
              <Label htmlFor="verify-key-tables">Key tables</Label>
              <Textarea
                id="verify-key-tables"
                rows={4}
                className="font-mono text-xs"
                placeholder={"shop.orders\nshop.customers"}
                value={draft.keyTables}
                onChange={(e) => setDraft({ ...draft, keyTables: e.target.value })}
              />
              <p className="text-xs text-muted-foreground">
                One per line or comma-separated: db.table (MySQL) or db.schema.table (PostgreSQL). These get exact
                row counts on both sides; a mismatch is flagged.
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="verify-check-sql">Check SQL (optional)</Label>
              <Textarea
                id="verify-check-sql"
                rows={3}
                className="font-mono text-xs"
                placeholder="SELECT max(created_at) FROM shop.orders"
                value={draft.checkSql}
                onChange={(e) => setDraft({ ...draft, checkSql: e.target.value })}
              />
              <p className="text-xs text-muted-foreground">Runs on production and on the restored copy; the output is compared.</p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="verify-health-url">Health URL (optional)</Label>
              <Input
                id="verify-health-url"
                type="url"
                placeholder="https://app.example/health"
                value={draft.healthUrl}
                onChange={(e) => setDraft({ ...draft, healthUrl: e.target.value })}
              />
              <p className="text-xs text-muted-foreground">Fetched after production is replaced; 2xx counts as healthy.</p>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <div className="flex justify-end">
              <Button type="submit" size="sm" disabled={saving}>
                {saving && <Loader2 className="animate-spin" />}
                Save checks
              </Button>
            </div>
          </form>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
