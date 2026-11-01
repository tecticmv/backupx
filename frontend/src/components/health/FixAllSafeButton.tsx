import { useState } from "react";
import { Loader2, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { apiJson } from "@/lib/api";
import { useTasks } from "@/hooks/useTasks";
import type { SafeFixPreviewResponse } from "@/types/health";
import type { TaskStartResponse } from "@/types/task";

/**
 * "Fix all safe issues (n)": the popover previews exactly which fixes will run
 * (the server's list), and Confirm sends those issue ids only.
 */
export default function FixAllSafeButton({ count, onChanged }: { count: number; onChanged: () => void }) {
  const { track } = useTasks();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<SafeFixPreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    setPreview(null);
    setError(null);
    try {
      setPreview(await apiJson<SafeFixPreviewResponse>("/api/fixes/all-safe", { method: "POST", json: { preview: true } }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the list of fixes");
    }
  };

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) load();
  };

  const confirm = async () => {
    if (!preview?.fixes.length) return;
    setSubmitting(true);
    try {
      const res = await apiJson<TaskStartResponse>("/api/fixes/all-safe", {
        method: "POST",
        json: { issue_ids: preview.fixes.map((f) => f.issue_id) },
      });
      track(res.task, { onDone: () => onChanged() });
      toast.success(res.message || `Fixing ${preview.fixes.length} issue${preview.fixes.length === 1 ? "" : "s"}`);
      setOpen(false);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not start the fixes");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button size="sm" disabled={count === 0}>
          <Wand2 />
          Fix all safe issues ({count})
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96 max-w-[calc(100vw-2rem)]">
        <PopoverHeader>
          <PopoverTitle>Fix all safe issues</PopoverTitle>
          <PopoverDescription>These fixes run one click each, without downtime.</PopoverDescription>
        </PopoverHeader>
        <div className="mt-3 max-h-72 overflow-y-auto">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : !preview ? (
            <div className="space-y-2" aria-busy="true">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : preview.fixes.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nothing to fix right now.</p>
          ) : (
            <ul className="space-y-2">
              {preview.fixes.map((f) => (
                <li key={f.issue_id} className="rounded-md border p-2 text-sm">
                  <p className="font-medium">{f.label}</p>
                  <p className="text-xs text-muted-foreground">
                    {f.title} · {f.resource.name}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button size="sm" onClick={confirm} disabled={!preview?.fixes.length || submitting}>
            {submitting && <Loader2 className="animate-spin" />}
            Fix {preview?.fixes.length ?? ""} {preview?.fixes.length === 1 ? "issue" : "issues"}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
