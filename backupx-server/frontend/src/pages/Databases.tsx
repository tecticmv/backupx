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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import type { DatabaseConfig } from "@/types/database";
import type { Server } from "@/types/server";
import {
  Plus,
  Pencil,
  Trash2,
  Database,
  Loader2,
  TestTube,
  Server as ServerIcon,
} from "lucide-react";

interface DatabaseFormData {
  name: string;
  type: "mysql";
  host: string;
  port: number;
  username: string;
  password: string;
  databases: string;
  status: "active" | "inactive";
}

const initialFormData: DatabaseFormData = {
  name: "",
  type: "mysql",
  host: "localhost",
  port: 3306,
  username: "",
  password: "",
  databases: "*",
  status: "active",
};

export default function Databases() {
  const [configs, setConfigs] = useState<DatabaseConfig[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false);
  const [editingConfig, setEditingConfig] = useState<DatabaseConfig | null>(null);
  const [deletingConfig, setDeletingConfig] = useState<DatabaseConfig | null>(null);
  const [formData, setFormData] = useState<DatabaseFormData>(initialFormData);
  const [testServerId, setTestServerId] = useState<string>("");
  const [isSaving, setIsSaving] = useState(false);
  const [isTestingConnection, setIsTestingConnection] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchConfigs();
    fetchServers();
  }, []);

  const fetchConfigs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch("/api/databases");
      if (response.ok) {
        setConfigs(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch database configs:", err);
    } finally {
      setIsLoading(false);
    }
  };

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

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: name === "port" ? parseInt(value) || 3306 : value,
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);

    try {
      const url = editingConfig
        ? `/api/databases/${editingConfig.id}`
        : "/api/databases";
      const method = editingConfig ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save database configuration");
      }

      toast.success(
        editingConfig
          ? "Database configuration updated"
          : "Database configuration created"
      );
      setIsDialogOpen(false);
      setEditingConfig(null);
      setFormData(initialFormData);
      fetchConfigs();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSaving(false);
    }
  };

  const handleEdit = (config: DatabaseConfig) => {
    setEditingConfig(config);
    setFormData({
      name: config.name,
      type: config.type,
      host: config.host,
      port: config.port,
      username: config.username,
      password: "",
      databases: config.databases,
      status: config.status || "active",
    });
    setIsDialogOpen(true);
  };

  const handleDelete = async () => {
    if (!deletingConfig) return;

    try {
      const response = await fetch(`/api/databases/${deletingConfig.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete database configuration");

      toast.success("Database configuration deleted");
      setIsDeleteDialogOpen(false);
      setDeletingConfig(null);
      fetchConfigs();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestConnection = async () => {
    if (!testServerId) {
      toast.error("Please select a server to test the connection through");
      return;
    }

    setIsTestingConnection(true);
    try {
      const response = await fetch("/api/databases/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...formData,
          server_id: testServerId,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Connection test failed");
      }

      toast.success("Database connection successful!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Connection test failed");
    } finally {
      setIsTestingConnection(false);
    }
  };

  const openNewDialog = () => {
    setEditingConfig(null);
    setFormData(initialFormData);
    setTestServerId("");
    setIsDialogOpen(true);
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
            <CardTitle className="text-sm font-medium">Database Configs</CardTitle>
            <Database className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{configs.length}</div>
            <p className="text-xs text-muted-foreground">
              MySQL databases configured
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
              {new Set(configs.map((c) => c.host)).size}
            </div>
            <p className="text-xs text-muted-foreground">
              Different database servers
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Database Configs Table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Database Configurations</CardTitle>
            <CardDescription>
              Configure MySQL database connections for backup jobs
            </CardDescription>
          </div>
          <Button onClick={openNewDialog}>
            <Plus className="h-4 w-4 mr-2" />
            Add Database
          </Button>
        </CardHeader>
        <CardContent>
          {configs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Database className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No database configurations</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add your first MySQL database to get started
              </p>
              <Button onClick={openNewDialog}>
                <Plus className="h-4 w-4 mr-2" />
                Add Database
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>Port</TableHead>
                  <TableHead>Username</TableHead>
                  <TableHead>Databases</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {configs.map((config) => (
                  <TableRow key={config.id}>
                    <TableCell className="font-medium">{config.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.host}
                    </TableCell>
                    <TableCell>{config.port}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.username}
                    </TableCell>
                    <TableCell>
                      {config.databases === "*" ? (
                        <span className="text-muted-foreground">All databases</span>
                      ) : (
                        config.databases
                      )}
                    </TableCell>
                    <TableCell>
                      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                        config.status === "active"
                          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400"
                          : "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400"
                      }`}>
                        {config.status === "active" ? "Active" : "Inactive"}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleEdit(config)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setDeletingConfig(config);
                            setIsDeleteDialogOpen(true);
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

      {/* Database Dialog */}
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleSubmit}>
            <DialogHeader>
              <DialogTitle>
                {editingConfig ? "Edit Database" : "Add Database"}
              </DialogTitle>
              <DialogDescription>
                Configure your MySQL database connection for backups. The database
                will be accessed via SSH from a configured server.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  name="name"
                  placeholder="Production MySQL"
                  value={formData.name}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="host">Host</Label>
                  <Input
                    id="host"
                    name="host"
                    placeholder="localhost or db.example.com"
                    value={formData.host}
                    onChange={handleInputChange}
                    required
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="port">Port</Label>
                  <Input
                    id="port"
                    name="port"
                    type="number"
                    placeholder="3306"
                    value={formData.port}
                    onChange={handleInputChange}
                    required
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="username">Username</Label>
                  <Input
                    id="username"
                    name="username"
                    placeholder="backup_user"
                    value={formData.username}
                    onChange={handleInputChange}
                    required
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="password">
                    Password {editingConfig && "(leave blank to keep current)"}
                  </Label>
                  <Input
                    id="password"
                    name="password"
                    type="password"
                    placeholder="********"
                    value={formData.password}
                    onChange={handleInputChange}
                    required={!editingConfig}
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="databases">Databases to Backup</Label>
                  <Input
                    id="databases"
                    name="databases"
                    placeholder="* for all, or db1, db2"
                    value={formData.databases}
                    onChange={handleInputChange}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Use * for all, or comma-separated names
                  </p>
                </div>
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
                    Inactive databases are skipped
                  </p>
                </div>
              </div>

              {/* Test Connection Server Selection */}
              <div className="grid gap-2 pt-2 border-t">
                <Label>Test Connection via Server</Label>
                <Select value={testServerId} onValueChange={setTestServerId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select a server to test through" />
                  </SelectTrigger>
                  <SelectContent>
                    {servers.map((server) => (
                      <SelectItem key={server.id} value={server.id}>
                        {server.name} ({server.host}) - {server.connection_type === 'agent' ? 'Agent' : 'SSH'}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {testServerId && servers.find(s => s.id === testServerId)?.connection_type === 'agent'
                    ? 'MySQL connection will be tested via the agent on the selected server'
                    : 'MySQL connection will be tested via SSH from the selected server'}
                </p>
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestConnection}
                disabled={isTestingConnection || !formData.host || !formData.username || !testServerId}
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
            <DialogTitle>Delete Database Configuration</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{deletingConfig?.name}"? This action
              cannot be undone. Backup jobs using this configuration will need to be
              updated.
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
