import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "sonner";
import type { JobStorage, PitrDetails } from "@/types/job";
import PitrBadge from "@/components/PitrBadge";
import { formatWindow } from "@/lib/pitr";
import { Download, Loader2, Send } from "lucide-react";

interface Props {
  jobId: string | null;
  jobName?: string;
  onOpenChange: (open: boolean) => void;
}

// <input type="datetime-local"> value for a Date, in the browser's time zone
function toLocalInput(date: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

export default function PitrRestoreDialog({ jobId, jobName, onOpenChange }: Props) {
  const [details, setDetails] = useState<PitrDetails | null>(null);
  const [storages, setStorages] = useState<JobStorage[]>([]);
  const [storageId, setStorageId] = useState<string>("");
  const [target, setTarget] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);

  useEffect(() => {
    if (!jobId) return;
    setIsLoading(true);
    Promise.all([
      fetch(`/api/jobs/${jobId}/pitr`).then((r) => r.json()),
      fetch(`/api/jobs/${jobId}/storages`).then((r) => r.json()),
    ])
      .then(([pitr, st]) => {
        setDetails(pitr);
        const list: JobStorage[] = st.storages || [];
        setStorages(list);
        setStorageId(list[0]?.id ?? "__inline");
        const end = pitr.window_end ? new Date(pitr.window_end) : new Date();
        setTarget(toLocalInput(new Date(end.getTime() - 60_000)));
      })
      .catch(() => toast.error("Failed to load point-in-time recovery status"))
      .finally(() => setIsLoading(false));
  }, [jobId]);

  const shipNow = async () => {
    const res = await fetch(`/api/jobs/${jobId}/pitr/ship`, { method: "POST" });
    const data = await res.json();
    if (res.ok) toast.success(data.message);
    else toast.error(data.error || "Failed to start");
  };

  const download = async () => {
    if (!target) return;
    setIsDownloading(true);
    try {
      const res = await fetch(`/api/jobs/${jobId}/pitr/restore-script`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ time: new Date(target).toISOString(), storage_id: storageId === "__inline" ? null : storageId }),
      });
      if (!res.ok) {
        const data = await res.json();
        toast.error(data.error || "Can't build a restore script for that time");
        return;
      }
      const blob = await res.blob();
      const name =
        res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `restore-${jobId}.sh`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
      toast.success("Restore script downloaded");
    } catch {
      toast.error("Download failed");
    } finally {
      setIsDownloading(false);
    }
  };

  return (
    <Dialog open={!!jobId} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Point-in-time restore{jobName ? `: ${jobName}` : ""}</DialogTitle>
          <DialogDescription>
            Downloads a script that restores the database as it was at the chosen second into a new local
            container (it never touches production). Run it on any machine with docker and restic.
          </DialogDescription>
        </DialogHeader>
        {isLoading || !details ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-primary" />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-lg border p-3 text-sm space-y-1">
              <div className="flex items-center gap-2">
                <PitrBadge pitr={details} />
                <span className="text-muted-foreground">
                  {formatWindow(details.window_start, details.window_end)}
                </span>
              </div>
              {details.lag_minutes !== null && (
                <p className="text-xs text-muted-foreground">Lag {Math.round(details.lag_minutes)} min</p>
              )}
              {details.windows.length > 1 && (
                <div className="text-xs text-muted-foreground">
                  Earlier restorable ranges:
                  {details.windows
                    .filter((w) => w.anchored)
                    .slice(0, -1)
                    .map((w) => (
                      <p key={w.chain_id}>• {formatWindow(w.from, w.to)}</p>
                    ))}
                </div>
              )}
              {details.last_error && <p className="text-xs text-destructive">{details.last_error}</p>}
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="pitr_target">Restore to (your local time)</Label>
                <Input
                  id="pitr_target"
                  type="datetime-local"
                  step={1}
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {target ? `= ${new Date(target).toISOString().replace(".000Z", "Z")} UTC` : ""}
                </p>
              </div>
              <div className="space-y-2">
                <Label>Storage</Label>
                <Select value={storageId} onValueChange={setStorageId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Primary storage" />
                  </SelectTrigger>
                  <SelectContent>
                    {storages.map((s) => (
                      <SelectItem key={s.id ?? "inline"} value={s.id ?? "__inline"}>
                        {s.name}
                        {s.primary ? " (primary)" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            {details.anchors.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Latest full backup with a binlog position:{" "}
                {new Date(details.anchors[details.anchors.length - 1].time).toLocaleString()} (
                {details.anchors[details.anchors.length - 1].binlog_file}:
                {details.anchors[details.anchors.length - 1].binlog_pos})
              </p>
            )}
          </div>
        )}
        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={shipNow} disabled={!details?.enabled}>
            <Send className="h-4 w-4 mr-2" />
            Ship now
          </Button>
          <Button onClick={download} disabled={isDownloading || !target || !details}>
            {isDownloading ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Download className="h-4 w-4 mr-2" />
            )}
            Download restore script
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
