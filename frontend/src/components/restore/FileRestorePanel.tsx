import { useState } from "react";
import { Archive, Loader2, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import FileRestoreDialog from "@/components/restore/FileRestoreDialog";
import FileSearch from "@/components/restore/FileSearch";
import SnapshotFileTree from "@/components/restore/SnapshotFileTree";
import { useTasks } from "@/hooks/useTasks";
import { compactTimestamp, slugify, startSnapshotZip } from "@/lib/restore";
import type { FileRestoreRequest, FileTarget, Restore } from "@/types/restore";

type Choice = FileTarget | "zip";

const OPTIONS: { value: Choice; title: string; help: string }[] = [
  {
    value: "original",
    title: "Original location",
    help: "Overwrites the selected paths on the job's server. A backup of those paths is taken first.",
  },
  { value: "alternate", title: "Another folder on the job's server", help: "Nothing existing is overwritten." },
  {
    value: "scratch",
    title: "Scratch extract on the backupx server",
    help: "Look at the files without touching the job's server. Removed after 24 h.",
  },
  { value: "zip", title: "Download as ZIP", help: "One path at a time; the download starts when it's ready." },
];

interface FileRestorePanelProps {
  jobId: string;
  jobName: string;
  snapshotId: string;
  storageId: string | null;
  onStarted: (restore: Restore | null) => void;
}

/** Step 3 for file jobs: pick paths (tree + search) and where they go. */
export default function FileRestorePanel({ jobId, jobName, snapshotId, storageId, onStarted }: FileRestorePanelProps) {
  const { track } = useTasks();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [choice, setChoice] = useState<Choice>("scratch");
  const [altPath, setAltPath] = useState(() => `/var/tmp/backupx-restore/${slugify(jobName)}-${compactTimestamp()}`);
  const [request, setRequest] = useState<FileRestoreRequest | null>(null);
  const [zipping, setZipping] = useState(false);

  const toggle = (path: string, checked: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(path);
      else next.delete(path);
      return next;
    });

  const paths = [...selected].sort();
  const zipBlocked = choice === "zip" && paths.length !== 1;
  const disabled = paths.length === 0 || zipBlocked || (choice === "alternate" && !altPath.trim()) || zipping;

  const go = async () => {
    if (choice === "zip") {
      setZipping(true);
      try {
        const data = await startSnapshotZip(jobId, snapshotId, paths[0], storageId);
        track(data.task, { autoDownload: true });
        toast.success("Preparing ZIP - the download starts when it's ready");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not start the ZIP");
      } finally {
        setZipping(false);
      }
      return;
    }
    setRequest({
      job_id: jobId,
      snapshot_id: snapshotId,
      storage_id: storageId,
      paths,
      target: choice,
      target_path: choice === "alternate" ? altPath.trim() : undefined,
    });
  };

  return (
    <div className="space-y-5">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <Label>Browse the backup</Label>
          <SnapshotFileTree
            key={`${snapshotId}:${storageId ?? ""}`}
            jobId={jobId}
            snapshotId={snapshotId}
            storageId={storageId}
            selected={selected}
            onToggle={toggle}
          />
        </div>
        <div className="space-y-2">
          <Label>Or search it</Label>
          <FileSearch
            key={`${snapshotId}:${storageId ?? ""}`}
            jobId={jobId}
            snapshotId={snapshotId}
            storageId={storageId}
            selected={selected}
            onToggle={toggle}
          />
        </div>
      </div>

      <div className="space-y-2">
        <Label>Selected ({paths.length})</Label>
        {paths.length === 0 ? (
          <p className="text-sm text-muted-foreground">Tick files or folders in the tree or the search results.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {paths.map((p) => (
              <Badge key={p} variant="secondary" className="max-w-full gap-1 font-mono">
                <span className="truncate">{p}</span>
                <button type="button" onClick={() => toggle(p, false)} aria-label={`Remove ${p}`}>
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            ))}
          </div>
        )}
      </div>

      <RadioGroup value={choice} onValueChange={(v) => setChoice(v as Choice)} className="gap-3">
        {OPTIONS.map((o) => (
          <Label
            key={o.value}
            htmlFor={`file-target-${o.value}`}
            className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary"
          >
            <RadioGroupItem value={o.value} id={`file-target-${o.value}`} className="mt-0.5" />
            <div className="grid min-w-0 flex-1 gap-1">
              <span className="font-medium">{o.title}</span>
              <span className="text-sm text-muted-foreground">{o.help}</span>
              {o.value === "alternate" && choice === "alternate" && (
                <Input
                  value={altPath}
                  onChange={(e) => setAltPath(e.target.value)}
                  className="mt-1 font-mono"
                  aria-label="Target folder"
                />
              )}
            </div>
          </Label>
        ))}
      </RadioGroup>

      {zipBlocked && paths.length > 1 && (
        <p className="text-sm text-muted-foreground">A ZIP takes one path. Select a single file or folder.</p>
      )}

      <div className="flex justify-end">
        <Button onClick={go} disabled={disabled} variant={choice === "original" ? "destructive" : "default"}>
          {zipping ? <Loader2 className="animate-spin" /> : choice === "zip" ? <Archive /> : null}
          {choice === "zip" ? "Download ZIP" : "Review restore..."}
        </Button>
      </div>

      <FileRestoreDialog request={request} onOpenChange={(o) => !o && setRequest(null)} onStarted={onStarted} />
    </div>
  );
}
