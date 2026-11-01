import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { apiJson } from "@/lib/api";
import { isActiveTask, type Task } from "@/types/task";

// Polling (no SSE): fast while something is running or the tray is open,
// slow otherwise so an idle tab costs next to nothing.
const FAST_POLL_MS = 2500;
const SLOW_POLL_MS = 15000;
const RECENT_MINUTES = 60;

interface TasksContextValue {
  tasks: Task[];
  activeCount: number;
  trayOpen: boolean;
  focusedTaskId: string | null;
  setFocusedTaskId: (taskId: string | null) => void;
  setTrayOpen: (open: boolean) => void;
  openTray: (taskId?: string) => void;
  /** Register a task the user just started: it shows up at once and toasts when it ends. */
  track: (task: Task | undefined | null, options?: TrackOptions) => void;
  refresh: () => Promise<void>;
  cancel: (taskId: string) => Promise<void>;
}

interface TrackOptions {
  /** Called once when the task reaches a final status. */
  onDone?: (task: Task) => void;
  /** Start the result file download when it succeeds (e.g. a ZIP). */
  autoDownload?: boolean;
}

const TasksContext = createContext<TasksContextValue | null>(null);

export function useTasks() {
  const ctx = useContext(TasksContext);
  if (!ctx) throw new Error("useTasks must be used within a TasksProvider");
  return ctx;
}

export function downloadTaskFile(task: Task) {
  const url = task.result?.download_url;
  if (!url) return;
  const link = document.createElement("a");
  link.href = url;
  link.download = task.result?.filename ?? "";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

export function TasksProvider({ children, enabled = true }: { children: React.ReactNode; enabled?: boolean }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [trayOpen, setTrayOpen] = useState(false);
  const [focusedTaskId, setFocusedTaskId] = useState<string | null>(null);
  const watched = useRef(new Map<string, TrackOptions>());
  const [watchCount, setWatchCount] = useState(0);
  const timer = useRef<number | null>(null);

  const openTray = useCallback((taskId?: string) => {
    setFocusedTaskId(taskId ?? null);
    setTrayOpen(true);
  }, []);

  const notifyFinished = useCallback((task: Task) => {
    const options = watched.current.get(task.id);
    if (!options) return;
    watched.current.delete(task.id);
    setWatchCount(watched.current.size);
    const message = task.result?.message || task.error || task.step || "";
    if (task.status === "succeeded") {
      if (options.autoDownload && task.result?.download_url) downloadTaskFile(task);
      toast.success(`${task.title}: done`, {
        description: message || undefined,
        action: task.result?.download_url
          ? { label: "Download", onClick: () => downloadTaskFile(task) }
          : undefined,
      });
    } else if (task.status === "cancelled") {
      toast.info(`${task.title}: cancelled`);
    } else {
      toast.error(`${task.title}: ${task.status === "rolled_back" ? "rolled back" : "failed"}`, {
        description: message || undefined,
        action: { label: "Details", onClick: () => openTray(task.id) },
      });
    }
    options.onDone?.(task);
  }, [openTray]);

  const refresh = useCallback(async () => {
    try {
      const data = await apiJson<{ tasks: Task[] }>(`/api/tasks?recent=${RECENT_MINUTES}&limit=50`);
      setTasks(data.tasks);
      for (const task of data.tasks) {
        if (!isActiveTask(task)) notifyFinished(task);
      }
    } catch {
      // Logged out or server restarting: keep the last list, try again later
    }
  }, [notifyFinished]);

  // Logged out: show nothing (the last list stays in state until the next login's poll)
  const visibleTasks = useMemo(() => (enabled ? tasks : []), [enabled, tasks]);
  const activeCount = useMemo(() => visibleTasks.filter(isActiveTask).length, [visibleTasks]);
  const fast = activeCount > 0 || trayOpen || watchCount > 0;

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const loop = async () => {
      await refresh();
      if (!stopped) timer.current = window.setTimeout(loop, fast ? FAST_POLL_MS : SLOW_POLL_MS);
    };
    loop();
    return () => {
      stopped = true;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [refresh, fast, enabled]);


  const track = useCallback(
    (task: Task | undefined | null, options: TrackOptions = {}) => {
      if (!task) return;
      watched.current.set(task.id, options);
      setWatchCount(watched.current.size);
      setTasks((prev) => [task, ...prev.filter((t) => t.id !== task.id)]);
      if (!isActiveTask(task)) notifyFinished(task);
    },
    [notifyFinished]
  );

  const cancel = useCallback(
    async (taskId: string) => {
      try {
        await apiJson(`/api/tasks/${taskId}/cancel`, { method: "POST" });
        toast.info("Cancel requested");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Could not cancel");
      }
      refresh();
    },
    [refresh]
  );

  const value = useMemo(
    () => ({
      tasks: visibleTasks,
      activeCount,
      trayOpen,
      focusedTaskId,
      setFocusedTaskId,
      setTrayOpen,
      openTray,
      track,
      refresh,
      cancel,
    }),
    [visibleTasks, activeCount, trayOpen, focusedTaskId, openTray, track, refresh, cancel]
  );

  return <TasksContext.Provider value={value}>{children}</TasksContext.Provider>;
}
