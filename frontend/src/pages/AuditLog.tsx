import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Shield,
  Download,
  ChevronLeft,
  ChevronRight,
  Filter,
  RefreshCw,
  User,
  Clock
} from 'lucide-react';
import type { AuditEntry, AuditLogResponse, AuditStats, AuditAction, AuditResourceType, AuditStatus } from '@/types/audit';

export default function AuditLog() {
  const [logs, setLogs] = useState<AuditEntry[]>([]);
  const [stats, setStats] = useState<AuditStats | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [limit] = useState(25);

  // Filters
  const [actionFilter, setActionFilter] = useState<string>('');
  const [resourceTypeFilter, setResourceTypeFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        limit: limit.toString(),
        offset: (page * limit).toString(),
      });

      if (actionFilter) params.append('action', actionFilter);
      if (resourceTypeFilter) params.append('resource_type', resourceTypeFilter);
      if (statusFilter) params.append('status', statusFilter);

      const response = await fetch(`/api/audit?${params}`);
      if (response.ok) {
        const data: AuditLogResponse = await response.json();
        setLogs(data.logs);
        setTotal(data.total);
      }
    } catch (error) {
      console.error('Failed to fetch audit logs:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchStats = async () => {
    try {
      const response = await fetch('/api/audit/stats');
      if (response.ok) {
        const data: AuditStats = await response.json();
        setStats(data);
      }
    } catch (error) {
      console.error('Failed to fetch audit stats:', error);
    }
  };

  useEffect(() => {
    fetchLogs();
    fetchStats();
  }, [page, actionFilter, resourceTypeFilter, statusFilter]);

  const handleExport = async (format: 'json' | 'csv') => {
    try {
      const response = await fetch(`/api/audit/export?format=${format}`);
      if (response.ok) {
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `audit_log.${format}`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
      }
    } catch (error) {
      console.error('Failed to export audit logs:', error);
    }
  };

  const getActionBadge = (action: AuditAction) => {
    const variants: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
      CREATE: 'default',
      UPDATE: 'secondary',
      DELETE: 'destructive',
      LOGIN: 'default',
      LOGOUT: 'outline',
      LOGIN_FAILED: 'destructive',
      RUN_BACKUP: 'default',
      BACKUP_COMPLETE: 'default',
      BACKUP_FAILED: 'destructive',
    };
    return <Badge variant={variants[action] || 'outline'}>{action}</Badge>;
  };

  const getStatusBadge = (status: AuditStatus) => {
    return (
      <Badge variant={status === 'success' ? 'default' : 'destructive'}>
        {status}
      </Badge>
    );
  };

  const getResourceIcon = (type: AuditResourceType) => {
    const icons: Record<string, string> = {
      job: 'Backup job',
      server: 'Server',
      s3_config: 'S3 Storage',
      db_config: 'Database',
      notification_channel: 'Notification',
      session: 'Session',
    };
    return icons[type] || type;
  };

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleString();
  };

  const totalPages = Math.ceil(total / limit);

  const filteredLogs = searchQuery
    ? logs.filter(
        (log) =>
          log.user_name?.toLowerCase().includes(searchQuery.toLowerCase()) ||
          log.resource_name?.toLowerCase().includes(searchQuery.toLowerCase()) ||
          log.resource_id?.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : logs;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Shield className="h-8 w-8" />
            Audit Log
          </h1>
          <p className="text-muted-foreground mt-1">
            Track all system activities and changes
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => handleExport('csv')}>
            <Download className="h-4 w-4 mr-2" />
            Export CSV
          </Button>
          <Button variant="outline" onClick={() => handleExport('json')}>
            <Download className="h-4 w-4 mr-2" />
            Export JSON
          </Button>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Total Events
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{stats.total}</div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Successful
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-green-600">
                {stats.by_status.success || 0}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Failed
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold text-red-600">
                {stats.by_status.failure || 0}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Login Attempts
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">
                {(stats.by_action.LOGIN || 0) + (stats.by_action.LOGIN_FAILED || 0)}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Filters */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <Filter className="h-5 w-5" />
            Filters
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-4">
            <Input
              placeholder="Search by user or resource..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-64"
            />
            <Select value={actionFilter || "all"} onValueChange={(v) => setActionFilter(v === "all" ? "" : v)}>
              <SelectTrigger className="w-40">
                <SelectValue placeholder="Action" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Actions</SelectItem>
                <SelectItem value="CREATE">Create</SelectItem>
                <SelectItem value="UPDATE">Update</SelectItem>
                <SelectItem value="DELETE">Delete</SelectItem>
                <SelectItem value="LOGIN">Login</SelectItem>
                <SelectItem value="LOGOUT">Logout</SelectItem>
                <SelectItem value="LOGIN_FAILED">Login Failed</SelectItem>
                <SelectItem value="RUN_BACKUP">Run Backup</SelectItem>
              </SelectContent>
            </Select>
            <Select value={resourceTypeFilter || "all"} onValueChange={(v) => setResourceTypeFilter(v === "all" ? "" : v)}>
              <SelectTrigger className="w-40">
                <SelectValue placeholder="Resource Type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Resources</SelectItem>
                <SelectItem value="job">Jobs</SelectItem>
                <SelectItem value="server">Servers</SelectItem>
                <SelectItem value="s3_config">S3 Storage</SelectItem>
                <SelectItem value="db_config">Databases</SelectItem>
                <SelectItem value="notification_channel">Notifications</SelectItem>
                <SelectItem value="session">Sessions</SelectItem>
              </SelectContent>
            </Select>
            <Select value={statusFilter || "all"} onValueChange={(v) => setStatusFilter(v === "all" ? "" : v)}>
              <SelectTrigger className="w-32">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Status</SelectItem>
                <SelectItem value="success">Success</SelectItem>
                <SelectItem value="failure">Failure</SelectItem>
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              onClick={() => {
                setActionFilter('');
                setResourceTypeFilter('');
                setStatusFilter('');
                setSearchQuery('');
                setPage(0);
              }}
            >
              Clear Filters
            </Button>
            <Button variant="outline" onClick={fetchLogs}>
              <RefreshCw className="h-4 w-4 mr-2" />
              Refresh
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Audit Log Table */}
      <Card>
        <CardHeader>
          <CardTitle>Activity Log</CardTitle>
          <CardDescription>
            Showing {filteredLogs.length} of {total} events
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-[500px]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Timestamp</TableHead>
                  <TableHead>User</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Resource</TableHead>
                  <TableHead>Details</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>IP Address</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading ? (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8">
                      <RefreshCw className="h-6 w-6 animate-spin mx-auto" />
                    </TableCell>
                  </TableRow>
                ) : filteredLogs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                      No audit log entries found
                    </TableCell>
                  </TableRow>
                ) : (
                  filteredLogs.map((log) => (
                    <TableRow key={log.id}>
                      <TableCell className="whitespace-nowrap">
                        <div className="flex items-center gap-1 text-sm">
                          <Clock className="h-3 w-3 text-muted-foreground" />
                          {formatTimestamp(log.timestamp)}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <User className="h-3 w-3 text-muted-foreground" />
                          {log.user_name || 'system'}
                        </div>
                      </TableCell>
                      <TableCell>{getActionBadge(log.action)}</TableCell>
                      <TableCell>
                        <div className="text-sm">
                          <div className="font-medium">
                            {getResourceIcon(log.resource_type)}
                          </div>
                          {log.resource_name && (
                            <div className="text-muted-foreground text-xs">
                              {log.resource_name}
                            </div>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        {log.error_message ? (
                          <span className="text-red-600 text-sm">{log.error_message}</span>
                        ) : log.changes ? (
                          <span className="text-muted-foreground text-xs">
                            Changes recorded
                          </span>
                        ) : (
                          '-'
                        )}
                      </TableCell>
                      <TableCell>{getStatusBadge(log.status)}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {log.ip_address || '-'}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </ScrollArea>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4 pt-4 border-t">
              <div className="text-sm text-muted-foreground">
                Page {page + 1} of {totalPages}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage(Math.max(0, page - 1))}
                  disabled={page === 0}
                >
                  <ChevronLeft className="h-4 w-4" />
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
                  disabled={page >= totalPages - 1}
                >
                  Next
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
