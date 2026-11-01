// Fetch helpers for the backupx API.
//
// CSRF: every mutating request to our own origin must carry the session's
// CSRF token in the X-CSRFToken header (Flask-WTF). installApiFetch() wraps
// window.fetch once at start-up so every existing `fetch("/api/...", {method:
// "POST"})` call gets the header without changes; when the server answers
// 400 {code: "csrf"} (token missing/rotated, e.g. after re-login) the token is
// fetched again and the request retried once.

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);
const CSRF_HEADER = "X-CSRFToken";

let csrfToken: string | null = null;
let csrfPromise: Promise<string | null> | null = null;
let nativeFetch: typeof window.fetch = (...args) => window.fetch(...args);

export function setCsrfToken(token: string | null | undefined) {
  csrfToken = token || null;
}

async function loadCsrfToken(force = false): Promise<string | null> {
  if (csrfToken && !force) return csrfToken;
  if (!csrfPromise) {
    csrfPromise = nativeFetch("/api/auth/csrf", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        csrfToken = data?.csrf_token ?? null;
        return csrfToken;
      })
      .catch(() => null)
      .finally(() => {
        csrfPromise = null;
      });
  }
  return csrfPromise;
}

function isSameOrigin(input: RequestInfo | URL): boolean {
  try {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    return new URL(url, window.location.href).origin === window.location.origin;
  } catch {
    return false;
  }
}

function methodOf(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  if (typeof input !== "string" && !(input instanceof URL)) return input.method.toUpperCase();
  return "GET";
}

async function isCsrfFailure(response: Response): Promise<boolean> {
  if (response.status !== 400) return false;
  try {
    const data = await response.clone().json();
    return data?.code === "csrf";
  } catch {
    return false;
  }
}

async function withToken(init: RequestInit | undefined, force: boolean): Promise<RequestInit> {
  const token = await loadCsrfToken(force);
  const headers = new Headers(init?.headers);
  if (token) headers.set(CSRF_HEADER, token);
  return { ...init, headers, credentials: init?.credentials ?? "same-origin" };
}

/** fetch() that adds the CSRF header to mutating same-origin requests. */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const method = methodOf(input, init);
  if (SAFE_METHODS.has(method) || !isSameOrigin(input) || String(input).endsWith("/api/auth/login")) {
    return nativeFetch(input, init);
  }
  const response = await nativeFetch(input, await withToken(init, false));
  // A Request object's body can't be replayed; plain (url, init) calls can
  const replayable = typeof input === "string" || input instanceof URL;
  if (replayable && (await isCsrfFailure(response))) {
    return nativeFetch(input, await withToken(init, true));
  }
  return response;
}

let installed = false;

/** Route every window.fetch call through apiFetch (call once, before rendering). */
export function installApiFetch() {
  if (installed) return;
  installed = true;
  nativeFetch = window.fetch.bind(window);
  window.fetch = apiFetch as typeof window.fetch;
}

export class ApiError extends Error {
  status: number;
  code?: string;
  data: unknown;
  constructor(message: string, status: number, code?: string, data?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

/** JSON request; throws ApiError with the server's error/code on non-2xx. */
export async function apiJson<T = unknown>(url: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const headers = new Headers(rest.headers);
  if (json !== undefined) headers.set("Content-Type", "application/json");
  const response = await apiFetch(url, {
    ...rest,
    headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const body = (data ?? {}) as { error?: string; code?: string };
    throw new ApiError(body.error || `Request failed (${response.status})`, response.status, body.code, data);
  }
  return data as T;
}
