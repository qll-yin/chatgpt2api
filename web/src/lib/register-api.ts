/**
 * 注册管理相关的 API 封装。
 *
 * 与 lib/api.ts 的 httpRequest 保持同一套约定（自动带 auth-key、
 * 统一错误处理），单独成文件是因为注册相关接口数量多且自成一路。
 */

import { httpRequest } from "@/lib/request";

// ──────────────────────── 类型 ────────────────────────

export type RegisterProxy = {
  proxy: string;
  enabled: boolean;
  max_uses: number;
  used_count: number;
  failed_count: number;
  proxy_type: string;
  cf_blocked: number;
  ipv6_status: string;
  note?: string | null;
  status: "available" | "disabled" | "exhausted";
};

export type RegisterProxyStats = {
  total: number;
  available: number;
  disabled: number;
  exhausted: number;
  used: number;
  failed: number;
  enabled: number;
};

export type RegisterStatus = {
  available: number;
  in_use: number;
  done: number;
  failed: number;
  total: number;
};

export type RegisterRun = {
  run_id: string;
  email?: string | null;
  status?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  error?: string | null;
  error_category?: string | null;
  duration?: number | null;
};

export type RegisteredItem = {
  email: string;
  has_access_token: boolean;
  has_refresh_token: boolean;
  proxy: string;
  created_at?: number | null;
};

export type MailProviderField = {
  key: string;
  label?: string;
  type?: string;
  placeholder?: string;
  secret?: boolean;
};

export type MailProvider = {
  kind: string;
  display_name: string;
  pooled: boolean;
  ephemeral: boolean;
  config_fields: MailProviderField[];
};

export type BatchStatus = {
  state: "stopped" | "running" | "paused";
  started_at: number;
  elapsed: number;
  registered_ok: number;
  registered_fail: number;
  target_count: number;
  remaining: number | null;
  concurrency: number;
  workers: { id: number; email: string; run_id: string; proxy: string; started_at: number }[];
  last_message: string;
  pool_stats: RegisterStatus;
};

export type BatchStartOptions = {
  concurrency: number;
  target_count: number;
  cool_down_seconds: number;
  proxy: string;
  mail_source: string;
  want_access_token: boolean;
  want_session_token: boolean;
  want_refresh_token: boolean;
  want_2fa: boolean;
  otp_timeout: number;
  auto_import: boolean;
  with_proxy: boolean;
};

export type RegisterOnceOptions = Omit<BatchStartOptions, "concurrency" | "target_count" | "cool_down_seconds">;

export type ImportToPoolResult = {
  total: number;
  imported: number;
  skipped: number;
  missing: string[];
};

export type AutoRegisterSettings = {
  enabled: boolean;
  threshold: number;
  target_batch: number;
  check_interval_minute: number;
  register_url: string;
  register_auth_key: string;
  register_options: Record<string, unknown>;
  cooldown_minute: number;
};

export type AutoRegisterState = {
  running: boolean;
  last_check_at: number;
  last_check_active: number;
  last_trigger_at: number;
  last_trigger_result: string;
  last_error: string;
  trigger_count: number;
  in_flight: boolean;
};

// ──────────────────────── 接口 ────────────────────────

export async function fetchRegisterStatus(): Promise<RegisterStatus> {
  return httpRequest<RegisterStatus>("/api/register/status");
}

export async function fetchRegisterRuns(): Promise<{ items: RegisterRun[] }> {
  return httpRequest<{ items: RegisterRun[] }>("/api/register/runs");
}

export async function fetchRegistered(
  limit = 100,
  offset = 0,
): Promise<{ items: RegisteredItem[]; total: number }> {
  return httpRequest<{ items: RegisteredItem[]; total: number }>(
    `/api/register/registered?limit=${limit}&offset=${offset}`,
  );
}

export async function importRegisteredToPool(
  emails: string[],
  withProxy: boolean,
): Promise<ImportToPoolResult> {
  return httpRequest<ImportToPoolResult>("/api/register/import-to-pool", {
    method: "POST",
    body: { emails, with_proxy: withProxy },
  });
}

export async function importTokenTextToPool(
  text: string,
): Promise<ImportToPoolResult> {
  return httpRequest<ImportToPoolResult>("/api/register/import-text", {
    method: "POST",
    body: { text },
  });
}

export async function fetchRegisterProxies(): Promise<{
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies",
  );
}

export async function importRegisterProxies(
  text: string,
  mode: "replace" | "append" = "replace",
): Promise<{ total: number }> {
  // 后端负责把文本拆成代理列表（每行一个），这里原样传过去
  return httpRequest<{ total: number }>("/api/register/proxies", {
    method: "POST",
    body: { text, mode },
  });
}

export async function fetchMailProviders(): Promise<{ providers: MailProvider[] }> {
  return httpRequest<{ providers: MailProvider[] }>("/api/register/mail-providers");
}

export async function fetchRegisterSettings(): Promise<{ values: Record<string, string> }> {
  return httpRequest<{ values: Record<string, string> }>("/api/register/settings");
}

export async function saveRegisterSettings(
  values: Record<string, string>,
): Promise<{ values: Record<string, string> }> {
  return httpRequest<{ values: Record<string, string> }>("/api/register/settings", {
    method: "POST",
    body: { values },
  });
}

// ──────────────────────── 批量注册 ────────────────────────

export async function startBatchRegister(options: BatchStartOptions) {
  return httpRequest<{ ok: boolean; concurrency: number; target_count: number }>(
    "/api/register/batch/start",
    { method: "POST", body: options },
  );
}

export async function pauseBatchRegister() {
  return httpRequest<{ ok: boolean }>("/api/register/batch/pause", { method: "POST" });
}

export async function resumeBatchRegister() {
  return httpRequest<{ ok: boolean }>("/api/register/batch/resume", { method: "POST" });
}

export async function stopBatchRegister() {
  return httpRequest<{ ok: boolean }>("/api/register/batch/stop", { method: "POST" });
}

export async function fetchBatchStatus(): Promise<BatchStatus> {
  return httpRequest<BatchStatus>("/api/register/batch/status");
}

export async function registerOnce(options: RegisterOnceOptions) {
  return httpRequest<{
    ok: boolean;
    run_id: string;
    email: string;
    error?: string;
    error_category?: string;
    imported?: boolean;
    duration?: number;
  }>("/api/register/once", { method: "POST", body: options });
}

// ──────────────────────── 号池自动补充 ────────────────────────

export async function fetchAutoRegisterSettings(): Promise<{
  settings: AutoRegisterSettings;
  state: AutoRegisterState;
}> {
  return httpRequest<{ settings: AutoRegisterSettings; state: AutoRegisterState }>(
    "/api/auto-register/settings",
  );
}

export async function saveAutoRegisterSettings(
  settings: Partial<AutoRegisterSettings>,
): Promise<{ settings: AutoRegisterSettings }> {
  return httpRequest<{ settings: AutoRegisterSettings }>("/api/auto-register/settings", {
    method: "POST",
    body: settings,
  });
}

export async function checkAutoRegisterNow() {
  return httpRequest<{ skipped: boolean; reason?: string; triggered?: boolean; active?: number }>(
    "/api/auto-register/check",
    { method: "POST" },
  );
}

export async function triggerAutoRegisterNow() {
  return httpRequest<{ ok: boolean; error?: string }>("/api/auto-register/trigger", {
    method: "POST",
  });
}
