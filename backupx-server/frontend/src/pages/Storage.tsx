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
import { Switch } from "@/components/ui/switch";
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
import {
  Plus,
  Pencil,
  Trash2,
  CloudCog,
  Loader2,
  TestTube,
  HardDrive,
} from "lucide-react";

const initialFormData: S3ConfigFormData = {
  name: "",
  endpoint: "",
  bucket: "",
  access_key: "",
  secret_key: "",
  region: "",
  skip_ssl_verify: false,
};

export default function Storage() {
  const [configs, setConfigs] = useState<S3Config[]>([]);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false);
  const [editingConfig, setEditingConfig] = useState<S3Config | null>(null);
  const [deletingConfig, setDeletingConfig] = useState<S3Config | null>(null);
  const [formData, setFormData] = useState<S3ConfigFormData>(initialFormData);
  const [isSaving, setIsSaving] = useState(false);
  const [isTestingConnection, setIsTestingConnection] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchConfigs();
  }, []);

  const fetchConfigs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch("/api/s3-configs");
      if (response.ok) {
        setConfigs(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch S3 configs:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);

    try {
      const url = editingConfig
        ? `/api/s3-configs/${editingConfig.id}`
        : "/api/s3-configs";
      const method = editingConfig ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save S3 configuration");
      }

      toast.success(
        editingConfig
          ? "S3 configuration updated"
          : "S3 configuration created"
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

  const handleEdit = (config: S3Config) => {
    setEditingConfig(config);
    setFormData({
      name: config.name,
      endpoint: config.endpoint,
      bucket: config.bucket,
      access_key: config.access_key,
      secret_key: "",
      region: config.region || "",
      skip_ssl_verify: config.skip_ssl_verify || false,
    });
    setIsDialogOpen(true);
  };

  const handleDelete = async () => {
    if (!deletingConfig) return;

    try {
      const response = await fetch(`/api/s3-configs/${deletingConfig.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete S3 configuration");

      toast.success("S3 configuration deleted");
      setIsDeleteDialogOpen(false);
      setDeletingConfig(null);
      fetchConfigs();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestConnection = async () => {
    setIsTestingConnection(true);
    try {
      const response = await fetch("/api/s3-configs/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Connection test failed");
      }

      toast.success("S3 connection successful!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Connection test failed");
    } finally {
      setIsTestingConnection(false);
    }
  };

  const openNewDialog = () => {
    setEditingConfig(null);
    setFormData(initialFormData);
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
            <CardTitle className="text-sm font-medium">Storage Configs</CardTitle>
            <HardDrive className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{configs.length}</div>
            <p className="text-xs text-muted-foreground">
              S3 endpoints configured
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Unique Endpoints</CardTitle>
            <CloudCog className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {new Set(configs.map((c) => c.endpoint)).size}
            </div>
            <p className="text-xs text-muted-foreground">
              Different providers
            </p>
          </CardContent>
        </Card>
      </div>

      {/* S3 Configs Table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>S3 Storage Configurations</CardTitle>
            <CardDescription>
              Configure S3-compatible storage endpoints for your backups
            </CardDescription>
          </div>
          <Button onClick={openNewDialog}>
            <Plus className="h-4 w-4 mr-2" />
            Add Configuration
          </Button>
        </CardHeader>
        <CardContent>
          {configs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <CloudCog className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No S3 configurations</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add your first S3 configuration to get started
              </p>
              <Button onClick={openNewDialog}>
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
                {configs.map((config) => (
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

      {/* S3 Dialog */}
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleSubmit}>
            <DialogHeader>
              <DialogTitle>
                {editingConfig ? "Edit S3 Configuration" : "Add S3 Configuration"}
              </DialogTitle>
              <DialogDescription>
                Configure your S3-compatible storage endpoint. Supports AWS S3,
                MinIO, DigitalOcean Spaces, and other compatible services.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  name="name"
                  placeholder="My S3 Storage"
                  value={formData.name}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="endpoint">Endpoint</Label>
                <Input
                  id="endpoint"
                  name="endpoint"
                  placeholder="s3.amazonaws.com or minio.example.com:9000"
                  value={formData.endpoint}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="bucket">Bucket</Label>
                  <Input
                    id="bucket"
                    name="bucket"
                    placeholder="my-backup-bucket"
                    value={formData.bucket}
                    onChange={handleInputChange}
                    required
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="region">Region (optional)</Label>
                  <Input
                    id="region"
                    name="region"
                    placeholder="us-east-1"
                    value={formData.region}
                    onChange={handleInputChange}
                  />
                </div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="access_key">Access Key</Label>
                <Input
                  id="access_key"
                  name="access_key"
                  placeholder="AKIAIOSFODNN7EXAMPLE"
                  value={formData.access_key}
                  onChange={handleInputChange}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="secret_key">
                  Secret Key {editingConfig && "(leave blank to keep current)"}
                </Label>
                <Input
                  id="secret_key"
                  name="secret_key"
                  type="password"
                  placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                  value={formData.secret_key}
                  onChange={handleInputChange}
                  required={!editingConfig}
                />
              </div>
              <div className="flex items-center justify-between rounded-lg border p-3">
                <div className="space-y-0.5">
                  <Label htmlFor="skip_ssl_verify">Skip SSL Verification</Label>
                  <p className="text-xs text-muted-foreground">
                    Enable for self-signed certificates (less secure)
                  </p>
                </div>
                <Switch
                  id="skip_ssl_verify"
                  checked={formData.skip_ssl_verify || false}
                  onCheckedChange={(checked) =>
                    setFormData((prev) => ({ ...prev, skip_ssl_verify: checked }))
                  }
                />
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestConnection}
                disabled={isTestingConnection || !formData.endpoint || !formData.bucket}
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
            <DialogTitle>Delete S3 Configuration</DialogTitle>
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
