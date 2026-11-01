import { createContext, useCallback, useContext, useRef, useState } from "react";
import { Loader2, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiJson, ApiError } from "@/lib/api";

// Step-up re-authentication: confirm/maintenance actions (restores, config
// changes, DB restarts...) need the admin password re-entered within the
// last 5 minutes. ensureStepUp() resolves true right away while a step-up is
// still valid, otherwise asks for the password.

interface StepUpContextValue {
  ensureStepUp: (reason?: string) => Promise<boolean>;
  /**
   * Run a request; if the server answers 403 step_up_required, ask for the
   * password and run it once more.
   */
  withStepUp: (request: () => Promise<Response>, reason?: string) => Promise<Response>;
}

const StepUpContext = createContext<StepUpContextValue | null>(null);

export function useStepUp() {
  const ctx = useContext(StepUpContext);
  if (!ctx) throw new Error("useStepUp must be used within a StepUpProvider");
  return ctx;
}

async function stepUpRequired(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  try {
    const data = await response.clone().json();
    return data?.code === "step_up_required";
  } catch {
    return false;
  }
}

export function StepUpProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<string | undefined>();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const pending = useRef<((ok: boolean) => void) | null>(null);

  const finish = (ok: boolean) => {
    pending.current?.(ok);
    pending.current = null;
    setOpen(false);
    setPassword("");
    setError(null);
  };

  const prompt = useCallback((why?: string) => {
    return new Promise<boolean>((resolve) => {
      pending.current?.(false);
      pending.current = resolve;
      setReason(why);
      setError(null);
      setPassword("");
      setOpen(true);
    });
  }, []);

  const ensureStepUp = useCallback(
    async (why?: string) => {
      try {
        const status = await apiJson<{ valid: boolean }>("/api/auth/step-up");
        if (status.valid) return true;
      } catch {
        // fall through to the prompt
      }
      return prompt(why);
    },
    [prompt]
  );

  const withStepUp = useCallback(
    async (request: () => Promise<Response>, why?: string) => {
      const response = await request();
      if (!(await stepUpRequired(response))) return response;
      if (!(await prompt(why))) return response;
      return request();
    },
    [prompt]
  );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiJson("/api/auth/step-up", { method: "POST", json: { password } });
      finish(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not verify the password");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <StepUpContext.Provider value={{ ensureStepUp, withStepUp }}>
      {children}
      <Dialog open={open} onOpenChange={(o) => !o && finish(false)}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={submit} className="grid gap-4">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <ShieldCheck className="h-5 w-5" />
                Confirm it's you
              </DialogTitle>
              <DialogDescription>
                {reason ? `${reason}. ` : ""}Re-enter your password. It stays confirmed for 5 minutes.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-2">
              <Label htmlFor="step-up-password">Password</Label>
              <Input
                id="step-up-password"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={error ? true : undefined}
              />
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => finish(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!password || submitting}>
                {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                Confirm
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </StepUpContext.Provider>
  );
}
