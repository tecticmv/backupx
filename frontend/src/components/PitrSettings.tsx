import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";
import type { JobFormData, PitrCheckResult } from "@/types/job";
import PitrPrereqTable from "@/components/PitrPrereqTable";
import { History, ListChecks, Loader2 } from "lucide-react";

interface Props {
  jobId?: string;
  formData: JobFormData;
  setFormData: React.Dispatch<React.SetStateAction<JobFormData>>;
}

export default function PitrSettings({ jobId, formData, setFormData }: Props) {
  const [result, setResult] = useState<PitrCheckResult | null>(null);
  const [isChecking, setIsChecking] = useState(false);

  const set = (patch: Partial<JobFormData>) => setFormData((prev) => ({ ...prev, ...patch }));

  const check = async () => {
    if (!jobId) return;
    setIsChecking(true);
    try {
      // Checks with the values in the form (client image), before they're saved
      const res = await fetch(`/api/jobs/${jobId}/pitr/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pitr_client_image: formData.pitr_client_image }),
      });
      const data = await res.json();
      if (res.ok) setResult(data);
      else toast.error(data.error || "Prerequisite check failed");
    } catch {
      toast.error("Prerequisite check failed");
    } finally {
      setIsChecking(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm font-medium">
        <History className="h-4 w-4 text-primary" />
        Point-in-time Recovery
      </div>
      <div className="flex items-center justify-between rounded-lg border p-3">
        <div className="space-y-0.5">
          <Label htmlFor="pitr_enabled">Enable point-in-time recovery</Label>
          <p className="text-xs text-muted-foreground">
            MySQL/MariaDB: a streamer on the job's server copies the binary logs continuously and backupx
            ships them to a sibling repository (&lt;prefix&gt;-binlog). PostgreSQL: WAL-G archiving
            (needs postgresql.conf changes and a restart you schedule).
          </p>
        </div>
        <Switch
          id="pitr_enabled"
          checked={formData.pitr_enabled}
          onCheckedChange={(checked) => set({ pitr_enabled: checked })}
        />
      </div>
      {formData.pitr_enabled && (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="pitr_interval">Ship every (minutes)</Label>
            <Input
              id="pitr_interval"
              type="number"
              min={1}
              max={60}
              value={formData.pitr_interval}
              onChange={(e) => set({ pitr_interval: parseInt(e.target.value) || 5 })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="pitr_retention_days">Keep (days)</Label>
            <Input
              id="pitr_retention_days"
              type="number"
              min={1}
              max={365}
              value={formData.pitr_retention_days}
              onChange={(e) => set({ pitr_retention_days: parseInt(e.target.value) || 7 })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="pitr_client_image">Client image (optional)</Label>
            <Input
              id="pitr_client_image"
              value={formData.pitr_client_image}
              placeholder="e.g. percona/percona-server:8.4"
              className="font-mono"
              onChange={(e) => set({ pitr_client_image: e.target.value })}
            />
          </div>
          <p className="text-xs text-muted-foreground md:col-span-3">
            Binlogs are kept back to the oldest full backup within the period. The client image runs
            mysqlbinlog/mysqldump in docker with the server's own version (needed when the server's local
            clients don't match, e.g. MySQL 8.4 with Ubuntu's 8.0 clients); leave empty to use local
            binaries. Docker-container databases always use their container's image.
          </p>
        </div>
      )}
      <div className="space-y-3">
        <Button type="button" variant="outline" size="sm" onClick={check} disabled={!jobId || isChecking}>
          {isChecking ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <ListChecks className="h-4 w-4 mr-2" />}
          Check prerequisites
        </Button>
        {!jobId && <p className="text-xs text-muted-foreground">Save the job first to run the check.</p>}
        {result && <PitrPrereqTable result={result} />}
      </div>
    </div>
  );
}
