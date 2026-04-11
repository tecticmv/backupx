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
import { Progress } from "@/components/ui/progress";
import { ContributionGraph } from "@/components/ContributionGraph";
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
  CalendarClock,
  Plus,
  Activity,
  Timer,
  Archive,
  RefreshCw,
  Database,
  Server,
} from "lucide-react";

interface ContributionDay {
  date: string;
  success: number;
  failed: number;
  total: number;
}

interface DashboardStats {
  total_jobs: number;
  success_jobs: number;
  failed_jobs: number;
  running_jobs: number;
  scheduled_jobs: number;
  success_rate: number;
  success_rate_period: string;
  last_24h: {
    success: number;
    failed: number;
    total: number;
  };
  daily_stats: Array<{
    date: string;
    day: string;
    success: number;
    failed: number;
    total: number;
  }>;
  contribution_data: ContributionDay[];
  next_backup: string | null;
  next_backup_job: string | null;
  avg_duration: number;
  total_snapshots: number;
}

export default function Dashboard() {
  const [jobs, setJobs] = useState<Jobs>({});
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [stats, setStats] = useState<DashboardStats | null>(null);
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
      const [jobsRes, historyRes, statsRes] = await Promise.all([
        fetch("/api/jobs"),
        fetch("/api/history"),
        fetch("/api/dashboard/stats"),
      ]);

      if (jobsRes.ok) {
        setJobs(await jobsRes.json());
      }
      if (historyRes.ok) {
        const historyData = await historyRes.json();
        setHistory(historyData.slice(0, 10));
      }
      if (statsRes.ok) {
        setStats(await statsRes.json());
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
    if (mins < 60) return `${mins}m ${secs}s`;
    const hours = Math.floor(mins / 60);
    const remainingMins = mins % 60;
    return `${hours}h ${remainingMins}m`;
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

  const formatNextBackup = (isoString: string | null) => {
    if (!isoString) return null;
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = date.getTime() - now.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);

    if (diffMins < 0) return "Overdue";
    if (diffMins < 60) return `in ${diffMins}m`;
    if (diffHours < 24) return `in ${diffHours}h ${diffMins % 60}m`;
    const diffDays = Math.floor(diffHours / 24);
    return `in ${diffDays}d ${diffHours % 24}h`;
  };

  // Calculate totals from contribution data
  const totalBackupsYear = stats?.contribution_data?.reduce((sum, d) => sum + d.total, 0) ?? 0;
  const successBackupsYear = stats?.contribution_data?.reduce((sum, d) => sum + d.success, 0) ?? 0;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Hero Stats Section */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card className="bg-gradient-to-br from-emerald-500/10 to-emerald-500/5 border-emerald-500/20">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Success Rate</CardTitle>
            <TrendingUp className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold text-emerald-500">{stats?.success_rate ?? 0}%</div>
            <Progress
              value={stats?.success_rate ?? 0}
              className="mt-2 h-1.5"
            />
            <p className="text-xs text-muted-foreground mt-2">
              Last {stats?.success_rate_period ?? '7 days'}
            </p>
          </CardContent>
        </Card>

        <Card className="bg-gradient-to-br from-primary/10 to-primary/5 border-primary/20">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Last 24 Hours</CardTitle>
            <Activity className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="flex items-baseline gap-2">
              <span className="text-3xl font-bold text-emerald-500">{stats?.last_24h.success ?? 0}</span>
              <span className="text-muted-foreground">/</span>
              <span className="text-3xl font-bold text-destructive">{stats?.last_24h.failed ?? 0}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              {stats?.last_24h.total ?? 0} backups completed
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Next Backup</CardTitle>
            <CalendarClock className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">
              {stats?.next_backup ? formatNextBackup(stats.next_backup) : "—"}
            </div>
            <p className="text-xs text-muted-foreground mt-2 truncate">
              {stats?.next_backup_job ?? "No scheduled backups"}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Snapshots</CardTitle>
            <Archive className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats?.total_snapshots ?? 0}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Successful backup snapshots
            </p>
          </CardContent>
        </Card>
      </div>

      {/* GitHub-style Contribution Graph */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">Backup Activity</CardTitle>
              <CardDescription>
                {totalBackupsYear} backups in the last year
                {successBackupsYear > 0 && (
                  <span className="ml-2 text-emerald-500">
                    ({successBackupsYear} successful)
                  </span>
                )}
              </CardDescription>
            </div>
            <div className="flex items-center gap-4 text-xs text-muted-foreground">
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-3 rounded-sm bg-emerald-500" />
                <span>Success</span>
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-3 rounded-sm bg-amber-500" />
                <span>Mixed</span>
              </div>
              <div className="flex items-center gap-1.5">
                <div className="w-3 h-3 rounded-sm bg-destructive" />
                <span>Failed</span>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {stats?.contribution_data && (
            <ContributionGraph data={stats.contribution_data} />
          )}
        </CardContent>
      </Card>

      {/* Quick Stats Row */}
      <div className="grid gap-4 md:grid-cols-5">
        <Card className="bg-card/50">
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-primary/10">
                <FolderSync className="h-5 w-5 text-primary" />
              </div>
              <div>
                <p className="text-2xl font-bold">{stats?.total_jobs ?? 0}</p>
                <p className="text-xs text-muted-foreground">Total Jobs</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-card/50">
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-emerald-500/10">
                <CheckCircle2 className="h-5 w-5 text-emerald-500" />
              </div>
              <div>
                <p className="text-2xl font-bold text-emerald-500">{stats?.success_jobs ?? 0}</p>
                <p className="text-xs text-muted-foreground">Healthy</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-card/50">
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-destructive/10">
                <XCircle className="h-5 w-5 text-destructive" />
              </div>
              <div>
                <p className="text-2xl font-bold text-destructive">{stats?.failed_jobs ?? 0}</p>
                <p className="text-xs text-muted-foreground">Need Attention</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-card/50">
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-primary/10">
                <CalendarClock className="h-5 w-5 text-primary" />
              </div>
              <div>
                <p className="text-2xl font-bold">{stats?.scheduled_jobs ?? 0}</p>
                <p className="text-xs text-muted-foreground">Scheduled</p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card className="bg-card/50">
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-primary/10">
                <Timer className="h-5 w-5 text-primary" />
              </div>
              <div>
                <p className="text-2xl font-bold">
                  {stats?.avg_duration ? formatDuration(stats.avg_duration) : "—"}
                </p>
                <p className="text-xs text-muted-foreground">Avg Duration</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Jobs and Activity Grid */}
      <div className="grid gap-6 lg:grid-cols-7">
        <Card className="lg:col-span-4">
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Backup Jobs</CardTitle>
              <CardDescription>Manage and run your backup jobs</CardDescription>
            </div>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={fetchData}>
                <RefreshCw className="h-4 w-4" />
              </Button>
              <Button size="sm" onClick={() => setJobModalOpen(true)}>
                <Plus className="h-4 w-4 mr-1" />
                New Job
              </Button>
            </div>
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
                        <div className="flex items-center gap-2">
                          <p className="font-medium truncate">{job.name}</p>
                          {job.backup_type === 'database' ? (
                            <Database className="h-3 w-3 text-muted-foreground" />
                          ) : (
                            <Server className="h-3 w-3 text-muted-foreground" />
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground truncate">
                          {job.backup_type === 'database' ? 'Database Backup' : job.remote_host || 'Local'}
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
