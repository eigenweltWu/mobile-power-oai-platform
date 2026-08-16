/** Thin typed fetch client for the FastAPI backend (same-origin, /api/...). */

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, `Network error: ${e instanceof Error ? e.message : String(e)}`);
  }
  if (!res.ok) {
    let detail: string = res.statusText || `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
      else if (body && body.detail) detail = JSON.stringify(body.detail);
      else if (body && typeof body === 'object') detail = JSON.stringify(body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, jsonInit('POST', body)),
  put: <T>(path: string, body?: unknown) => request<T>(path, jsonInit('PUT', body)),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  getBlob: async (path: string): Promise<Blob> => {
    const res = await fetch(path);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return res.blob();
  },
};

/** Download a backend file (e.g. /api/experiments/{id}/export) to disk. */
export async function downloadFile(url: string, fallbackName: string): Promise<void> {
  const blob = await api.getBlob(url);
  const disp = (await (async () => {
    const r = await fetch(url);
    return r.headers.get('content-disposition') || '';
  })());
  const m = /filename="?([^";]+)"?/.exec(disp);
  const name = m ? m[1] : fallbackName;
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}
