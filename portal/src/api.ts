// Talks to the portal API. The private link (#token=...) is stored once, then removed
// from the address bar. In Phase 2 this becomes an SSO session and nothing else changes.

const KEY = "codespringhub-token";

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

let memoryToken: string | null = null;

function keep(value: string) {
  memoryToken = value;
  try {
    storage()?.setItem(KEY, value);
  } catch {
    /* private window: keep it in memory */
  }
}

// Two kinds of link: the private link printed by portal.sh (#token=...), and a one-time link
// from Claude's open_portal (#ticket=...), which is swapped for the private link here so the
// secret itself never appears in a chat.
export async function initToken() {
  const ticket = location.hash.match(/ticket=([^&]+)/);
  const m = location.hash.match(/token=([^&]+)/);
  if (ticket || m) history.replaceState(null, "", location.pathname + "#/");
  if (m) keep(m[1]);
  if (ticket) {
    try {
      const res = await fetch("/api/redeem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticket: decodeURIComponent(ticket[1]) }),
      });
      if (res.ok) keep((await res.json()).token);
    } catch {
      /* fall through: a saved link may still work, otherwise the page explains */
    }
  }
}

function token(): string | null {
  if (memoryToken) return memoryToken;
  try {
    return storage()?.getItem(KEY) ?? null;
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${token() ?? ""}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.";
    throw new ApiError(detail, res.status);
  }
  return data as T;
}

export const get = <T = any>(p: string) => api<T>("GET", p);
export const post = <T = any>(p: string, b?: unknown) => api<T>("POST", p, b ?? {});
export const put = <T = any>(p: string, b: unknown) => api<T>("PUT", p, b);
export const del = <T = any>(p: string) => api<T>("DELETE", p);

export async function callTool(name: string, args: Record<string, unknown>) {
  return post<{ is_error: boolean; content: string[]; characters: number }>(`/api/tools/${name}`, { arguments: args });
}

// ---- shared types ----------------------------------------------------------------

export type Me = {
  user: string;
  name: string;
  is_bsr: boolean;
  auth: string;
  categories: Record<string, string>;
  assays: string[];
};
export type Project = { id: string; name: string; description: string };
export type Scope = "lab" | "personal";
export type DocInfo = { id: string; title: string; category: string; rel_path: string; scope: Scope; owner: string | null };
export type Lab = {
  lab_id: string;
  name: string;
  description: string;
  projects: Project[];
  datasets: number;
  documents: number;
  can_edit: boolean;
  role: "member" | "pi" | "bsr";
  unix_group?: string;
  pis?: string[];
  documents_list?: DocInfo[];
};
export type Dataset = {
  id: string;
  short_id: string;
  title: string;
  kind: "data" | "code";
  scope: Scope;
  owner: string | null;
  can_edit: boolean;
  project: string | null;
  assay: string;
  description: string;
  sensitivity: "standard" | "sensitive";
  registered_by: string;
  registered_on: string;
  path: string;
};
export type Workflow = {
  id: string;
  name: string;
  assays: string[];
  status: "validated" | "candidate";
  version: string;
  url: string;
  summary: string;
};
