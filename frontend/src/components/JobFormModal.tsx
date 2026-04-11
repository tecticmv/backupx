import { useState, useEffect } from "react";
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
import type { Server } from "@/types/server";
import type { DatabaseConfig } from "@/types/database";
import {
  Loader2,
  Save,
  Server as ServerIcon,
  FolderSync,
  FolderOpen,
  CloudCog,
  CalendarClock,
  Shield,
  Settings,
  Database,
  Eye,
  EyeOff,
  Copy,
} from "lucide-react";
import DirectoryBrowser from "@/components/DirectoryBrowser";

const defaultFormData: JobFormData = {
  job_id: "",
  name: "",
  backup_type: "filesystem",
  server_id: "",
  directories: "",
  excludes: "",
  database_config_id: "",
  s3_config_id: "",
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

interface JobFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  jobId?: string;
  onSuccess: () => void;
}

export default function JobFormModal({
  open,
  onOpenChange,
  jobId,
  onSuccess,
}: JobFormModalProps) {
  const isEditing = !!jobId;

  const [formData, setFormData] = useState<JobFormData>(defaultFormData);
  const [s3Configs, setS3Configs] = useState<S3Config[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [dbConfigs, setDbConfigs] = useState<DatabaseConfig[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [revealedPassword, setRevealedPassword] = useState<string | null>(null);
  const [isRevealing, setIsRevealing] = useState(false);

  useEffect(() => {
    if (open) {
      fetchS3Configs();
      fetchServers();
      fetchDbConfigs();
      setRevealedPassword(null);
      if (jobId) {
        fetchJob();
      } else {
        setFormData(defaultFormData);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, jobId]);

  const fetchS3Configs = async () => {
    try {
      const response = await fetch("/api/s3-configs");
      if (response.ok) {
        const configs = await response.json();
        setS3Configs(configs);
      }
    } catch (error) {
      console.error("Failed to fetch S3 configs:", error);
    }
  };

  const fetchServers = async () => {
    try {
      const response = await fetch("/api/servers");
      if (response.ok) {
        const serverList = await response.json();
        setServers(serverList);
      }
    } catch (error) {
      console.error("Failed to fetch servers:", error);
    }
  };

  const fetchDbConfigs = async () => {
    try {
      const response = await fetch("/api/databases");
      if (response.ok) {
        const configs = await response.json();
        setDbConfigs(configs);
      }
    } catch (error) {
      console.error("Failed to fetch database configs:", error);
    }
  };

  const fetchJob = async () => {
    setIsLoading(true);
    try {
      const response = await fetch("/api/jobs");
      if (response.ok) {
        const jobs = await response.json();
        if (jobs[jobId!]) {
          const job: Job = jobs[jobId!];
          setFormData({
            job_id: jobId!,
            name: job.name,
            backup_type: job.backup_type || "filesystem",
            server_id: job.server_id || "",
            directories: (job.directories || []).join("\n"),
            excludes: (job.excludes || []).join("\n"),
            database_config_id: job.database_config_id || "",
            s3_config_id: job.s3_config_id || "",
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
    } catch {
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

  const handleRevealPassword = async () => {
    if (!jobId) return;
    if (revealedPassword !== null) {
      setRevealedPassword(null);
      return;
    }
    setIsRevealing(true);
    try {
      const response = await fetch(`/api/jobs/${jobId}/reveal-password`, {
        method: "POST",
      });
      if (response.ok) {
        const data = await response.json();
        setRevealedPassword(data.restic_password);
      } else {
        const data = await response.json();
        toast.error(data.error || "Failed to reveal password");
      }
    } catch {
      toast.error("Failed to reveal password");
    } finally {
      setIsRevealing(false);
    }
  };

  const handleCopyPassword = async () => {
    if (!revealedPassword) return;
    try {
      await navigator.clipboard.writeText(revealedPassword);
      toast.success("Password copied to clipboard");
    } catch {
      toast.error("Failed to copy");
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.server_id) {
      toast.error("Please select a server");
      return;
    }

    if (!formData.s3_config_id) {
      toast.error("Please select an S3 storage configuration");
      return;
    }

    if (formData.backup_type === "database" && !formData.database_config_id) {
      toast.error("Please select a database configuration");
      return;
    }

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
        onOpenChange(false);
        onSuccess();
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

  const selectedServer = servers.find((s) => s.id === formData.server_id);
  const selectedS3Config = s3Configs.find((c) => c.id === formData.s3_config_id);
  const selectedDbConfig = dbConfigs.find((c) => c.id === formData.database_config_id);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] flex flex-col p-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-4 shrink-0">
          <DialogTitle>
            {isEditing ? "Edit Backup Job" : "New Backup Job"}
          </DialogTitle>
          <DialogDescription>
            {isEditing
              ? "Update backup job configuration"
              : "Configure a new backup job"}
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col min-h-0 flex-1 overflow-hidden">
            <div className="flex-1 overflow-y-auto px-6">
              <div className="space-y-6 py-2">
                {/* Basic Information */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <FolderSync className="h-4 w-4 text-primary" />
                    Basic Information
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
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
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label>Backup Type</Label>
                    <Select
                      value={formData.backup_type}
                      onValueChange={(value: "filesystem" | "database") =>
                        setFormData((prev) => ({ ...prev, backup_type: value }))
                      }
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select backup type" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="filesystem">
                          <div className="flex items-center gap-2">
                            <FolderSync className="h-4 w-4" />
                            Filesystem Backup
                          </div>
                        </SelectItem>
                        <SelectItem value="database">
                          <div className="flex items-center gap-2">
                            <Database className="h-4 w-4" />
                            MySQL Database Backup
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <Separator />

                {/* Server Selection */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <ServerIcon className="h-4 w-4 text-primary" />
                    Remote Server
                  </div>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label>Server</Label>
                      <Select
                        value={formData.server_id}
                        onValueChange={(value) =>
                          setFormData((prev) => ({ ...prev, server_id: value }))
                        }
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="Select a server" />
                        </SelectTrigger>
                        <SelectContent>
                          {servers.length === 0 ? (
                            <SelectItem value="_empty" disabled>
                              No servers available
                            </SelectItem>
                          ) : (
                            servers.map((server) => (
                              <SelectItem key={server.id} value={server.id}>
                                {server.name} ({server.ssh_user}@{server.host})
                              </SelectItem>
                            ))
                          )}
                        </SelectContent>
                      </Select>
                      {servers.length === 0 && (
                        <p className="text-xs text-amber-600">
                          Please add servers in Settings first
                        </p>
                      )}
                    </div>
                    {selectedServer && (
                      <div className="rounded-lg border bg-muted/50 p-3 text-sm">
                        <div className="grid grid-cols-2 gap-2 text-muted-foreground">
                          <span>Host:</span>
                          <span className="font-mono">{selectedServer.host}</span>
                          <span>SSH User:</span>
                          <span className="font-mono">{selectedServer.ssh_user}</span>
                          <span>SSH Port:</span>
                          <span className="font-mono">{selectedServer.ssh_port}</span>
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                <Separator />

                {/* Backup Directories - Only for filesystem backups */}
                {formData.backup_type === "filesystem" && (
                  <div className="space-y-4">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <FolderSync className="h-4 w-4 text-primary" />
                      Backup Directories
                    </div>
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <Label htmlFor="directories">Directories to Backup</Label>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => setBrowseOpen(true)}
                            disabled={!formData.server_id}
                          >
                            <FolderOpen className="h-4 w-4 mr-1" />
                            Browse
                          </Button>
                        </div>
                        <Textarea
                          id="directories"
                          name="directories"
                          value={formData.directories}
                          onChange={handleChange}
                          placeholder={"/home\n/etc\n/var/www"}
                          rows={4}
                          className="font-mono text-sm"
                        />
                        <p className="text-xs text-muted-foreground">
                          One directory per line
                        </p>
                        {formData.backup_type === "filesystem" && (
                          <DirectoryBrowser
                            serverId={formData.server_id}
                            open={browseOpen}
                            onOpenChange={setBrowseOpen}
                            selectedPaths={formData.directories.split("\n").filter(Boolean)}
                            onSelect={(paths) => {
                              setFormData((prev) => ({
                                ...prev,
                                directories: paths.join("\n"),
                              }));
                            }}
                          />
                        )}
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="excludes">Exclude Patterns</Label>
                        <Textarea
                          id="excludes"
                          name="excludes"
                          value={formData.excludes}
                          onChange={handleChange}
                          placeholder={"*.log\nnode_modules\n.cache"}
                          rows={4}
                          className="font-mono text-sm"
                        />
                        <p className="text-xs text-muted-foreground">
                          One pattern per line
                        </p>
                      </div>
                    </div>
                  </div>
                )}

                {/* Database Selection - Only for database backups */}
                {formData.backup_type === "database" && (
                  <div className="space-y-4">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <Database className="h-4 w-4 text-primary" />
                      Database Configuration
                    </div>
                    <div className="space-y-4">
                      <div className="space-y-2">
                        <Label>Database</Label>
                        <Select
                          value={formData.database_config_id}
                          onValueChange={(value) =>
                            setFormData((prev) => ({ ...prev, database_config_id: value }))
                          }
                        >
                          <SelectTrigger>
                            <SelectValue placeholder="Select a database configuration" />
                          </SelectTrigger>
                          <SelectContent>
                            {dbConfigs.length === 0 ? (
                              <SelectItem value="_empty" disabled>
                                No database configurations available
                              </SelectItem>
                            ) : (
                              dbConfigs.map((config) => (
                                <SelectItem key={config.id} value={config.id}>
                                  {config.name} ({config.host}:{config.port})
                                </SelectItem>
                              ))
                            )}
                          </SelectContent>
                        </Select>
                        {dbConfigs.length === 0 && (
                          <p className="text-xs text-amber-600">
                            Please configure databases in Configuration &gt; Databases first
                          </p>
                        )}
                      </div>
                      {selectedDbConfig && (
                        <div className="rounded-lg border bg-muted/50 p-3 text-sm">
                          <div className="grid grid-cols-2 gap-2 text-muted-foreground">
                            <span>Host:</span>
                            <span className="font-mono">{selectedDbConfig.host}:{selectedDbConfig.port}</span>
                            <span>Username:</span>
                            <span className="font-mono">{selectedDbConfig.username}</span>
                            <span>Databases:</span>
                            <span className="font-mono">
                              {selectedDbConfig.databases === "*" ? "All databases" : selectedDbConfig.databases}
                            </span>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {(formData.backup_type === "filesystem" || formData.backup_type === "database") && <Separator />}

                {/* S3 Storage */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <CloudCog className="h-4 w-4 text-primary" />
                    S3 Storage
                  </div>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label>S3 Storage Configuration</Label>
                      <Select
                        value={formData.s3_config_id}
                        onValueChange={(value) =>
                          setFormData((prev) => ({ ...prev, s3_config_id: value }))
                        }
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="Select an S3 storage configuration" />
                        </SelectTrigger>
                        <SelectContent>
                          {s3Configs.length === 0 ? (
                            <SelectItem value="_empty" disabled>
                              No S3 configurations available
                            </SelectItem>
                          ) : (
                            s3Configs.map((config) => (
                              <SelectItem key={config.id} value={config.id}>
                                {config.name} ({config.bucket})
                              </SelectItem>
                            ))
                          )}
                        </SelectContent>
                      </Select>
                      {s3Configs.length === 0 && (
                        <p className="text-xs text-amber-600">
                          Please configure S3 storage in Settings first
                        </p>
                      )}
                    </div>
                    {selectedS3Config && (
                      <div className="rounded-lg border bg-muted/50 p-3 text-sm">
                        <div className="grid grid-cols-2 gap-2 text-muted-foreground">
                          <span>Endpoint:</span>
                          <span className="font-mono">{selectedS3Config.endpoint}</span>
                          <span>Bucket:</span>
                          <span className="font-mono">{selectedS3Config.bucket}</span>
                        </div>
                      </div>
                    )}
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <Label htmlFor="backup_prefix">Backup Prefix</Label>
                        <Input
                          id="backup_prefix"
                          name="backup_prefix"
                          value={formData.backup_prefix}
                          onChange={handleChange}
                          placeholder="backups/server1"
                        />
                        <p className="text-xs text-muted-foreground">
                          Path prefix within the bucket
                        </p>
                      </div>
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <Label htmlFor="restic_password">
                            Restic Password{" "}
                            {isEditing && (
                              <span className="text-muted-foreground text-xs">
                                (blank to keep current)
                              </span>
                            )}
                          </Label>
                          {isEditing && (
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-6 text-xs gap-1"
                              onClick={handleRevealPassword}
                              disabled={isRevealing}
                            >
                              {isRevealing ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : revealedPassword !== null ? (
                                <EyeOff className="h-3 w-3" />
                              ) : (
                                <Eye className="h-3 w-3" />
                              )}
                              {revealedPassword !== null ? "Hide" : "Reveal"}
                            </Button>
                          )}
                        </div>
                        <Input
                          id="restic_password"
                          name="restic_password"
                          type="password"
                          value={formData.restic_password}
                          onChange={handleChange}
                          required={!isEditing}
                          placeholder={isEditing ? "•••••••••••" : ""}
                        />
                        {revealedPassword !== null && (
                          <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-2 space-y-1">
                            <div className="flex items-center justify-between gap-2">
                              <code className="text-xs font-mono break-all flex-1 select-all">
                                {revealedPassword}
                              </code>
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-6 w-6 p-0 shrink-0"
                                onClick={handleCopyPassword}
                                title="Copy to clipboard"
                              >
                                <Copy className="h-3 w-3" />
                              </Button>
                            </div>
                            <p className="text-[10px] text-amber-700 dark:text-amber-400">
                              Store this securely. This action is audit-logged.
                            </p>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                <Separator />

                {/* Schedule */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <CalendarClock className="h-4 w-4 text-primary" />
                    Schedule
                  </div>
                  <div className="flex items-center justify-between rounded-lg border p-3">
                    <div className="space-y-0.5">
                      <Label htmlFor="schedule_enabled">
                        Enable Scheduled Backups
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        Automatically run backups on a schedule
                      </p>
                    </div>
                    <Switch
                      id="schedule_enabled"
                      checked={formData.schedule_enabled}
                      onCheckedChange={(checked) =>
                        setFormData((prev) => ({
                          ...prev,
                          schedule_enabled: checked,
                        }))
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
                        Format: minute hour day month weekday (e.g., "0 2 * * *"
                        for daily at 2 AM)
                      </p>
                    </div>
                  )}
                </div>

                <Separator />

                {/* Retention Policy */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <Shield className="h-4 w-4 text-primary" />
                    Retention Policy
                  </div>
                  <div className="grid gap-4 grid-cols-2 md:grid-cols-4">
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
                  </div>
                </div>

                <Separator />

                {/* Advanced Settings */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <Settings className="h-4 w-4 text-primary" />
                    Advanced Settings
                  </div>
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
                      Maximum time for backup to complete (default: 7200 = 2
                      hours)
                    </p>
                  </div>
                </div>
              </div>
            </div>

            <DialogFooter className="px-6 py-4 border-t shrink-0">
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={isSaving || servers.length === 0 || s3Configs.length === 0}>
                {isSaving ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-2" />
                )}
                {isEditing ? "Update Job" : "Create Job"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
