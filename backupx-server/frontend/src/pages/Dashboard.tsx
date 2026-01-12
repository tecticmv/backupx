import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import type { Jobs, HistoryEntry } from "@/types/job";
import JobFormModal from "@/components/JobFormModal";
import {
  Play,
  Camera,
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  Loader2,
  FolderSync,
  TrendingUp,
  TrendingDown,
  CalendarClock,
  Plus,
} from "lucide-react";

export default function Dashboard() {
  const [jobs, setJobs] = useState<Jobs>({});
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [runningJobs, setRunningJobs] = useState<Set<string>>(new Set());
  const [jobModalOpen, setJobModalOpen] = useState(false);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchData = async () => {
    try {
      const [jobsRes, historyRes] = await Promise.all([
        fetch("/api/jobs"),
        fetch("/api/history"),
      ]);

      if (jobsRes.ok) {
        setJobs(await jobsRes.json());
      }
      if (historyRes.ok) {
        const historyData = await historyRes.json();
        setHistory(historyData.slice(-10).reverse());
      }
    } catch (error) {
      console.error("Failed to fetch data:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const runBackup = async (jobId: string) => {
    setRunningJobs((prev) => new Set(prev).add(jobId));
    try {
      const response = await fetch(`/api/jobs/${jobId}/run`, {
        method: "POST",
      });
      const data = await response.json();
      if (data.success) {
        toast.success("Backup started");
        fetchData();
      } else {
        toast.error(data.error || "Failed to start backup");
      }
    } catch {
      toast.error("Failed to start backup");
    } finally {
      setRunningJobs((prev) => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    }
  };

  const jobsList = Object.entries(jobs);
  const totalJobs = jobsList.length;
  const successJobs = jobsList.filter(([, j]) => j.status === "success").length;
  const failedJobs = jobsList.filter(([, j]) => j.status === "failed").length;
  const scheduledJobs = jobsList.filter(([, j]) => j.schedule_enabled).length;

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "success":
        return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
      case "failed":
        return <XCircle className="h-4 w-4 text-destructive" />;
      case "running":
        return <Loader2 className="h-4 w-4 text-primary animate-spin" />;
      case "timeout":
        return <Clock className="h-4 w-4 text-amber-500" />;
      default:
        return <AlertCircle className="h-4 w-4 text-muted-foreground" />;
    }
  };

  const getStatusBadge = (status: string) => {
    const variants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
      success: "default",
      failed: "destructive",
      running: "secondary",
      timeout: "destructive",
      pending: "outline",
    };
    const labels: Record<string, string> = {
      success: "Success",
      failed: "Failed",
      running: "Running",
      timeout: "Timeout",
      pending: "Pending",
    };
    return (
      <Badge variant={variants[status] || "outline"} className={status === "success" ? "bg-emerald-600 hover:bg-emerald-600" : ""}>
        {labels[status] || status}
      </Badge>
    );
  };

  const formatDuration = (seconds: number) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    return `${mins}m ${secs}s`;
  };

  const formatTime = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
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
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Jobs</CardTitle>
            <FolderSync className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{totalJobs}</div>
            <p className="text-xs text-muted-foreground">
              Configured backup jobs
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Successful</CardTitle>
            <TrendingUp className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-emerald-500">{successJobs}</div>
            <p className="text-xs text-muted-foreground">
              Last run succeeded
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Failed</CardTitle>
            <TrendingDown className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{failedJobs}</div>
            <p className="text-xs text-muted-foreground">
              Require attention
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Scheduled</CardTitle>
            <CalendarClock className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">{scheduledJobs}</div>
            <p className="text-xs text-muted-foreground">
              Automated backups
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-7">
        <Card className="lg:col-span-4">
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Backup Jobs</CardTitle>
              <CardDescription>Manage and run your backup jobs</CardDescription>
            </div>
            <Button size="sm" onClick={() => setJobModalOpen(true)}>
              <Plus className="h-4 w-4 mr-1" />
              New Job
            </Button>
          </CardHeader>
          <CardContent>
            {jobsList.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center">
                <FolderSync className="h-12 w-12 text-muted-foreground/50 mb-4" />
                <h3 className="font-medium">No backup jobs yet</h3>
                <p className="text-sm text-muted-foreground mt-1 mb-4">
                  Create your first backup job to get started
                </p>
                <Button onClick={() => setJobModalOpen(true)}>
                  <Plus className="h-4 w-4 mr-2" />
                  Create Job
                </Button>
              </div>
            ) : (
              <div className="space-y-2">
                {jobsList.map(([jobId, job]) => (
                  <div
                    key={jobId}
                    className="flex items-center justify-between p-3 rounded-lg border bg-card hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      {getStatusIcon(job.status)}
                      <div className="min-w-0">
                        <p className="font-medium truncate">{job.name}</p>
                        <p className="text-xs text-muted-foreground truncate">
                          {job.remote_host}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 ml-4">
                      {job.schedule_enabled && (
                        <Badge variant="outline" className="hidden sm:flex">
                          <CalendarClock className="h-3 w-3 mr-1" />
                          {job.schedule_cron}
                        </Badge>
                      )}
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => runBackup(jobId)}
                        disabled={runningJobs.has(jobId) || job.status === "running"}
                      >
                        {runningJobs.has(jobId) || job.status === "running" ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Play className="h-4 w-4" />
                        )}
                      </Button>
                      <Link to={`/jobs/${jobId}/snapshots`}>
                        <Button size="icon" variant="ghost">
                          <Camera className="h-4 w-4" />
                        </Button>
                      </Link>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Recent Activity</CardTitle>
            <CardDescription>Latest backup operations</CardDescription>
          </CardHeader>
          <CardContent>
            {history.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center">
                <Clock className="h-12 w-12 text-muted-foreground/50 mb-4" />
                <h3 className="font-medium">No activity yet</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  Run a backup to see activity here
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                {history.map((entry, idx) => (
                  <div key={idx} className="flex items-start gap-3">
                    {getStatusIcon(entry.status)}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm truncate">
                          {entry.job_name}
                        </span>
                        {getStatusBadge(entry.status)}
                      </div>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground mt-0.5">
                        <span>{formatDuration(entry.duration)}</span>
                        <span>•</span>
                        <span>{formatTime(entry.timestamp)}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <JobFormModal
        open={jobModalOpen}
        onOpenChange={setJobModalOpen}
        onSuccess={fetchData}
      />
    </div>
  );
}
