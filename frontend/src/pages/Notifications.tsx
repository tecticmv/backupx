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
import { Badge } from "@/components/ui/badge";
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
import { toast } from "sonner";
import type {
  NotificationChannel,
  NotificationChannelType,
  EmailConfig,
  SlackConfig,
  DiscordConfig,
  TelegramConfig,
  WebhookConfig,
} from "@/types/notification";
import {
  Plus,
  Pencil,
  Trash2,
  Bell,
  Loader2,
  TestTube,
  Mail,
  MessageSquare,
  Webhook,
  CheckCircle2,
  XCircle,
  Send,
} from "lucide-react";

interface NotificationFormData {
  name: string;
  type: NotificationChannelType;
  enabled: boolean;
  notify_on_success: boolean;
  notify_on_failure: boolean;
  // Email config
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_password: string;
  smtp_tls: boolean;
  from_address: string;
  to_addresses: string;
  // Slack/Discord/Webhook config
  webhook_url: string;
  // Telegram config
  telegram_bot_token: string;
  telegram_chat_id: string;
  // Generic webhook config
  webhook_method: "GET" | "POST" | "PUT";
}

const initialFormData: NotificationFormData = {
  name: "",
  type: "email",
  enabled: true,
  notify_on_success: true,
  notify_on_failure: true,
  smtp_host: "",
  smtp_port: 587,
  smtp_user: "",
  smtp_password: "",
  smtp_tls: true,
  from_address: "",
  to_addresses: "",
  webhook_url: "",
  telegram_bot_token: "",
  telegram_chat_id: "",
  webhook_method: "POST",
};

export default function Notifications() {
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false);
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null);
  const [deletingChannel, setDeletingChannel] = useState<NotificationChannel | null>(null);
  const [formData, setFormData] = useState<NotificationFormData>(initialFormData);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchChannels();
  }, []);

  const fetchChannels = async () => {
    try {
      const response = await fetch("/api/notifications");
      if (response.ok) {
        setChannels(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch notification channels:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === "number" ? parseInt(value) || 0 : value,
    }));
  };

  const handleSwitchChange = (name: keyof NotificationFormData, checked: boolean) => {
    setFormData((prev) => ({ ...prev, [name]: checked }));
  };

  const handleTypeChange = (value: NotificationChannelType) => {
    setFormData((prev) => ({ ...prev, type: value }));
  };

  const buildConfig = (): EmailConfig | SlackConfig | DiscordConfig | TelegramConfig | WebhookConfig => {
    switch (formData.type) {
      case "email":
        return {
          smtp_host: formData.smtp_host,
          smtp_port: formData.smtp_port,
          smtp_user: formData.smtp_user,
          smtp_password: formData.smtp_password,
          smtp_tls: formData.smtp_tls,
          from_address: formData.from_address,
          to_addresses: formData.to_addresses,
        };
      case "slack":
      case "discord":
        return {
          webhook_url: formData.webhook_url,
        };
      case "telegram":
        return {
          bot_token: formData.telegram_bot_token,
          chat_id: formData.telegram_chat_id,
        };
      case "webhook":
        return {
          url: formData.webhook_url,
          method: formData.webhook_method,
          headers: {},
        };
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);

    try {
      const payload = {
        name: formData.name,
        type: formData.type,
        enabled: formData.enabled,
        notify_on_success: formData.notify_on_success,
        notify_on_failure: formData.notify_on_failure,
        config: buildConfig(),
      };

      const url = editingChannel
        ? `/api/notifications/${editingChannel.id}`
        : "/api/notifications";
      const method = editingChannel ? "PUT" : "POST";

      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save notification channel");
      }

      toast.success(
        editingChannel
          ? "Notification channel updated"
          : "Notification channel created"
      );
      setIsDialogOpen(false);
      setEditingChannel(null);
      setFormData(initialFormData);
      fetchChannels();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSaving(false);
    }
  };

  const handleEdit = (channel: NotificationChannel) => {
    setEditingChannel(channel);
    const config = channel.config;

    const newFormData: NotificationFormData = {
      ...initialFormData,
      name: channel.name,
      type: channel.type,
      enabled: channel.enabled,
      notify_on_success: channel.notify_on_success,
      notify_on_failure: channel.notify_on_failure,
    };

    if (channel.type === "email") {
      const emailConfig = config as EmailConfig;
      newFormData.smtp_host = emailConfig.smtp_host || "";
      newFormData.smtp_port = emailConfig.smtp_port || 587;
      newFormData.smtp_user = emailConfig.smtp_user || "";
      newFormData.smtp_password = emailConfig.smtp_password || "";
      newFormData.smtp_tls = emailConfig.smtp_tls !== false;
      newFormData.from_address = emailConfig.from_address || "";
      newFormData.to_addresses = emailConfig.to_addresses || "";
    } else if (channel.type === "slack" || channel.type === "discord") {
      const webhookConfig = config as SlackConfig | DiscordConfig;
      newFormData.webhook_url = webhookConfig.webhook_url || "";
    } else if (channel.type === "telegram") {
      const telegramConfig = config as TelegramConfig;
      newFormData.telegram_bot_token = telegramConfig.bot_token || "";
      newFormData.telegram_chat_id = telegramConfig.chat_id || "";
    } else if (channel.type === "webhook") {
      const webhookConfig = config as WebhookConfig;
      newFormData.webhook_url = webhookConfig.url || "";
      newFormData.webhook_method = webhookConfig.method || "POST";
    }

    setFormData(newFormData);
    setIsDialogOpen(true);
  };

  const handleDelete = async () => {
    if (!deletingChannel) return;

    try {
      const response = await fetch(`/api/notifications/${deletingChannel.id}`, {
        method: "DELETE",
      });

      if (!response.ok) throw new Error("Failed to delete notification channel");

      toast.success("Notification channel deleted");
      setIsDeleteDialogOpen(false);
      setDeletingChannel(null);
      fetchChannels();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    }
  };

  const handleTestNotification = async () => {
    setIsTesting(true);
    try {
      const response = await fetch("/api/notifications/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: formData.type,
          config: buildConfig(),
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Test failed");
      }

      toast.success("Test notification sent successfully!");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Test failed");
    } finally {
      setIsTesting(false);
    }
  };

  const openNewDialog = () => {
    setEditingChannel(null);
    setFormData(initialFormData);
    setIsDialogOpen(true);
  };

  const getTypeIcon = (type: NotificationChannelType) => {
    switch (type) {
      case "email":
        return <Mail className="h-4 w-4" />;
      case "slack":
      case "discord":
        return <MessageSquare className="h-4 w-4" />;
      case "telegram":
        return <Send className="h-4 w-4" />;
      case "webhook":
        return <Webhook className="h-4 w-4" />;
    }
  };

  const getTypeBadge = (type: NotificationChannelType) => {
    const labels: Record<NotificationChannelType, string> = {
      email: "Email",
      slack: "Slack",
      discord: "Discord",
      telegram: "Telegram",
      webhook: "Webhook",
    };
    return (
      <Badge variant="outline" className="capitalize">
        {getTypeIcon(type)}
        <span className="ml-1">{labels[type]}</span>
      </Badge>
    );
  };

  const enabledCount = channels.filter((c) => c.enabled).length;

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
            <CardTitle className="text-sm font-medium">Total Channels</CardTitle>
            <Bell className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{channels.length}</div>
            <p className="text-xs text-muted-foreground">
              Notification channels configured
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Active</CardTitle>
            <CheckCircle2 className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-emerald-500">{enabledCount}</div>
            <p className="text-xs text-muted-foreground">
              Channels sending notifications
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Disabled</CardTitle>
            <XCircle className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{channels.length - enabledCount}</div>
            <p className="text-xs text-muted-foreground">
              Channels paused
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Channel Types</CardTitle>
            <Webhook className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {new Set(channels.map((c) => c.type)).size}
            </div>
            <p className="text-xs text-muted-foreground">
              Different types in use
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Channels Table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Notification Channels</CardTitle>
            <CardDescription>
              Configure channels to receive alerts when backup jobs complete
            </CardDescription>
          </div>
          <Button onClick={openNewDialog}>
            <Plus className="h-4 w-4 mr-2" />
            Add Channel
          </Button>
        </CardHeader>
        <CardContent>
          {channels.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Bell className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No notification channels</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add a channel to receive backup alerts
              </p>
              <Button onClick={openNewDialog}>
                <Plus className="h-4 w-4 mr-2" />
                Add Channel
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Success</TableHead>
                  <TableHead>Failure</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {channels.map((channel) => (
                  <TableRow key={channel.id}>
                    <TableCell className="font-medium">{channel.name}</TableCell>
                    <TableCell>{getTypeBadge(channel.type)}</TableCell>
                    <TableCell>
                      <Badge
                        variant={channel.enabled ? "default" : "secondary"}
                        className={channel.enabled ? "bg-emerald-600 hover:bg-emerald-600" : ""}
                      >
                        {channel.enabled ? "Active" : "Disabled"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {channel.notify_on_success ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                      ) : (
                        <XCircle className="h-4 w-4 text-muted-foreground" />
                      )}
                    </TableCell>
                    <TableCell>
                      {channel.notify_on_failure ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                      ) : (
                        <XCircle className="h-4 w-4 text-muted-foreground" />
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleEdit(channel)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => {
                            setDeletingChannel(channel);
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

      {/* Add/Edit Dialog */}
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="sm:max-w-[550px] max-h-[90vh] overflow-y-auto">
          <form onSubmit={handleSubmit}>
            <DialogHeader>
              <DialogTitle>
                {editingChannel ? "Edit Notification Channel" : "Add Notification Channel"}
              </DialogTitle>
              <DialogDescription>
                Configure a channel to receive backup notifications.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  name="name"
                  placeholder="My Notification Channel"
                  value={formData.name}
                  onChange={handleInputChange}
                  required
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="type">Channel Type</Label>
                <Select
                  value={formData.type}
                  onValueChange={(v) => handleTypeChange(v as NotificationChannelType)}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="email">
                      <div className="flex items-center gap-2">
                        <Mail className="h-4 w-4" />
                        Email (SMTP)
                      </div>
                    </SelectItem>
                    <SelectItem value="slack">
                      <div className="flex items-center gap-2">
                        <MessageSquare className="h-4 w-4" />
                        Slack
                      </div>
                    </SelectItem>
                    <SelectItem value="discord">
                      <div className="flex items-center gap-2">
                        <MessageSquare className="h-4 w-4" />
                        Discord
                      </div>
                    </SelectItem>
                    <SelectItem value="telegram">
                      <div className="flex items-center gap-2">
                        <Send className="h-4 w-4" />
                        Telegram
                      </div>
                    </SelectItem>
                    <SelectItem value="webhook">
                      <div className="flex items-center gap-2">
                        <Webhook className="h-4 w-4" />
                        Generic Webhook
                      </div>
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Email Config */}
              {formData.type === "email" && (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="grid gap-2">
                      <Label htmlFor="smtp_host">SMTP Host</Label>
                      <Input
                        id="smtp_host"
                        name="smtp_host"
                        placeholder="smtp.gmail.com"
                        value={formData.smtp_host}
                        onChange={handleInputChange}
                        required
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="smtp_port">SMTP Port</Label>
                      <Input
                        id="smtp_port"
                        name="smtp_port"
                        type="number"
                        placeholder="587"
                        value={formData.smtp_port}
                        onChange={handleInputChange}
                        required
                      />
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="grid gap-2">
                      <Label htmlFor="smtp_user">SMTP Username</Label>
                      <Input
                        id="smtp_user"
                        name="smtp_user"
                        placeholder="user@example.com"
                        value={formData.smtp_user}
                        onChange={handleInputChange}
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="smtp_password">
                        SMTP Password {editingChannel && "(leave blank to keep)"}
                      </Label>
                      <Input
                        id="smtp_password"
                        name="smtp_password"
                        type="password"
                        placeholder="********"
                        value={formData.smtp_password}
                        onChange={handleInputChange}
                      />
                    </div>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="from_address">From Address</Label>
                    <Input
                      id="from_address"
                      name="from_address"
                      placeholder="backups@example.com"
                      value={formData.from_address}
                      onChange={handleInputChange}
                      required
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="to_addresses">To Addresses</Label>
                    <Input
                      id="to_addresses"
                      name="to_addresses"
                      placeholder="admin@example.com, team@example.com"
                      value={formData.to_addresses}
                      onChange={handleInputChange}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Comma-separated list of email addresses
                    </p>
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label>Use TLS</Label>
                      <p className="text-xs text-muted-foreground">
                        Enable TLS encryption for SMTP
                      </p>
                    </div>
                    <Switch
                      checked={formData.smtp_tls}
                      onCheckedChange={(checked) => handleSwitchChange("smtp_tls", checked)}
                    />
                  </div>
                </>
              )}

              {/* Slack/Discord Config */}
              {(formData.type === "slack" || formData.type === "discord") && (
                <div className="grid gap-2">
                  <Label htmlFor="webhook_url">Webhook URL</Label>
                  <Input
                    id="webhook_url"
                    name="webhook_url"
                    placeholder={
                      formData.type === "slack"
                        ? "https://hooks.slack.com/services/..."
                        : "https://discord.com/api/webhooks/..."
                    }
                    value={formData.webhook_url}
                    onChange={handleInputChange}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    {formData.type === "slack"
                      ? "Create an incoming webhook in your Slack workspace settings"
                      : "Create a webhook in your Discord server settings (Server Settings > Integrations > Webhooks)"}
                  </p>
                </div>
              )}

              {/* Telegram Config */}
              {formData.type === "telegram" && (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="telegram_bot_token">Bot Token</Label>
                    <Input
                      id="telegram_bot_token"
                      name="telegram_bot_token"
                      placeholder="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
                      value={formData.telegram_bot_token}
                      onChange={handleInputChange}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Create a bot via @BotFather and get the token
                    </p>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="telegram_chat_id">Chat ID</Label>
                    <Input
                      id="telegram_chat_id"
                      name="telegram_chat_id"
                      placeholder="-1001234567890"
                      value={formData.telegram_chat_id}
                      onChange={handleInputChange}
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Your user ID, group ID, or channel ID (use @userinfobot to find your ID)
                    </p>
                  </div>
                </>
              )}

              {/* Generic Webhook Config */}
              {formData.type === "webhook" && (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="webhook_url">Webhook URL</Label>
                    <Input
                      id="webhook_url"
                      name="webhook_url"
                      placeholder="https://api.example.com/webhooks/backup"
                      value={formData.webhook_url}
                      onChange={handleInputChange}
                      required
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="webhook_method">HTTP Method</Label>
                    <Select
                      value={formData.webhook_method}
                      onValueChange={(v) =>
                        setFormData((prev) => ({ ...prev, webhook_method: v as "GET" | "POST" | "PUT" }))
                      }
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="POST">POST</SelectItem>
                        <SelectItem value="PUT">PUT</SelectItem>
                        <SelectItem value="GET">GET</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )}

              <div className="border-t pt-4 mt-2">
                <h4 className="font-medium mb-3">Notification Settings</h4>
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label>Enabled</Label>
                      <p className="text-xs text-muted-foreground">
                        Send notifications through this channel
                      </p>
                    </div>
                    <Switch
                      checked={formData.enabled}
                      onCheckedChange={(checked) => handleSwitchChange("enabled", checked)}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label>Notify on Success</Label>
                      <p className="text-xs text-muted-foreground">
                        Send notification when backup succeeds
                      </p>
                    </div>
                    <Switch
                      checked={formData.notify_on_success}
                      onCheckedChange={(checked) =>
                        handleSwitchChange("notify_on_success", checked)
                      }
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label>Notify on Failure</Label>
                      <p className="text-xs text-muted-foreground">
                        Send notification when backup fails
                      </p>
                    </div>
                    <Switch
                      checked={formData.notify_on_failure}
                      onCheckedChange={(checked) =>
                        handleSwitchChange("notify_on_failure", checked)
                      }
                    />
                  </div>
                </div>
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleTestNotification}
                disabled={isTesting || !formData.name}
              >
                {isTesting ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <TestTube className="h-4 w-4 mr-2" />
                )}
                Test
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
            <DialogTitle>Delete Notification Channel</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{deletingChannel?.name}"? This action
              cannot be undone. You will no longer receive notifications through this channel.
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
