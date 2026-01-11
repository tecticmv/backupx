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
import { toast } from "sonner";
import type { S3Config, S3ConfigFormData } from "@/types/s3";
import type { Server, ServerFormData } from "@/types/server";
import {
  Plus,
  Pencil,
  Trash2,
  CloudCog,
  Loader2,
  TestTube,
  Server as ServerIcon,
  Monitor,
} from "lucide-react";

const initialS3FormData: S3ConfigFormData = {
  name: "",
  endpoint: "",
  bucket: "",
  access_key: "",
  secret_key: "",
  region: "",
};

const initialServerFormData: ServerFormData = {
  name: "",
  host: "",
  ssh_port: 22,
  ssh_user: "root",
  ssh_key: "/root/.ssh/id_rsa",
};

export default function Settings() {
  // S3 State
  const [s3Configs, setS3Configs] = useState<S3Config[]>([]);
  const [isS3DialogOpen, setIsS3DialogOpen] = useState(false);
  const [isS3DeleteDialogOpen, setIsS3DeleteDialogOpen] = useState(false);
  const [editingS3Config, setEditingS3Config] = useState<S3Config | null>(null);
  const [deletingS3Config, setDeletingS3Config] = useState<S3Config | null>(null);
  const [s3FormData, setS3FormData] = useState<S3ConfigFormData>(initialS3FormData);
  const [isSavingS3, setIsSavingS3] = useState(false);
  const [isTestingS3Connection, setIsTestingS3Connection] = useState(false);

  // Server State
  const [servers, setServers] = useState<Server[]>([]);
  const [isServerDialogOpen, setIsServerDialogOpen] = useState(false);
  const [isServerDeleteDialogOpen, setIsServerDeleteDialogOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<Server | null>(null);
  const [deletingServer, setDeletingServer] = useState<Server | null>(null);
  const [serverFormData, setServerFormData] = useState<ServerFormData>(initialServerFormData);
  const [isSavingServer, setIsSavingServer] = useState(false);
  const [isTestingServerConnection, setIsTestingServerConnection] = useState(false);

  // Loading State
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchAll = async () => {
    setIsLoading(true);
    await Promise.all([fetchS3Configs(), fetchServers()]);
    setIsLoading(false);
  };

  // S3 Functions
  const fetchS3Configs = async () => {
    try {
      const response = await fetch("/api/s3-configs");
      if (response.ok) {
        setS3Configs(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch S3 configs:", err);
    }
  };

  const handleS3InputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setS3FormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleS3Submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingS3(true);

    try {
      const url = editingS3Config
        ? `/api/s3-configs/${editingS3Config.id}`
        : "/api/s3-configs";
      const method = editingS3Config ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(s3FormData),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save S3 configuration");
      }

      toast.success(
        editingS3Config
          ? "S3 configuration updated"
          : "S3 configuration created"
      );
      setIsS3DialogOpen(false);
      setEditingS3Config(null);
      setS3FormData(initialS3FormData);
      fetchS3Configs();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSavingS3(false);
    }
  };

  const handleS3Edit = (config: S3Config) => {
    setEditingS3Config(config);
    setS3FormData({
      name: config.name,
      endpoint: config.endpoint,
      bucket: config.bucket,
      access_key: config.access_key,
      secret_key: "",
      region: config.region || "",
    });
    setIsS3DialogOpen(true);
  };

  const handleS3Delete = async () => {
    if (!deletingS3Config) return;

    try {
      const response = await fetch(`/api/s3-configs/${deletingS3Config.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete S3 configuration");

      toast.success("S3 configuration deleted");
      setIsS3DeleteDialogOpen(false);
      setDeletingS3Config(null);
      fetchS3Configs();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestS3Connection = async () => {
    setIsTestingS3Connection(true);
    try {
      const response = await fetch("/api/s3-configs/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(s3FormData),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Connection test failed");
      }

      toast.success("S3 connection successful!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Connection test failed");
    } finally {
      setIsTestingS3Connection(false);
    }
  };

  const openNewS3Dialog = () => {
    setEditingS3Config(null);
    setS3FormData(initialS3FormData);
    setIsS3DialogOpen(true);
  };

  // Server Functions
  const fetchServers = async () => {
    try {
      const response = await fetch("/api/servers");
      if (response.ok) {
        setServers(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch servers:", err);
    }
  };

  const handleServerInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type } = e.target;
    setServerFormData((prev) => ({
      ...prev,
      [name]: type === "number" ? parseInt(value) || 0 : value,
    }));
  };

  const handleServerSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingServer(true);

    try {
      const url = editingServer
        ? `/api/servers/${editingServer.id}`
        : "/api/servers";
      const method = editingServer ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(serverFormData),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save server");
      }

      toast.success(
        editingServer ? "Server updated" : "Server created"
      );
      setIsServerDialogOpen(false);
      setEditingServer(null);
      setServerFormData(initialServerFormData);
      fetchServers();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSavingServer(false);
    }
  };

  const handleServerEdit = (server: Server) => {
    setEditingServer(server);
    setServerFormData({
      name: server.name,
      host: server.host,
      ssh_port: server.ssh_port,
      ssh_user: server.ssh_user,
      ssh_key: server.ssh_key,
    });
    setIsServerDialogOpen(true);
  };

  const handleServerDelete = async () => {
    if (!deletingServer) return;

    try {
      const response = await fetch(`/api/servers/${deletingServer.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete server");

      toast.success("Server deleted");
      setIsServerDeleteDialogOpen(false);
      setDeletingServer(null);
      fetchServers();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestServerConnection = async () => {
    setIsTestingServerConnection(true);
    try {
      const response = await fetch("/api/servers/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(serverFormData),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Connection test failed");
      }

      toast.success("SSH connection successful!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Connection test failed");
    } finally {
      setIsTestingServerConnection(false);
    }
  };

  const openNewServerDialog = () => {
    setEditingServer(null);
    setServerFormData(initialServerFormData);
    setIsServerDialogOpen(true);
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
      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Remote Servers</CardTitle>
            <Monitor className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{servers.length}</div>
            <p className="text-xs text-muted-foreground">
              Servers configured
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">S3 Storage</CardTitle>
            <CloudCog className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{s3Configs.length}</div>
            <p className="text-xs text-muted-foreground">
              Storage endpoints
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
              Different hosts
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">S3 Endpoints</CardTitle>
            <CloudCog className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {new Set(s3Configs.map((c) => c.endpoint)).size}
            </div>
            <p className="text-xs text-muted-foreground">
              Unique providers
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Servers Section */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Remote Servers</CardTitle>
            <CardDescription>
              Configure remote servers to backup via SSH
            </CardDescription>
          </div>
          <Button onClick={openNewServerDialog}>
            <Plus className="h-4 w-4 mr-2" />
            Add Server
          </Button>
        </CardHeader>
        <CardContent>
          {servers.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Monitor className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No servers configured</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add your first remote server to get started
              </p>
              <Button onClick={openNewServerDialog}>
                <Plus className="h-4 w-4 mr-2" />
                Add Server
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>SSH User</TableHead>
                  <TableHead>Port</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {servers.map((server) => (
                  <TableRow key={server.id}>
                    <TableCell className="font-medium">{server.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {server.host}
                    </TableCell>
                    <TableCell>{server.ssh_user}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {server.ssh_port}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleServerEdit(server)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setDeletingServer(server);
                            setIsServerDeleteDialogOpen(true);
                          }}
                          className="text-destructive hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* S3 Storage Section */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>S3 Storage Configurations</CardTitle>
            <CardDescription>
              Configure S3-compatible storage endpoints for your backups
            </CardDescription>
          </div>
          <Button onClick={openNewS3Dialog}>
            <Plus className="h-4 w-4 mr-2" />
            Add Configuration
          </Button>
        </CardHeader>
        <CardContent>
          {s3Configs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <CloudCog className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No S3 configurations</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add your first S3 configuration to get started
              </p>
              <Button onClick={openNewS3Dialog}>
                <Plus className="h-4 w-4 mr-2" />
                Add Configuration
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Endpoint</TableHead>
                  <TableHead>Bucket</TableHead>
                  <TableHead>Region</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {s3Configs.map((config) => (
                  <TableRow key={config.id}>
                    <TableCell className="font-medium">{config.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.endpoint}
                    </TableCell>
                    <TableCell>{config.bucket}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.region || "-"}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleS3Edit(config)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setDeletingS3Config(config);
                            setIsS3DeleteDialogOpen(true);
                          }}
                          className="text-destructive hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Server Dialog */}
      <Dialog open={isServerDialogOpen} onOpenChange={setIsServerDialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleServerSubmit}>
            <DialogHeader>
              <DialogTitle>
                {editingServer ? "Edit Server" : "Add Server"}
              </DialogTitle>
              <DialogDescription>
                Configure a remote server for SSH-based backups.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="server-name">Name</Label>
                <Input
                  id="server-name"
                  name="name"
                  placeholder="Production Server"
                  value={serverFormData.name}
                  onChange={handleServerInputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="server-host">Host</Label>
                <Input
                  id="server-host"
                  name="host"
                  placeholder="192.168.1.100 or server.example.com"
                  value={serverFormData.host}
                  onChange={handleServerInputChange}
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="server-ssh_user">SSH User</Label>
                  <Input
                    id="server-ssh_user"
                    name="ssh_user"
                    placeholder="root"
                    value={serverFormData.ssh_user}
                    onChange={handleServerInputChange}
                    required
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="server-ssh_port">SSH Port</Label>
                  <Input
                    id="server-ssh_port"
                    name="ssh_port"
                    type="number"
                    placeholder="22"
                    value={serverFormData.ssh_port}
                    onChange={handleServerInputChange}
                    required
                  />
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="server-ssh_key">SSH Key Path</Label>
                <Input
                  id="server-ssh_key"
                  name="ssh_key"
                  placeholder="/root/.ssh/id_rsa"
                  value={serverFormData.ssh_key}
                  onChange={handleServerInputChange}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Path to the SSH private key on the backup server
                </p>
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestServerConnection}
                disabled={isTestingServerConnection || !serverFormData.host}
              >
                {isTestingServerConnection ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <TestTube className="h-4 w-4 mr-2" />
                )}
                Test Connection
              </Button>
              <Button type="submit" disabled={isSavingServer}>
                {isSavingServer && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* S3 Dialog */}
      <Dialog open={isS3DialogOpen} onOpenChange={setIsS3DialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleS3Submit}>
            <DialogHeader>
              <DialogTitle>
                {editingS3Config ? "Edit S3 Configuration" : "Add S3 Configuration"}
              </DialogTitle>
              <DialogDescription>
                Configure your S3-compatible storage endpoint. Supports AWS S3,
                MinIO, DigitalOcean Spaces, and other compatible services.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="s3-name">Name</Label>
                <Input
                  id="s3-name"
                  name="name"
                  placeholder="My S3 Storage"
                  value={s3FormData.name}
                  onChange={handleS3InputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="s3-endpoint">Endpoint</Label>
                <Input
                  id="s3-endpoint"
                  name="endpoint"
                  placeholder="s3.amazonaws.com or minio.example.com:9000"
                  value={s3FormData.endpoint}
                  onChange={handleS3InputChange}
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="s3-bucket">Bucket</Label>
                  <Input
                    id="s3-bucket"
                    name="bucket"
                    placeholder="my-backup-bucket"
                    value={s3FormData.bucket}
                    onChange={handleS3InputChange}
                    required
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="s3-region">Region (optional)</Label>
                  <Input
                    id="s3-region"
                    name="region"
                    placeholder="us-east-1"
                    value={s3FormData.region}
                    onChange={handleS3InputChange}
                  />
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="s3-access_key">Access Key</Label>
                <Input
                  id="s3-access_key"
                  name="access_key"
                  placeholder="AKIAIOSFODNN7EXAMPLE"
                  value={s3FormData.access_key}
                  onChange={handleS3InputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="s3-secret_key">
                  Secret Key {editingS3Config && "(leave blank to keep current)"}
                </Label>
                <Input
                  id="s3-secret_key"
                  name="secret_key"
                  type="password"
                  placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                  value={s3FormData.secret_key}
                  onChange={handleS3InputChange}
                  required={!editingS3Config}
                />
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestS3Connection}
                disabled={isTestingS3Connection || !s3FormData.endpoint || !s3FormData.bucket}
              >
                {isTestingS3Connection ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <TestTube className="h-4 w-4 mr-2" />
                )}
                Test Connection
              </Button>
              <Button type="submit" disabled={isSavingS3}>
                {isSavingS3 && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Server Delete Dialog */}
      <Dialog open={isServerDeleteDialogOpen} onOpenChange={setIsServerDeleteDialogOpen}>
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
              onClick={() => setIsServerDeleteDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleServerDelete}>
              <Trash2 className="h-4 w-4 mr-2" />
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* S3 Delete Dialog */}
      <Dialog open={isS3DeleteDialogOpen} onOpenChange={setIsS3DeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete S3 Configuration</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{deletingS3Config?.name}"? This action
              cannot be undone. Backup jobs using this configuration will need to be
              updated.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setIsS3DeleteDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleS3Delete}>
              <Trash2 className="h-4 w-4 mr-2" />
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
