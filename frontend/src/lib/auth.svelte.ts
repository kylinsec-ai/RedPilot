/** 控制面管理员凭据只保存在当前浏览器 session，不写入 localStorage。 */

function readToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem("ghost.admin_token") || "";
  } catch {
    return "";
  }
}

export const adminAuth = $state({ token: readToken() });

export function setAdminToken(value: string): void {
  const token = value.trim();
  adminAuth.token = token;
  if (typeof window === "undefined") return;
  try {
    if (token) window.sessionStorage.setItem("ghost.admin_token", token);
    else window.sessionStorage.removeItem("ghost.admin_token");
  } catch {
    // sessionStorage disabled: in-memory token remains usable for this page.
  }
}
