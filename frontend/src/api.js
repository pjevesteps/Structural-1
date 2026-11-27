// In production single-container builds, API_BASE defaults to "" (same-origin relative /api/...).
// In local Vite dev, vite.config.js proxies /api -> http://127.0.0.1:8000 seamlessly.
export const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");

export function apiUrl(path) {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
}
