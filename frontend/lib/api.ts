export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

export function getAccessToken() { return typeof window === "undefined" ? null : sessionStorage.getItem("gnk_access"); }
export function saveTokens(data: {access_token:string; refresh_token:string}) {
  sessionStorage.setItem("gnk_access", data.access_token);
  localStorage.setItem("gnk_refresh", data.refresh_token);
}
export function clearTokens() { sessionStorage.removeItem("gnk_access"); localStorage.removeItem("gnk_refresh"); }

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getAccessToken();
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {...init, headers, credentials: "include"});
  } catch {
    throw new Error("Cannot reach the GnKAlgo API. Please try again shortly.");
  }
  if (response.status === 401 && localStorage.getItem("gnk_refresh") && path !== "/auth/refresh") {
    const refreshed = await fetch(`${API_URL}/auth/refresh`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({refresh_token:localStorage.getItem("gnk_refresh")})});
    if (refreshed.ok) { saveTokens(await refreshed.json()); headers.set("Authorization", `Bearer ${getAccessToken()}`); response = await fetch(`${API_URL}${path}`, {...init, headers, credentials:"include"}); }
    else clearTokens();
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const fallback = response.status >= 500
      ? "The GnKAlgo service is temporarily unavailable. Please try again shortly."
      : `Request failed (${response.status})`;
    throw new Error(body.detail ?? fallback);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
