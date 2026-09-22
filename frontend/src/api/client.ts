// same-origin fetch wrapper; attaches the csrf token on writes and surfaces the
// server's error detail so the ui can show a meaningful message.

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
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

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = await csrfToken();
    if (csrf) headers.set("X-CSRFToken", csrf);
  }

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
    throw new ApiError(typeof detail === "string" ? detail : `Ошибка ${resp.status}`, resp.status, body);
  }
  return body as T;
}
