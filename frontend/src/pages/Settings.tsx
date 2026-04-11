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
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import {
  Loader2,
  User,
  Palette,
  Globe,
  Sun,
  Moon,
  Monitor,
  Key,
  Check,
  Settings as SettingsIcon,
} from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function Settings() {
  const { user } = useAuth();
  const { theme, setTheme } = useTheme();

  // Loading State
  const [isLoading, setIsLoading] = useState(true);

  // Timezone State
  const [timezone, setTimezone] = useState("UTC");
  const [availableTimezones, setAvailableTimezones] = useState<string[]>([]);
  const [isSavingTimezone, setIsSavingTimezone] = useState(false);

  // Password State
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isChangingPassword, setIsChangingPassword] = useState(false);

  useEffect(() => {
    fetchSettings();
  }, []);

  const fetchSettings = async () => {
    setIsLoading(true);
    await fetchTimezone();
    setIsLoading(false);
  };

  // Timezone Functions
  const fetchTimezone = async () => {
    try {
      const response = await fetch("/api/settings/timezone");
      if (response.ok) {
        const data = await response.json();
        setTimezone(data.timezone);
        setAvailableTimezones(data.available_timezones || []);
      }
    } catch (err) {
      console.error("Failed to fetch timezone:", err);
    }
  };

  const handleTimezoneChange = async (newTimezone: string) => {
    setIsSavingTimezone(true);
    try {
      const response = await fetch("/api/settings/timezone", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timezone: newTimezone }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || "Failed to save timezone");
      }

      setTimezone(newTimezone);
      toast.success("Timezone updated successfully");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSavingTimezone(false);
    }
  };

  // Password Change
  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault();

    if (newPassword !== confirmPassword) {
      toast.error("New passwords do not match");
      return;
    }

    if (newPassword.length < 8) {
      toast.error("Password must be at least 8 characters");
      return;
    }

    setIsChangingPassword(true);
    try {
      const response = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Failed to change password");
      }

      toast.success("Password changed successfully");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsChangingPassword(false);
    }
  };

  const themeOptions = [
    { value: "light", label: "Light", icon: Sun },
    { value: "dark", label: "Dark", icon: Moon },
    { value: "system", label: "System", icon: Monitor },
  ];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-3xl">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <SettingsIcon className="h-6 w-6" />
          Settings
        </h1>
        <p className="text-muted-foreground mt-1">
          Manage your account and application preferences
        </p>
      </div>

      {/* Account Settings */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <User className="h-5 w-5" />
            Account
          </CardTitle>
          <CardDescription>
            Manage your account settings and password
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* User Info */}
          <div className="flex items-center gap-4">
            <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
              <span className="text-2xl font-bold text-primary">
                {user?.username?.charAt(0).toUpperCase() || "A"}
              </span>
            </div>
            <div>
              <p className="font-medium text-lg">{user?.username || "Admin"}</p>
              <p className="text-sm text-muted-foreground">Administrator</p>
            </div>
          </div>

          <Separator />

          {/* Password Change */}
          <div>
            <h3 className="font-medium flex items-center gap-2 mb-4">
              <Key className="h-4 w-4" />
              Change Password
            </h3>
            <form onSubmit={handlePasswordChange} className="space-y-4">
              <div className="grid gap-2">
                <Label htmlFor="current-password">Current Password</Label>
                <Input
                  id="current-password"
                  type="password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  placeholder="Enter current password"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="new-password">New Password</Label>
                  <Input
                    id="new-password"
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="Enter new password"
                    required
                    minLength={8}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="confirm-password">Confirm Password</Label>
                  <Input
                    id="confirm-password"
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Confirm new password"
                    required
                    minLength={8}
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Password must be at least 8 characters long
              </p>
              <Button
                type="submit"
                disabled={isChangingPassword || !currentPassword || !newPassword || !confirmPassword}
              >
                {isChangingPassword && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                Update Password
              </Button>
            </form>
          </div>
        </CardContent>
      </Card>

      {/* Appearance Settings */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Palette className="h-5 w-5" />
            Appearance
          </CardTitle>
          <CardDescription>
            Customize how the application looks
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div>
              <Label className="mb-3 block">Theme</Label>
              <div className="grid grid-cols-3 gap-3">
                {themeOptions.map((option) => {
                  const Icon = option.icon;
                  const isSelected = theme === option.value;
                  return (
                    <button
                      key={option.value}
                      onClick={() => setTheme(option.value as "light" | "dark" | "system")}
                      className={`
                        relative flex flex-col items-center gap-2 p-4 rounded-lg border-2 transition-all
                        ${isSelected
                          ? "border-primary bg-primary/5"
                          : "border-muted hover:border-muted-foreground/25 hover:bg-accent/50"
                        }
                      `}
                    >
                      <Icon className={`h-6 w-6 ${isSelected ? "text-primary" : "text-muted-foreground"}`} />
                      <span className={`text-sm font-medium ${isSelected ? "text-primary" : ""}`}>
                        {option.label}
                      </span>
                      {isSelected && (
                        <div className="absolute top-2 right-2">
                          <Check className="h-4 w-4 text-primary" />
                        </div>
                      )}
                    </button>
                  );
                })}
              </div>
              <p className="text-xs text-muted-foreground mt-2">
                Select your preferred color scheme. System will match your device settings.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Application Settings */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe className="h-5 w-5" />
            Application
          </CardTitle>
          <CardDescription>
            Configure global application settings
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div className="grid gap-2 max-w-md">
              <Label htmlFor="timezone">Scheduler Timezone</Label>
              <div className="flex gap-2">
                <Select
                  value={timezone}
                  onValueChange={handleTimezoneChange}
                  disabled={isSavingTimezone}
                >
                  <SelectTrigger id="timezone" className="flex-1">
                    <SelectValue placeholder="Select timezone" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableTimezones.map((tz) => (
                      <SelectItem key={tz} value={tz}>
                        {tz}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {isSavingTimezone && (
                  <Loader2 className="h-4 w-4 animate-spin self-center" />
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Timezone used for scheduling backup jobs. All cron schedules will be interpreted in this timezone.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* App Info */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">About</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Application</span>
              <span className="font-medium">BackupX</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Backend</span>
              <span className="font-medium">Restic + S3</span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
