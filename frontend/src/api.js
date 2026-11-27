// In production single-container builds, API_BASE defaults to "" (same-origin relative /api/...).
// In local Vite dev, vite.config.js proxies /api -> http://127.0.0.1:8000 seamlessly.
export const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");

export function apiUrl(path) {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
}

let _fetchWrapped = false;

/**
 * Wrap window.fetch once so every /api/* call automatically includes HttpOnly cookies
 * (credentials: "include") and triggers a clean session-expired event on HTTP 401.
 */
export function installApiFetchDefaults() {
  if (_fetchWrapped || typeof window === "undefined" || typeof window.fetch !== "function") {
    return;
  }
  _fetchWrapped = true;
  const nativeFetch = window.fetch.bind(window);

  window.fetch = async (input, init) => {
    const urlStr = typeof input === "string" ? input : input?.url || "";
    const isApiCall =
      urlStr.startsWith("/api/") ||
      urlStr === "/api" ||
      (API_BASE && urlStr.startsWith(`${API_BASE}/api`));

    const nextInit = isApiCall
      ? { credentials: "include", ...(init || {}) }
      : init;

    const res = await nativeFetch(input, nextInit);

    if (
      isApiCall &&
      res.status === 401 &&
      !urlStr.includes("/api/auth/login") &&
      !urlStr.includes("/api/auth/me")
    ) {
      window.dispatchEvent(new CustomEvent("sao:unauthorized"));
    }
    return res;
  };
}
