import { useState, useEffect } from "react";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import type { Server, ServerFormData, ConnectionType } from "@/types/server";
import {
  Plus,
  Pencil,
  Trash2,
  Loader2,
  TestTube,
  Server as ServerIcon,
  Monitor,
  Network,
  Key,
  CheckCircle2,
  XCircle,
  RefreshCw,
} from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const initialServerFormData: ServerFormData = {
  name: "",
  host: "",
  connection_type: "ssh",
  ssh_port: 22,
  ssh_user: "root",
  ssh_key: "/home/backupx/.ssh/id_rsa",
  agent_port: 8090,
  agent_api_key: "",
  status: "active",
};

type ConnectionStatus = "online" | "offline" | "checking" | "unknown";

interface ServerStatus {
  status: ConnectionStatus;
  message?: string;
  agentName?: string;
  version?: string;
}

export default function Servers() {
  const [servers, setServers] = useState<Server[]>([]);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<Server | null>(null);
  const [deletingServer, setDeletingServer] = useState<Server | null>(null);
  const [formData, setFormData] = useState<ServerFormData>(initialServerFormData);
  const [isSaving, setIsSaving] = useState(false);
  const [isTestingConnection, setIsTestingConnection] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [serverStatuses, setServerStatuses] = useState<Record<string, ServerStatus>>({});
  const [isRefreshingAll, setIsRefreshingAll] = useState(false);

  useEffect(() => {
    fetchServers();
  }, []);

  // Check all server connections when servers are loaded
  useEffect(() => {
    if (servers.length > 0) {
      checkAllConnections();
    }
  }, [servers.length]);

  const fetchServers = async () => {
    setIsLoading(true);
    try {
      const response = await fetch("/api/servers");
      if (response.ok) {
        setServers(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch servers:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const checkServerConnection = async (serverId: string): Promise<ServerStatus> => {
    setServerStatuses((prev) => ({
      ...prev,
      [serverId]: { status: "checking" },
    }));

    try {
      const response = await fetch(`/api/servers/${serverId}/test`, {
        method: "POST",
      });
      const data = await response.json();

      const status: ServerStatus = {
        status: data.success ? "online" : "offline",
        message: data.message || data.error,
        agentName: data.agent_name,
        version: data.version,
      };

      setServerStatuses((prev) => ({
        ...prev,
        [serverId]: status,
      }));

      return status;
    } catch {
      const status: ServerStatus = { status: "offline", message: "Connection failed" };
      setServerStatuses((prev) => ({
        ...prev,
        [serverId]: status,
      }));
      return status;
    }
  };

  const checkAllConnections = async () => {
    setIsRefreshingAll(true);
    await Promise.all(servers.map((server) => checkServerConnection(server.id)));
    setIsRefreshingAll(false);
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === "number" ? parseInt(value) || 0 : value,
    }));
  };

  const handleConnectionTypeChange = (value: ConnectionType) => {
    setFormData((prev) => ({
      ...prev,
      connection_type: value,
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);

    try {
      const url = editingServer
        ? `/api/servers/${editingServer.id}`
        : "/api/servers";
      const method = editingServer ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save server");
      }

      toast.success(editingServer ? "Server updated" : "Server created");
      setIsDialogOpen(false);
      setEditingServer(null);
      setFormData(initialServerFormData);
      fetchServers();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSaving(false);
    }
  };

  const handleEdit = (server: Server) => {
    setEditingServer(server);
    setFormData({
      name: server.name,
      host: server.host,
      connection_type: server.connection_type || 'ssh',
      ssh_port: server.ssh_port || 22,
      ssh_user: server.ssh_user || 'root',
      ssh_key: server.ssh_key || '/home/backupx/.ssh/id_rsa',
      agent_port: server.agent_port || 8090,
      agent_api_key: server.agent_api_key || '',
      status: server.status || 'active',
    });
    setIsDialogOpen(true);
  };

  const handleDelete = async () => {
    if (!deletingServer) return;

    try {
      const response = await fetch(`/api/servers/${deletingServer.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete server");

      toast.success("Server deleted");
      setIsDeleteDialogOpen(false);
      setDeletingServer(null);
      fetchServers();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestConnection = async () => {
    setIsTestingConnection(true);
    try {
      const response = await fetch("/api/servers/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Connection test failed");
      }

      toast.success(data.message || "Connection successful!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Connection test failed");
    } finally {
      setIsTestingConnection(false);
    }
  };

  const openNewDialog = () => {
    setEditingServer(null);
    setFormData(initialServerFormData);
    setIsDialogOpen(true);
  };

  const sshServers = servers.filter((s) => s.connection_type !== 'agent');
  const agentServers = servers.filter((s) => s.connection_type === 'agent');

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Servers</CardTitle>
            <Monitor className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{servers.length}</div>
            <p className="text-xs text-muted-foreground">
              Configured for backup
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">SSH Servers</CardTitle>
            <Key className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{sshServers.length}</div>
            <p className="text-xs text-muted-foreground">
              SSH key authentication
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Agent Servers</CardTitle>
            <Network className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{agentServers.length}</div>
            <p className="text-xs text-muted-foreground">
              BackupX Agent
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Unique Hosts</CardTitle>
            <ServerIcon className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {new Set(servers.map((s) => s.host)).size}
            </div>
            <p className="text-xs text-muted-foreground">
              Different hostnames
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Servers Table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Remote Servers</CardTitle>
            <CardDescription>
              Configure remote servers for backup via SSH or BackupX Agent
            </CardDescription>
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={checkAllConnections}
              disabled={isRefreshingAll || servers.length === 0}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${isRefreshingAll ? "animate-spin" : ""}`} />
              Refresh Status
            </Button>
            <Button onClick={openNewDialog}>
              <Plus className="h-4 w-4 mr-2" />
              Add Server
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {servers.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Monitor className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No servers configured</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add your first remote server to get started
              </p>
              <Button onClick={openNewDialog}>
                <Plus className="h-4 w-4 mr-2" />
                Add Server
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Status</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Connection</TableHead>
                  <TableHead>Enabled</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {servers.map((server) => {
                  const status = serverStatuses[server.id];
                  return (
                  <TableRow key={server.id}>
                    <TableCell>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <div className="flex items-center">
                            {status?.status === "checking" ? (
                              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                            ) : status?.status === "online" ? (
                              <CheckCircle2 className="h-4 w-4 text-green-500" />
                            ) : status?.status === "offline" ? (
                              <XCircle className="h-4 w-4 text-red-500" />
                            ) : (
                              <div className="h-4 w-4 rounded-full bg-muted" />
                            )}
                          </div>
                        </TooltipTrigger>
                        <TooltipContent>
                          {status?.status === "checking" ? (
                            "Checking connection..."
                          ) : status?.status === "online" ? (
                            <div>
                              <p className="font-medium text-green-500">Online</p>
                              {status.agentName && <p className="text-xs">Agent: {status.agentName}</p>}
                              {status.version && <p className="text-xs">Version: {status.version}</p>}
                            </div>
                          ) : status?.status === "offline" ? (
                            <div>
                              <p className="font-medium text-red-500">Offline</p>
                              {status.message && <p className="text-xs">{status.message}</p>}
                            </div>
                          ) : (
                            "Unknown - click refresh to check"
                          )}
                        </TooltipContent>
                      </Tooltip>
                    </TableCell>
                    <TableCell className="font-medium">{server.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {server.host}
                    </TableCell>
                    <TableCell>
                      <Badge variant={server.connection_type === 'agent' ? 'default' : 'secondary'}>
                        {server.connection_type === 'agent' ? (
                          <>
                            <Network className="h-3 w-3 mr-1" />
                            Agent
                          </>
                        ) : (
                          <>
                            <Key className="h-3 w-3 mr-1" />
                            SSH
                          </>
                        )}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {server.connection_type === 'agent'
                        ? `Port ${server.agent_port}`
                        : `${server.ssh_user}@:${server.ssh_port}`}
                    </TableCell>
                    <TableCell>
                      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                        server.status === "active"
                          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400"
                          : "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400"
                      }`}>
                        {server.status === "active" ? "Active" : "Inactive"}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() => checkServerConnection(server.id)}
                              disabled={status?.status === "checking"}
                            >
                              <RefreshCw className={`h-4 w-4 ${status?.status === "checking" ? "animate-spin" : ""}`} />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Test connection</TooltipContent>
                        </Tooltip>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleEdit(server)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setDeletingServer(server);
                            setIsDeleteDialogOpen(true);
                          }}
                          className="text-destructive hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Server Dialog */}
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleSubmit}>
            <DialogHeader>
              <DialogTitle>
                {editingServer ? "Edit Server" : "Add Server"}
              </DialogTitle>
              <DialogDescription>
                Configure a remote server for backups via SSH or BackupX Agent.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  name="name"
                  placeholder="Production Server"
                  value={formData.name}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="host">Host</Label>
                <Input
                  id="host"
                  name="host"
                  placeholder="192.168.1.100 or server.example.com"
                  value={formData.host}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="connection_type">Connection Type</Label>
                <Select
                  value={formData.connection_type}
                  onValueChange={handleConnectionTypeChange}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select connection type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ssh">
                      <div className="flex items-center">
                        <Key className="h-4 w-4 mr-2" />
                        SSH (Key-based)
                      </div>
                    </SelectItem>
                    <SelectItem value="agent">
                      <div className="flex items-center">
                        <Network className="h-4 w-4 mr-2" />
                        BackupX Agent
                      </div>
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {formData.connection_type === 'ssh'
                    ? 'Connect via SSH using key authentication'
                    : 'Connect via BackupX Agent running on the remote server'}
                </p>
              </div>

              {/* SSH-specific fields */}
              {formData.connection_type === 'ssh' && (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="grid gap-2">
                      <Label htmlFor="ssh_user">SSH User</Label>
                      <Input
                        id="ssh_user"
                        name="ssh_user"
                        placeholder="root"
                        value={formData.ssh_user}
                        onChange={handleInputChange}
                        required
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="ssh_port">SSH Port</Label>
                      <Input
                        id="ssh_port"
                        name="ssh_port"
                        type="number"
                        placeholder="22"
                        value={formData.ssh_port}
                        onChange={handleInputChange}
                        required
                      />
                    </div>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="ssh_key">SSH Key Path</Label>
                    <Input
                      id="ssh_key"
                      name="ssh_key"
                      placeholder="/home/backupx/.ssh/id_rsa"
                      value={formData.ssh_key}
                      onChange={handleInputChange}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Path to the SSH private key on the BackupX server
                    </p>
                  </div>
                </>
              )}

              {/* Agent-specific fields */}
              {formData.connection_type === 'agent' && (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="agent_port">Agent Port</Label>
                    <Input
                      id="agent_port"
                      name="agent_port"
                      type="number"
                      placeholder="8090"
                      value={formData.agent_port}
                      onChange={handleInputChange}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Port where BackupX Agent is listening
                    </p>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="agent_api_key">Agent API Key</Label>
                    <Input
                      id="agent_api_key"
                      name="agent_api_key"
                      type="password"
                      placeholder="Enter API key"
                      value={formData.agent_api_key}
                      onChange={handleInputChange}
                      required={!editingServer}
                    />
                    <p className="text-xs text-muted-foreground">
                      API key configured on the remote BackupX Agent
                    </p>
                  </div>
                </>
              )}

              <div className="grid gap-2">
                <Label htmlFor="status">Status</Label>
                <Select
                  value={formData.status}
                  onValueChange={(value: "active" | "inactive") =>
                    setFormData((prev) => ({ ...prev, status: value }))
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="active">Active</SelectItem>
                    <SelectItem value="inactive">Inactive</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Inactive servers are skipped in backup jobs
                </p>
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestConnection}
                disabled={isTestingConnection || !formData.host}
              >
                {isTestingConnection ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <TestTube className="h-4 w-4 mr-2" />
                )}
                Test Connection
              </Button>
              <Button type="submit" disabled={isSaving}>
                {isSaving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Dialog */}
      <Dialog open={isDeleteDialogOpen} onOpenChange={setIsDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Server</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{deletingServer?.name}"? This action
              cannot be undone. Backup jobs using this server will need to be updated.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setIsDeleteDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              <Trash2 className="h-4 w-4 mr-2" />
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
