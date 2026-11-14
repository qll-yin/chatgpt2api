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
  /** 号池绑定配额：能绑多少个账号到号池（0 = 不限） */
  bind_max_uses: number;
  /** 已绑定的账号数 */
  bind_used_count: number;
  /** 剩余可绑定名额（-1 = 不限） */
  bind_remaining: number;
  /** 号池绑定状态：动态代理不适用 */
  bind_status: "available" | "disabled" | "exhausted" | "not_applicable";
  cf_blocked: number;
  /** 连续 409 invalid_state 的次数（达阈值静态代理自动停用） */
  invalid_state: number;
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

/** 号池绑定配额概览（静态代理专用）。 */
export type BindProxyStats = {
  static_total: number;
  bindable: number;
  bind_exhausted: number;
  bind_used: number;
  bind_capacity: number;
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
  batch_id?: string | null;
  email?: string | null;
  status?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  log_path?: string | null;
  error?: string | null;
  error_category?: string | null;
  duration?: number | null;
};

/** 一次批量注册 = 一个批次，下面挂多个 run */
export type RegisterRunBatch = {
  batch_id: string;
  started_at?: number | null;
  finished_at?: number | null;
  duration?: number | null;
  total: number;
  ok: number;
  fail: number;
  running: number;
  /** 成功注册的邮箱 */
  emails: string[];
  runs: RegisterRun[];
};

export type RegisteredItem = {
  email: string;
  has_access_token: boolean;
  has_refresh_token: boolean;
  proxy: string;
  created_at?: number | null;
  /** 标注异常（被封 / 额度耗尽 / 不可用） */
  abnormal: boolean;
};

export type MailProviderField = {
  key: string;
  label?: string;
  /** text / password / number / select */
  type?: string;
  placeholder?: string;
  required?: boolean;
  help?: string;
  /** type === "select" 时的候选项 */
  options?: string[];
};

export type MailProvider = {
  kind: string;
  display_name: string;
  pooled: boolean;
  ephemeral: boolean;
  config_fields: MailProviderField[];
  /** 必填配置项是否都填了 */
  configured: boolean;
  /** 缺哪些必填项 */
  missing: string[];
  /** 是否真的能用（配置齐 + 池化来源还有可用邮箱） */
  ready: boolean;
  /** 池化来源的邮箱池可用数 */
  pool_available: number | null;
};

export type BatchStatus = {
  /** waiting = 等资源（号池空 / 代理用尽），此时没有任何注册在跑 */
  state: "stopped" | "running" | "paused" | "waiting";
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
  /** 号池绑定统计（绑了几个 / 因超额跳过几个 / 回落用注册代理几个） */
  bind_stats?: {
    bound: number;
    skipped_no_quota: number;
    no_proxy: number;
  };
};

export type MailPoolItem = {
  email: string;
  password?: string | null;
  client_id?: string | null;
  refresh_token?: string | null;
  relay_url?: string | null;
  kind: string;
  status: string;
  imported_at?: number | null;
  claimed_at?: number | null;
  finished_at?: number | null;
  fail_reason?: string | null;
};

export type SmsConfig = {
  sms_enabled: string;
  sms_provider: string;
  sms_api_key: string;
  sms_country: string;
  sms_service: string;
  sms_max_price: string;
  sms_fixed_price: string;
  sms_reuse_phone: string;
  sms_phone_success_max: string;
  sms_auto_country: string;
  sms_strict_whitelist: string;
  sms_allowed_countries: string;
  sms_auto_min_stock: string;
  sms_auto_max_price: string;
  sms_max_phone_attempts: string;
  sms_per_phone_timeout: string;
};

export type SmsCountry = {
  id: string;
  name_cn: string;
  openai_sms_safe?: boolean;
  price?: number | null;
  count?: number | null;
};

export type MailOrder = {
  order_no: string;
  email: string;
  suffix: string;
  service_mode: string;
  supply_policy: string;
  project_id?: number | null;
  order_status: string;
  pay_amount: string;
  received_at?: number | null;
  used_email: string;
  attempt_count: number;
  max_attempts: number;
  last_error?: string | null;
  disabled: boolean;
  has_token: boolean;
  reusable: boolean;
};

export type MailOrderMail = {
  id?: string | null;
  sender: string;
  subject: string;
  receivedAt: string;
  verificationCode: string;
  bodyPreview: string;
};

export type AutoRegisterSettings = {
  enabled: boolean;
  threshold: number;
  target_batch: number;
  check_interval_minute: number;
  concurrency: number;
  /** 触发的批量注册用哪个邮箱来源（留空 = 用「邮箱配置」里选的） */
  mail_source: string;
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

export async function fetchRegisterRuns(
  limit = 50,
  offset = 0,
): Promise<{ items: RegisterRunBatch[]; total: number }> {
  return httpRequest<{ items: RegisterRunBatch[]; total: number }>(
    `/api/register/runs?limit=${limit}&offset=${offset}`,
  );
}

export async function fetchBatchLog(batchId: string): Promise<{
  batch_id: string;
  emails: string[];
  content: string;
}> {
  return httpRequest<{ batch_id: string; emails: string[]; content: string }>(
    `/api/register/batches/${encodeURIComponent(batchId)}/log`,
  );
}

export async function fetchRegistered(
  limit = 100,
  offset = 0,
): Promise<{ items: RegisteredItem[]; total: number }> {
  return httpRequest<{ items: RegisteredItem[]; total: number }>(
    `/api/register/registered?limit=${limit}&offset=${offset}`,
  );
}

export async function fetchRegisteredDetail(email: string): Promise<{
  item: Record<string, unknown> & {
    email: string;
    access_token?: string | null;
    refresh_token?: string | null;
    id_token?: string | null;
    session_token?: string | null;
    password?: string | null;
    proxy?: string | null;
    device_id?: string | null;
    cookie_header?: string | null;
    totp_secret?: string | null;
    extra?: Record<string, unknown>;
    created_at?: number | null;
  };
}> {
  return httpRequest(`/api/register/registered/${encodeURIComponent(email)}`);
}

export async function importRegisteredToPool(
  emails: string[],
  withProxy: boolean,
  options?: { bindProxy?: string; autoBind?: boolean },
): Promise<ImportToPoolResult> {
  return httpRequest<ImportToPoolResult>("/api/register/import-to-pool", {
    method: "POST",
    body: {
      emails,
      with_proxy: withProxy,
      bind_proxy: options?.bindProxy || null,
      auto_bind: Boolean(options?.autoBind),
    },
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
  bind_stats: BindProxyStats;
}> {
  return httpRequest<{
    items: RegisterProxy[];
    stats: RegisterProxyStats;
    bind_stats: BindProxyStats;
  }>("/api/register/proxies");
}

/** 可绑账号的静态代理列表（含剩余名额）。 */
export async function fetchBindableProxies(): Promise<{
  items: RegisterProxy[];
  stats: BindProxyStats;
}> {
  return httpRequest<{ items: RegisterProxy[]; stats: BindProxyStats }>(
    "/api/register/proxies/bindable",
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

export async function updateRegisterProxy(
  proxy: string,
  patch: {
    enabled?: boolean;
    max_uses?: number;
    bind_max_uses?: number;
    note?: string;
    proxy_type?: "static" | "dynamic";
  },
): Promise<{ items: RegisterProxy[]; stats: RegisterProxyStats }> {
  return httpRequest<{ items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies",
    { method: "PATCH", body: { proxy, ...patch } },
  );
}

export async function deleteRegisterProxy(proxy: string): Promise<{
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ items: RegisterProxy[]; stats: RegisterProxyStats }>(
    `/api/register/proxies/${encodeURIComponent(proxy)}`,
    { method: "DELETE" },
  );
}

export async function clearRegisterProxies(): Promise<{
  removed: number;
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ removed: number; items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies",
    { method: "DELETE" },
  );
}

export async function resetRegisterProxyUsage(proxy?: string): Promise<{
  reset: number;
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ reset: number; items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies/reset",
    { method: "POST", body: { proxy: proxy || null } },
  );
}

export async function resetRegisterProxyCf(proxy?: string): Promise<{
  reset: number;
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ reset: number; items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies/cf-reset",
    { method: "POST", body: { proxy: proxy || null } },
  );
}

export type ProxyTestResult = {
  ok: boolean;
  latency_ms?: number;
  ip?: string;
  ipv6?: boolean;
  ipv6_addr?: string;
  error?: string;
};

export async function resetRegisterProxyInvalidState(proxy: string): Promise<{
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{ items: RegisterProxy[]; stats: RegisterProxyStats }>(
    "/api/register/proxies/invalid-reset",
    { method: "POST", body: { proxy } },
  );
}

export async function testRegisterProxies(
  proxies: string[],
  timeout = 8,
): Promise<{ results: Record<string, ProxyTestResult> }> {
  return httpRequest<{ results: Record<string, ProxyTestResult> }>(
    "/api/register/proxies/test",
    { method: "POST", body: { proxies, timeout } },
  );
}

export async function testRegisterProxiesIpv6(
  proxies: string[],
  timeout = 10,
): Promise<{
  results: Record<string, ProxyTestResult>;
  items: RegisterProxy[];
  stats: RegisterProxyStats;
}> {
  return httpRequest<{
    results: Record<string, ProxyTestResult>;
    items: RegisterProxy[];
    stats: RegisterProxyStats;
  }>("/api/register/proxies/ipv6-test", { method: "POST", body: { proxies, timeout } });
}

export async function fetchMailProviders(): Promise<{
  providers: MailProvider[];
  current: string;
}> {
  return httpRequest<{ providers: MailProvider[]; current: string }>(
    "/api/register/mail-providers",
  );
}

export async function fetchMailConfig(): Promise<{ config: Record<string, string> }> {
  return httpRequest<{ config: Record<string, string> }>("/api/register/mail-config");
}

export async function saveMailConfig(
  values: Record<string, string>,
): Promise<{ config: Record<string, string> }> {
  return httpRequest<{ config: Record<string, string> }>("/api/register/mail-config", {
    method: "POST",
    body: values,
  });
}

export async function testMailConnection(): Promise<{ ok: boolean; message?: string }> {
  return httpRequest<{ ok: boolean; message?: string }>("/api/register/mail-test", {
    method: "POST",
  });
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

// ──────────────────────── Remail 订单 ────────────────────────

export async function fetchMailOrders(
  limit = 50,
  offset = 0,
): Promise<{ items: MailOrder[]; total: number }> {
  return httpRequest<{ items: MailOrder[]; total: number }>(
    `/api/register/mail/orders?limit=${limit}&offset=${offset}`,
  );
}

export async function createMailOrder(payload: {
  suffix?: string;
  project_id?: number;
  service_mode?: string;
  supply_policy?: string;
}): Promise<{ ok: boolean; email: string; items: MailOrder[] }> {
  return httpRequest<{ ok: boolean; email: string; items: MailOrder[] }>(
    "/api/register/mail/orders",
    { method: "POST", body: payload },
  );
}

export async function fetchOrderMails(orderNo: string): Promise<{
  email: string;
  items: MailOrderMail[];
}> {
  return httpRequest<{ email: string; items: MailOrderMail[] }>(
    `/api/register/mail/orders/${encodeURIComponent(orderNo)}/mails`,
  );
}

export async function linkMailOrder(orderNo: string, email: string) {
  return httpRequest<{ ok: boolean }>(
    `/api/register/mail/orders/${encodeURIComponent(orderNo)}/link`,
    { method: "POST", body: { email } },
  );
}

export async function setOrderDisabled(orderNo: string, disabled: boolean) {
  return httpRequest<{ ok: boolean }>(
    `/api/register/mail/orders/${encodeURIComponent(orderNo)}/disabled`,
    { method: "POST", body: { disabled } },
  );
}

export async function deleteMailOrder(orderNo: string) {
  return httpRequest<{ ok: boolean }>(
    `/api/register/mail/orders/${encodeURIComponent(orderNo)}`,
    { method: "DELETE" },
  );
}

export async function bulkDeleteOrders(orderNos: string[]) {
  return httpRequest<{ ok: boolean; deleted: number }>(
    "/api/register/mail/orders/bulk_delete",
    { method: "POST", body: { order_nos: orderNos } },
  );
}

export async function fetchMailWallet(): Promise<{
  balance: string;
  enabled?: boolean;
}> {
  return httpRequest<{ balance: string; enabled?: boolean }>(
    "/api/register/mail/wallet",
  );
}

// ──────────────────────── 标注账号异常 ────────────────────────

export async function markRegisteredAbnormal(
  emails: string[],
  abnormal = true,
): Promise<{
  updated: number;
  total: number;
  proxy_stats: RegisterProxyStats;
}> {
  return httpRequest<{
    updated: number;
    total: number;
    proxy_stats: RegisterProxyStats;
  }>("/api/register/registered/mark-abnormal", {
    method: "POST",
    body: { emails, abnormal },
  });
}

// ──────────────────────── 邮箱池 ────────────────────────

export async function fetchMailPool(
  params: { status?: string; kind?: string; limit?: number; offset?: number } = {},
): Promise<{ items: MailPoolItem[]; total: number; stats: RegisterStatus }> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.kind) q.set("kind", params.kind);
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  return httpRequest<{ items: MailPoolItem[]; total: number; stats: RegisterStatus }>(
    `/api/register/mail-pool?${q.toString()}`,
  );
}

export async function importMailPool(
  text: string,
  kind = "outlook",
): Promise<{ ok: boolean; inserted?: number; updated?: number; skipped?: number }> {
  return httpRequest<{
    ok: boolean;
    inserted?: number;
    updated?: number;
    skipped?: number;
  }>("/api/register/mail-pool", { method: "POST", body: { text, kind } });
}

export async function deleteMailPool(emails: string[]) {
  return httpRequest<{ removed: number }>("/api/register/mail-pool", {
    method: "DELETE",
    body: { emails },
  });
}

// ──────────────────────── SMS 接码配置 ────────────────────────

export async function fetchSmsConfig(): Promise<{ config: SmsConfig }> {
  return httpRequest<{ config: SmsConfig }>("/api/register/sms");
}

export async function saveSmsConfig(values: Partial<SmsConfig>) {
  return httpRequest<{ ok: boolean; config: SmsConfig }>("/api/register/sms", {
    method: "POST",
    body: values,
  });
}

export async function testSms() {
  return httpRequest<{ ok: boolean; provider: string; balance: number; message: string }>(
    "/api/register/sms/test",
    { method: "POST" },
  );
}

export async function fetchSmsCountries(): Promise<{
  countries: SmsCountry[];
  openai_sms_safe: string[];
}> {
  return httpRequest<{ countries: SmsCountry[]; openai_sms_safe: string[] }>(
    "/api/register/sms/countries",
  );
}

export async function fetchSmsAllCountries(provider = ""): Promise<{
  countries: SmsCountry[];
  openai_sms_safe: string[];
  source: string;
}> {
  const q = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return httpRequest<{ countries: SmsCountry[]; openai_sms_safe: string[]; source: string }>(
    `/api/register/sms/all-countries${q}`,
  );
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
