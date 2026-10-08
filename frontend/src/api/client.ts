// same-origin fetch wrapper; attaches the csrf token on writes and surfaces the
// server's error detail so the ui can show a meaningful message.

import { accountChanged, accountKnown, currentAccount, currentSession } from "../lib/session";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

// the endpoints that find out or deliberately change who is signed in; everything else is bound to the
// account the tab shows. logout is not here: a tab must not end a session that is no longer its own
const IDENTITY = new Set([
  "/auth/me",
  "/auth/csrf",
  "/auth/jwks",
  "/auth/login",
  "/auth/register",
  "/auth/register/code",
  "/auth/password/code",
  "/auth/password/reset",
]);

export function isAccountChanged(e: unknown): boolean {
  return e instanceof ApiError && (e.body as { code?: string } | null)?.code === "account_changed";
}

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

// every write needs the token, a guest's first one included, so a visitor without the cookie fetches it first
async function csrfToken(): Promise<string | null> {
  const current = getCookie("csrftoken");
  if (current) return current;
  try {
    await fetch("/api/auth/csrf", { credentials: "same-origin" });
  } catch {
    return null;
  }
  return getCookie("csrftoken");
}

// a request belongs to the session it was made in, or to the one a flow of several requests passes: the
// browser sends whoever is signed in at the moment it leaves, so once its session ended it is not sent.
// the account it is made for goes with it, and the server refuses it when that is not who is signed in,
// for this tab and any other that shares its cookies
export async function api<T>(path: string, init: RequestInit = {}, session = currentSession()): Promise<T> {
  const bound = !IDENTITY.has(path.split("?")[0]);
  if (bound) await accountKnown();
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = await csrfToken();
    if (csrf) headers.set("X-CSRFToken", csrf);
  }
  if (session !== currentSession()) {
    throw new ApiError("Сеанс сменился, запрос не отправлен", 0);
  }
  if (bound) headers.set("X-Account", String(currentAccount() ?? "guest"));

  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, { ...init, headers, credentials: "same-origin" });
  } catch {
    throw new ApiError("Сервер недоступен. Проверьте соединение.", 0);
  }

  const body = resp.headers.get("content-type")?.includes("application/json")
    ? await resp.json().catch(() => null)
    : null;

  if (!resp.ok) {
    const detail = body?.detail;
    const error = new ApiError(typeof detail === "string" ? detail : `Ошибка ${resp.status}`, resp.status, body);
    if (isAccountChanged(error)) accountChanged();
    throw error;
  }
  return body as T;
}
