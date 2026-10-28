import { reactive, ref } from "../vendor/vue.esm-browser.prod.js";

// Shared state, API access, formatting and routing.

export const session = reactive({ user: null, csrf: "", checked: false });

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && session.csrf) headers["X-CSRF-Token"] = session.csrf;

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the panel. Check that the server is online.");
  }

  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/auth/login") {
    session.user = null;
    session.csrf = "";
  }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong.";
    throw new ApiError(response.status, detail.charAt(0).toUpperCase() + detail.slice(1) + ".");
  }
  return data;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (days) parts.push(`${days} ${days === 1 ? "day" : "days"}`);
  if (hours) parts.push(`${hours} ${hours === 1 ? "hour" : "hours"}`);
  if (!days && minutes) parts.push(`${minutes} min`);
  return parts.join(", ") || "Just started";
}

export const route = ref(location.pathname);
window.addEventListener("popstate", () => (route.value = location.pathname));

export function navigate(path) {
  if (path === route.value) return;
  history.pushState({}, "", path);
  route.value = path;
}


export function formatDate(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
