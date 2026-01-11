import { useState, useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import type { Job, JobFormData } from "@/types/job";
import type { S3Config } from "@/types/s3";
import { Loader2, ArrowLeft, Save, Server, FolderSync, CloudCog, CalendarClock, Shield, Settings } from "lucide-react";

const defaultFormData: JobFormData = {
  job_id: "",
  name: "",
  remote_host: "",
  ssh_port: 22,
  ssh_key: "/root/.ssh/id_rsa",
  directories: "",
  excludes: "",
  s3_endpoint: "",
  s3_bucket: "",
  s3_access_key: "",
  s3_secret_key: "",
  restic_password: "",
  backup_prefix: "",
  schedule_enabled: false,
  schedule_cron: "0 2 * * *",
  retention_hourly: 24,
  retention_daily: 7,
  retention_weekly: 4,
  retention_monthly: 12,
  timeout: 7200,
};

export default function JobForm() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const isEditing = !!jobId;

  const [formData, setFormData] = useState<JobFormData>(defaultFormData);
  const [s3Configs, setS3Configs] = useState<S3Config[]>([]);
  const [isLoading, setIsLoading] = useState(isEditing);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    fetchS3Configs();
    if (isEditing) {
      fetchJob();
    }
  }, [jobId]);

  const fetchS3Configs = async () => {
    try {
      const response = await fetch("/api/s3-configs");
      if (response.ok) {
        setS3Configs(await response.json());
      }
    } catch (error) {
      console.error("Failed to fetch S3 configs:", error);
    }
  };

  const fetchJob = async () => {
    try {
      const response = await fetch("/api/jobs");
      if (response.ok) {
        const jobs = await response.json();
        if (jobs[jobId!]) {
          const job: Job = jobs[jobId!];
          setFormData({
            job_id: jobId!,
            name: job.name,
            remote_host: job.remote_host,
            ssh_port: job.ssh_port,
            ssh_key: job.ssh_key,
            directories: job.directories.join("\n"),
            excludes: job.excludes.join("\n"),
            s3_endpoint: job.s3_endpoint,
            s3_bucket: job.s3_bucket,
            s3_access_key: job.s3_access_key,
            s3_secret_key: "",
            restic_password: "",
            backup_prefix: job.backup_prefix,
            schedule_enabled: job.schedule_enabled,
            schedule_cron: job.schedule_cron,
            retention_hourly: job.retention_hourly,
            retention_daily: job.retention_daily,
            retention_weekly: job.retention_weekly,
            retention_monthly: job.retention_monthly,
            timeout: job.timeout,
          });
        }
      }
    } catch (error) {
      toast.error("Failed to fetch job");
    } finally {
      setIsLoading(false);
    }
  };

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    const { name, value, type } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === "number" ? parseInt(value) || 0 : value,
    }));
  };

  const handleS3ConfigSelect = (configId: string) => {
    const config = s3Configs.find((c) => c.id === configId);
    if (config) {
      setFormData((prev) => ({
        ...prev,
        s3_endpoint: config.endpoint,
        s3_bucket: config.bucket,
        s3_access_key: config.access_key,
      }));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);

    try {
      const url = isEditing ? `/api/jobs/${jobId}` : "/api/jobs";
      const method = isEditing ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...formData,
          directories: formData.directories
            .split("\n")
            .map((d) => d.trim())
            .filter(Boolean),
          excludes: formData.excludes
            .split("\n")
            .map((e) => e.trim())
            .filter(Boolean),
        }),
      });

      if (response.ok) {
        toast.success(isEditing ? "Job updated" : "Job created");
        navigate("/jobs");
      } else {
        const data = await response.json();
        toast.error(data.error || "Failed to save job");
      }
    } catch {
      toast.error("Failed to save job");
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate("/jobs")}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold">
            {isEditing ? "Edit Backup Job" : "New Backup Job"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {isEditing
              ? "Update backup job configuration"
              : "Configure a new backup job"}
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <FolderSync className="h-5 w-5 text-primary" />
              <CardTitle>Basic Information</CardTitle>
            </div>
            <CardDescription>Job identification and naming</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="job_id">Job ID</Label>
              <Input
                id="job_id"
                name="job_id"
                value={formData.job_id}
                onChange={handleChange}
                placeholder="my-backup-job"
                disabled={isEditing}
                required
              />
              <p className="text-xs text-muted-foreground">
                Unique identifier for this job
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="name">Display Name</Label>
              <Input
                id="name"
                name="name"
                value={formData.name}
                onChange={handleChange}
                placeholder="My Backup Job"
                required
              />
              <p className="text-xs text-muted-foreground">
                Human-readable name for the job
              </p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <Server className="h-5 w-5 text-primary" />
              <CardTitle>Remote Server</CardTitle>
            </div>
            <CardDescription>SSH connection details for the backup source</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="remote_host">Remote Host</Label>
              <Input
                id="remote_host"
                name="remote_host"
                value={formData.remote_host}
                onChange={handleChange}
                placeholder="user@hostname"
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ssh_port">SSH Port</Label>
              <Input
                id="ssh_port"
                name="ssh_port"
                type="number"
                value={formData.ssh_port}
                onChange={handleChange}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="ssh_key">SSH Key Path</Label>
              <Input
                id="ssh_key"
                name="ssh_key"
                value={formData.ssh_key}
                onChange={handleChange}
                placeholder="/root/.ssh/id_rsa"
              />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <FolderSync className="h-5 w-5 text-primary" />
              <CardTitle>Backup Directories</CardTitle>
            </div>
            <CardDescription>Paths to backup and exclude patterns</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="directories">Directories to Backup</Label>
              <Textarea
                id="directories"
                name="directories"
                value={formData.directories}
                onChange={handleChange}
                placeholder="/home&#10;/etc&#10;/var/www"
                rows={5}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                One directory per line
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="excludes">Exclude Patterns</Label>
              <Textarea
                id="excludes"
                name="excludes"
                value={formData.excludes}
                onChange={handleChange}
                placeholder="*.log&#10;node_modules&#10;.cache"
                rows={5}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                One pattern per line
              </p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <CloudCog className="h-5 w-5 text-primary" />
              <CardTitle>S3 Storage</CardTitle>
            </div>
            <CardDescription>S3-compatible storage settings for backups</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {s3Configs.length > 0 && (
              <>
                <div className="space-y-2">
                  <Label>Use Saved Configuration</Label>
                  <Select onValueChange={handleS3ConfigSelect}>
                    <SelectTrigger>
                      <SelectValue placeholder="Select a saved S3 configuration" />
                    </SelectTrigger>
                    <SelectContent>
                      {s3Configs.map((config) => (
                        <SelectItem key={config.id} value={config.id}>
                          {config.name} ({config.bucket})
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <Separator />
              </>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="s3_endpoint">S3 Endpoint</Label>
                <Input
                  id="s3_endpoint"
                  name="s3_endpoint"
                  value={formData.s3_endpoint}
                  onChange={handleChange}
                  placeholder="s3.amazonaws.com"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="s3_bucket">Bucket</Label>
                <Input
                  id="s3_bucket"
                  name="s3_bucket"
                  value={formData.s3_bucket}
                  onChange={handleChange}
                  placeholder="my-backup-bucket"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="s3_access_key">Access Key</Label>
                <Input
                  id="s3_access_key"
                  name="s3_access_key"
                  value={formData.s3_access_key}
                  onChange={handleChange}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="s3_secret_key">
                  Secret Key{" "}
                  {isEditing && (
                    <span className="text-muted-foreground">(leave blank to keep current)</span>
                  )}
                </Label>
                <Input
                  id="s3_secret_key"
                  name="s3_secret_key"
                  type="password"
                  value={formData.s3_secret_key}
                  onChange={handleChange}
                  required={!isEditing}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="restic_password">
                  Restic Password{" "}
                  {isEditing && (
                    <span className="text-muted-foreground">(leave blank to keep current)</span>
                  )}
                </Label>
                <Input
                  id="restic_password"
                  name="restic_password"
                  type="password"
                  value={formData.restic_password}
                  onChange={handleChange}
                  required={!isEditing}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="backup_prefix">Backup Prefix</Label>
                <Input
                  id="backup_prefix"
                  name="backup_prefix"
                  value={formData.backup_prefix}
                  onChange={handleChange}
                  placeholder="backups/server1"
                />
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <CalendarClock className="h-5 w-5 text-primary" />
              <CardTitle>Schedule</CardTitle>
            </div>
            <CardDescription>Automatic backup scheduling</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between rounded-lg border p-4">
              <div className="space-y-0.5">
                <Label htmlFor="schedule_enabled" className="text-base">
                  Enable Scheduled Backups
                </Label>
                <p className="text-sm text-muted-foreground">
                  Automatically run backups on a schedule
                </p>
              </div>
              <Switch
                id="schedule_enabled"
                checked={formData.schedule_enabled}
                onCheckedChange={(checked) =>
                  setFormData((prev) => ({ ...prev, schedule_enabled: checked }))
                }
              />
            </div>
            {formData.schedule_enabled && (
              <div className="space-y-2">
                <Label htmlFor="schedule_cron">Cron Expression</Label>
                <Input
                  id="schedule_cron"
                  name="schedule_cron"
                  value={formData.schedule_cron}
                  onChange={handleChange}
                  placeholder="0 2 * * *"
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  Format: minute hour day month weekday (e.g., "0 2 * * *" for daily at 2 AM)
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <Shield className="h-5 w-5 text-primary" />
              <CardTitle>Retention Policy</CardTitle>
            </div>
            <CardDescription>How many snapshots to keep for each period</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-4">
            <div className="space-y-2">
              <Label htmlFor="retention_hourly">Hourly</Label>
              <Input
                id="retention_hourly"
                name="retention_hourly"
                type="number"
                value={formData.retention_hourly}
                onChange={handleChange}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="retention_daily">Daily</Label>
              <Input
                id="retention_daily"
                name="retention_daily"
                type="number"
                value={formData.retention_daily}
                onChange={handleChange}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="retention_weekly">Weekly</Label>
              <Input
                id="retention_weekly"
                name="retention_weekly"
                type="number"
                value={formData.retention_weekly}
                onChange={handleChange}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="retention_monthly">Monthly</Label>
              <Input
                id="retention_monthly"
                name="retention_monthly"
                type="number"
                value={formData.retention_monthly}
                onChange={handleChange}
              />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-4">
            <div className="flex items-center gap-2">
              <Settings className="h-5 w-5 text-primary" />
              <CardTitle>Advanced Settings</CardTitle>
            </div>
            <CardDescription>Additional configuration options</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-2 max-w-xs">
              <Label htmlFor="timeout">Timeout (seconds)</Label>
              <Input
                id="timeout"
                name="timeout"
                type="number"
                value={formData.timeout}
                onChange={handleChange}
              />
              <p className="text-xs text-muted-foreground">
                Maximum time for backup to complete (default: 7200 = 2 hours)
              </p>
            </div>
          </CardContent>
        </Card>

        <div className="flex justify-end gap-4">
          <Button type="button" variant="outline" onClick={() => navigate("/jobs")}>
            Cancel
          </Button>
          <Button type="submit" disabled={isSaving}>
            {isSaving ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Save className="h-4 w-4 mr-2" />
            )}
            {isEditing ? "Update Job" : "Create Job"}
          </Button>
        </div>
      </form>
    </div>
  );
}
