"use client";

import { Cloud, LoaderCircle, PlugZap, RefreshCw, Save, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { ImageStorageMode, ImageStorageProvider } from "@/lib/api";
import { refreshImageModelCatalog, testProxy, type ProxyTestResult } from "@/lib/api";

import { useSettingsStore } from "../store";

export function ConfigCard() {
  const [isTestingProxy, setIsTestingProxy] = useState(false);
  const [proxyTestResult, setProxyTestResult] = useState<ProxyTestResult | null>(null);
  const [isRefreshingImageModels, setIsRefreshingImageModels] = useState(false);
  const logLevelOptions = ["debug", "info", "warning", "error"];
  const fallbackImageModels = [
    "gpt-image-2.6",
    "gpt-image-2.6-sunburst",
    "gpt-image-2.6-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2",
  ];
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setRefreshAccountIntervalMinute = useSettingsStore((state) => state.setRefreshAccountIntervalMinute);
  const setImageRetentionDays = useSettingsStore((state) => state.setImageRetentionDays);
  const setImagePollTimeoutSecs = useSettingsStore((state) => state.setImagePollTimeoutSecs);
  const setImageAccountConcurrency = useSettingsStore((state) => state.setImageAccountConcurrency);
  const setDefaultImageModel = useSettingsStore((state) => state.setDefaultImageModel);
  const setImageSettleEnabled = useSettingsStore((state) => state.setImageSettleEnabled);
  const setImageRemoveConversationAfterResult = useSettingsStore((state) => state.setImageRemoveConversationAfterResult);
  const setImageSettleSecs = useSettingsStore((state) => state.setImageSettleSecs);
  const setImageTimeoutRetrySecs = useSettingsStore((state) => state.setImageTimeoutRetrySecs);
  const setAutoRemoveInvalidAccounts = useSettingsStore((state) => state.setAutoRemoveInvalidAccounts);
  const setAutoRemoveRateLimitedAccounts = useSettingsStore((state) => state.setAutoRemoveRateLimitedAccounts);
  const setAutoReloginAfterRefresh = useSettingsStore((state) => state.setAutoReloginAfterRefresh);
  const setLogLevel = useSettingsStore((state) => state.setLogLevel);
  const setProxy = useSettingsStore((state) => state.setProxy);
  const setBaseUrl = useSettingsStore((state) => state.setBaseUrl);
  const setGlobalSystemPrompt = useSettingsStore((state) => state.setGlobalSystemPrompt);
  const setSensitiveWordsText = useSettingsStore((state) => state.setSensitiveWordsText);
  const setAIReviewField = useSettingsStore((state) => state.setAIReviewField);
  const setImageStorageField = useSettingsStore((state) => state.setImageStorageField);
  const testImageStorage = useSettingsStore((state) => state.testImageStorage);
  const syncImagesToWebDAV = useSettingsStore((state) => state.syncImagesToWebDAV);
  const isTestingImageStorage = useSettingsStore((state) => state.isTestingImageStorage);
  const isSyncingImageStorage = useSettingsStore((state) => state.isSyncingImageStorage);
  const cleanupRemoteImages = useSettingsStore((state) => state.cleanupRemoteImages);
  const isCleaningRemoteImages = useSettingsStore((state) => state.isCleaningRemoteImages);
  const applyRemoteLifecycle = useSettingsStore((state) => state.applyRemoteLifecycle);
  const isApplyingRemoteLifecycle = useSettingsStore((state) => state.isApplyingRemoteLifecycle);
  const loadRemoteLifecycle = useSettingsStore((state) => state.loadRemoteLifecycle);
  const remoteLifecycle = useSettingsStore((state) => state.remoteLifecycle);
  const saveConfig = useSettingsStore((state) => state.saveConfig);
  const loadConfig = useSettingsStore((state) => state.loadConfig);

  const availableImageModels = config?.image_models || fallbackImageModels;

  // 远端是 S3 时读一次桶上的生命周期规则，展示当前状态
  useEffect(() => {
    if (config?.image_storage?.enabled && config?.image_storage?.provider === "s3") {
      void loadRemoteLifecycle();
    }
  }, [
    config?.image_storage?.enabled,
    config?.image_storage?.provider,
    loadRemoteLifecycle,
  ]);

  const handleSaveImageModels = async () => {
    await saveConfig();
  };

  const handleRefreshImageModels = async () => {
    setIsRefreshingImageModels(true);
    try {
      const result = await refreshImageModelCatalog();
      await loadConfig();
      if (result.refreshed) {
        toast.success(`已从上游更新 ${result.models.length} 个生图模型`);
      } else {
        toast.warning(`拉取失败，继续使用已缓存列表：${result.error || "未知错误"}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "拉取生图模型失败");
    } finally {
      setIsRefreshingImageModels(false);
    }
  };

  const handleTestProxy = async () => {
    const candidate = String(config?.proxy || "").trim();
    if (!candidate) {
      toast.error("请先填写代理地址");
      return;
    }
    setIsTestingProxy(true);
    setProxyTestResult(null);
    try {
      const data = await testProxy(candidate);
      setProxyTestResult(data.result);
      if (data.result.ok) {
        toast.success(`代理可用（${data.result.latency_ms} ms，HTTP ${data.result.status}）`);
      } else {
        toast.error(`代理不可用：${data.result.error ?? "未知错误"}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试代理失败");
    } finally {
      setIsTestingProxy(false);
    }
  };

  if (isLoadingConfig) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex items-center justify-center p-10">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-6">
        <div className="rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 text-sm leading-6 text-stone-600">
          管理员登录密钥继续从部署配置读取，不再在此页面展示；如需分发给其他人，请在下方创建普通用户密钥。
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <label className="text-sm text-stone-700">账号刷新间隔</label>
            <Input
              value={String(config?.refresh_account_interval_minute || "")}
              onChange={(event) => setRefreshAccountIntervalMinute(event.target.value)}
              placeholder="分钟"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位分钟，控制账号自动刷新频率。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">全局代理</label>
            <Input
              value={String(config?.proxy || "")}
              onChange={(event) => {
                setProxy(event.target.value);
                setProxyTestResult(null);
              }}
              placeholder="http://127.0.0.1:7890"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs leading-5 text-stone-500">
              留空表示不使用代理。支持协议://账号:密码@主机:端口，也可直接粘贴代理商的 主机:端口:账号:密码；示例 http://user:pass@127.0.0.1:7890、127.0.0.1:7890:user:pass。账号密码含 @/: 等特殊字符时需 URL 编码。
            </p>
            {proxyTestResult ? (
              <div
                className={`rounded-xl border px-3 py-2 text-xs leading-6 ${
                  proxyTestResult.ok
                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                    : "border-rose-200 bg-rose-50 text-rose-800"
                }`}
              >
                {proxyTestResult.ok
                  ? `代理可用：HTTP ${proxyTestResult.status}，用时 ${proxyTestResult.latency_ms} ms`
                  : `代理不可用：${proxyTestResult.error ?? "未知错误"}（用时 ${proxyTestResult.latency_ms} ms）`}
              </div>
            ) : null}
            <div className="flex justify-end">
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                onClick={() => void handleTestProxy()}
                disabled={isTestingProxy}
              >
                {isTestingProxy ? <LoaderCircle className="size-4 animate-spin" /> : <PlugZap className="size-4" />}
                测试代理
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片访问地址</label>
            <Input
              value={String(config?.base_url || "")}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://example.com"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">用于生成图片结果的访问前缀地址。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片自动清理</label>
            <Input
              value={String(config?.image_retention_days || "")}
              onChange={(event) => setImageRetentionDays(event.target.value)}
              placeholder="30"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">自动删除多少天前的本地图片。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片轮询超时</label>
            <Input
              value={String(config?.image_poll_timeout_secs || "")}
              onChange={(event) => setImagePollTimeoutSecs(event.target.value)}
              placeholder="120"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位秒，等待上游图片结果的最长时间。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">单账号图片并发</label>
            <Input
              value={String(config?.image_account_concurrency || "")}
              onChange={(event) => setImageAccountConcurrency(event.target.value)}
              placeholder="1"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">限制每个账号同时处理的图片请求数量，默认 3。</p>
          </div>
          <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-4 md:col-span-2">
            <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
              <div>
                <label className="text-sm font-medium text-stone-800">生图模型</label>
                <p className="mt-1 text-xs leading-5 text-stone-500">从 ChatGPT 官方上游拉取模型；失败时继续使用最近成功列表或内置回退列表。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                  onClick={() => void handleRefreshImageModels()}
                  disabled={isRefreshingImageModels}
                >
                  <RefreshCw className={`size-4 ${isRefreshingImageModels ? "animate-spin" : ""}`} />
                  拉取最新列表
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                  onClick={() => void handleSaveImageModels()}
                  disabled={isSavingConfig}
                >
                  {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                  保存全局模型
                </Button>
              </div>
            </div>
            <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto]">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">当前使用模型</label>
                <Select
                  value={String(config?.default_image_model || "gpt-image-2")}
                  onValueChange={setDefaultImageModel}
                >
                  <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white">
                    <SelectValue placeholder="选择默认模型" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableImageModels.map((model) => (
                      <SelectItem key={model} value={model}>{model}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs leading-5 text-stone-500">网页生图与省略 model 的图片 API 请求默认使用此模型。</p>
              </div>
              <div className="space-y-1 text-xs leading-5 text-stone-500 md:text-right">
                <p>来源：{config?.image_models_source === "upstream" ? "官方上游" : "历史回退列表"}</p>
                <p>{config?.image_models_updated_at ? `更新时间：${new Date(config.image_models_updated_at).toLocaleString("zh-CN")}` : "尚未成功拉取"}</p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {availableImageModels.map((model) => (
                <span key={model} className="rounded-full bg-stone-100 px-3 py-1 text-xs font-medium text-stone-700">
                  {model}
                </span>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
              <Checkbox
                checked={Boolean(config?.auto_remove_invalid_accounts)}
                onCheckedChange={(checked) => setAutoRemoveInvalidAccounts(Boolean(checked))}
              />
              自动移除异常账号
            </label>
            <p className="text-xs text-stone-500">刷新时检测并移除</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
              <Checkbox
                checked={Boolean(config?.image_settle_enabled !== false)}
                onCheckedChange={(checked) => setImageSettleEnabled(Boolean(checked))}
              />
              <span className="text-sm text-stone-700">图片二次确认机制</span>
            </div>
            <p className="text-xs text-stone-500">打开后能稍微提升获取图片的成功率。</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
              <Checkbox
                checked={Boolean(config?.image_remove_conversation_after_result)}
                onCheckedChange={(checked) => setImageRemoveConversationAfterResult(Boolean(checked))}
              />
              <span className="text-sm text-stone-700">出图后移除本地对话</span>
            </div>
            <p className="text-xs text-stone-500">成功拿到图片后，异步隐藏 ChatGPT 侧对应的本地对话记录。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片超时继续等待时间</label>
            <Input
              value={String(config?.image_timeout_retry_secs || "30")}
              onChange={(event) => setImageTimeoutRetrySecs(event.target.value)}
              placeholder="30"
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">单位秒，超时后点击"继续等待"额外等待的时间。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-stone-700">图片二次确认等待时间</label>
            <Input
              value={String(config?.image_settle_secs || "2.0")}
              onChange={(event) => setImageSettleSecs(event.target.value)}
              placeholder="2.0"
              className="h-10 rounded-xl border-stone-200 bg-white disabled:cursor-not-allowed disabled:opacity-50"
              disabled={!config?.image_settle_enabled}
            />
            <p className="text-xs text-stone-500">单位秒，找到图片后等待多久再次确认。需配合图片二次确认机制使用。</p>
          </div>
          <div className="flex gap-4 md:col-span-2">
            <div className="flex-1 space-y-2">
              <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(config?.auto_relogin_after_refresh)}
                  onCheckedChange={(checked) => setAutoReloginAfterRefresh(Boolean(checked))}
                />
                刷新后自动尝试移除异常状态
              </label>
              <p className="text-xs text-stone-500">开启后刷新时自动尝试密码登录恢复账号。</p>
            </div>
            <div className="flex-1" aria-hidden="true" />
          </div>
          <label className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
            <Checkbox
              checked={Boolean(config?.auto_remove_rate_limited_accounts)}
              onCheckedChange={(checked) => setAutoRemoveRateLimitedAccounts(Boolean(checked))}
            />
            自动移除限流账号
          </label>
          <div className="space-y-3 rounded-xl border border-stone-200 bg-white px-4 py-3">
            <div>
              <label className="text-sm text-stone-700">控制台日志级别</label>
              <p className="mt-1 text-xs text-stone-500">不选择时使用默认 info / warning / error。</p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              {logLevelOptions.map((level) => (
                <label key={level} className="flex items-center gap-2 text-sm capitalize text-stone-700">
                  <Checkbox
                    checked={Boolean(config?.log_levels?.includes(level))}
                    onCheckedChange={(checked) => setLogLevel(level, Boolean(checked))}
                  />
                  {level}
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-stone-700">全局附加指令</label>
            <Textarea
              value={String(config?.global_system_prompt || "")}
              onChange={(event) => setGlobalSystemPrompt(event.target.value)}
              placeholder="例如：先判断用户提示词是否合规；遇到违法、色情、暴力、仇恨等请求时拒绝回答。"
              className="min-h-28 rounded-xl border-stone-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-stone-500">每次请求都会作为 system 消息注入，可用于审核用户提示词、避免违规内容、统一约束模型行为或固定角色设定。</p>
          </div>
          <div className="space-y-2 md:col-span-2">
            <label className="text-sm text-stone-700">敏感词</label>
            <Textarea
              value={(config?.sensitive_words || []).join("\n")}
              onChange={(event) => setSensitiveWordsText(event.target.value)}
              placeholder="一行一个，命中即拒绝"
              className="min-h-28 rounded-xl border-stone-200 bg-white font-mono text-xs shadow-none"
            />
            <p className="text-xs text-stone-500">只要用户请求包含任意敏感词，就直接返回拒绝。</p>
          </div>
          <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-3 md:col-span-2">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <label className="flex items-center gap-3 text-sm text-stone-700">
                <Checkbox
                  checked={Boolean(config?.image_storage?.enabled)}
                  onCheckedChange={(checked) => setImageStorageField("enabled", Boolean(checked))}
                />
                启用远端图片存储
              </label>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                  onClick={() => void testImageStorage()}
                  disabled={isTestingImageStorage || !config?.image_storage?.enabled}
                >
                  {isTestingImageStorage ? <LoaderCircle className="size-4 animate-spin" /> : <Cloud className="size-4" />}
                  测试连接
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-stone-200 bg-white px-4 text-stone-700"
                  onClick={() => void syncImagesToWebDAV()}
                  disabled={isSyncingImageStorage || !config?.image_storage?.enabled || config?.image_storage?.mode === "local"}
                >
                  {isSyncingImageStorage ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  全量同步
                </Button>
              </div>
            </div>
            <p className="text-xs leading-6 text-stone-500">
              生成时只处理本次新图片；全量同步用于把已有本地图片补传到远端。
              远端上传失败会自动降级存到本机，链接照常返回，不会影响出图。
            </p>
            <div className="rounded-lg border border-stone-100 bg-stone-50 px-3 py-2 text-xs text-stone-600">
              当前待保存模式：
              <span className="ml-1 font-medium text-stone-900">
                {config?.image_storage?.enabled
                  ? config.image_storage.mode === "both"
                    ? "本机 + 远端"
                    : config.image_storage.mode === "remote"
                      ? "仅远端"
                      : "仅本机"
                  : "仅本机"}
              </span>
              <span className="ml-2 text-stone-400">修改后需要点保存，或通过测试/同步按钮自动保存。</span>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">远端存储类型</label>
                <Select
                  value={String(config?.image_storage?.provider || "s3")}
                  onValueChange={(value) => setImageStorageField("provider", value as ImageStorageProvider)}
                  disabled={!config?.image_storage?.enabled}
                >
                  <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white shadow-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="s3">Cloudflare R2 / S3 兼容</SelectItem>
                    <SelectItem value="webdav">WebDAV（兼容保留）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">保存模式</label>
                <Select
                  value={String(config?.image_storage?.mode || "local")}
                  onValueChange={(value) => setImageStorageField("mode", value as ImageStorageMode)}
                  disabled={!config?.image_storage?.enabled}
                >
                  <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white shadow-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="local">仅本机</SelectItem>
                    <SelectItem value="remote">仅远端</SelectItem>
                    <SelectItem value="both">本机 + 远端</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">远端目录</label>
                <Input
                  value={String(config?.image_storage?.remote_root_path || "")}
                  onChange={(event) => setImageStorageField("remote_root_path", event.target.value)}
                  placeholder="chatgpt2api/images"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={!config?.image_storage?.enabled}
                />
              </div>
            </div>

            {config?.image_storage?.provider === "webdav" ? (
              <div className="grid gap-4 rounded-lg border border-stone-100 bg-stone-50/60 p-3 md:grid-cols-3">
                <div className="space-y-2 md:col-span-3">
                  <label className="text-sm text-stone-700">WebDAV URL</label>
                  <Input
                    value={String(config?.image_storage?.webdav_url || "")}
                    onChange={(event) => setImageStorageField("webdav_url", event.target.value)}
                    placeholder="https://example.com/dav"
                    className="h-10 rounded-xl border-stone-200 bg-white"
                    disabled={!config?.image_storage?.enabled}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">用户名</label>
                  <Input
                    value={String(config?.image_storage?.webdav_username || "")}
                    onChange={(event) => setImageStorageField("webdav_username", event.target.value)}
                    className="h-10 rounded-xl border-stone-200 bg-white"
                    disabled={!config?.image_storage?.enabled}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">密码</label>
                  <Input
                    type="password"
                    value={String(config?.image_storage?.webdav_password || "")}
                    onChange={(event) => setImageStorageField("webdav_password", event.target.value)}
                    className="h-10 rounded-xl border-stone-200 bg-white"
                    disabled={!config?.image_storage?.enabled}
                  />
                </div>
              </div>
            ) : (
              <div className="grid gap-4 rounded-lg border border-stone-100 bg-stone-50/60 p-3 md:grid-cols-3">
                <div className="space-y-2 md:col-span-3">
                  <label className="text-sm text-stone-700">Endpoint</label>
                  <Input
                    value={String(config?.image_storage?.s3_endpoint || "")}
                    onChange={(event) => setImageStorageField("s3_endpoint", event.target.value)}
                    placeholder="https://<AccountID>.r2.cloudflarestorage.com"
                    className="h-10 rounded-xl border-stone-200 bg-white font-mono text-xs"
                    disabled={!config?.image_storage?.enabled}
                  />
                  <p className="text-xs text-stone-500">
                    Cloudflare R2：控制台 → R2 → 存储桶 → S3 API 凭据，Endpoint 就是
                    https://&lt;AccountID&gt;.r2.cloudflarestorage.com
                  </p>
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">Bucket 名称</label>
                  <Input
                    value={String(config?.image_storage?.s3_bucket || "")}
                    onChange={(event) => setImageStorageField("s3_bucket", event.target.value)}
                    placeholder="my-images"
                    className="h-10 rounded-xl border-stone-200 bg-white"
                    disabled={!config?.image_storage?.enabled}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">Region</label>
                  <Input
                    value={String(config?.image_storage?.s3_region || "auto")}
                    onChange={(event) => setImageStorageField("s3_region", event.target.value)}
                    placeholder="auto"
                    className="h-10 rounded-xl border-stone-200 bg-white"
                    disabled={!config?.image_storage?.enabled}
                  />
                  <p className="text-xs text-stone-500">R2 固定填 auto</p>
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">Access Key ID</label>
                  <Input
                    value={String(config?.image_storage?.s3_access_key_id || "")}
                    onChange={(event) => setImageStorageField("s3_access_key_id", event.target.value)}
                    placeholder="R2 凭据的 Access Key ID"
                    className="h-10 rounded-xl border-stone-200 bg-white font-mono text-xs"
                    disabled={!config?.image_storage?.enabled}
                  />
                </div>
                <div className="space-y-2 md:col-span-2">
                  <label className="text-sm text-stone-700">Secret Access Key</label>
                  <Input
                    type="password"
                    value={String(config?.image_storage?.s3_secret_access_key || "")}
                    onChange={(event) => setImageStorageField("s3_secret_access_key", event.target.value)}
                    placeholder="R2 凭据的 Secret Access Key"
                    className="h-10 rounded-xl border-stone-200 bg-white font-mono text-xs"
                    disabled={!config?.image_storage?.enabled}
                  />
                  <p className="text-xs text-stone-500">
                    只用于该存储桶的 R2 API 凭据，别用主账号 Token；密钥明文保存在 config.json 里。
                  </p>
                </div>
                <div className="space-y-2">
                  <label className="text-sm text-stone-700">寻址方式</label>
                  <Select
                    value={config?.image_storage?.s3_path_style === false ? "virtual" : "path"}
                    onValueChange={(value) => setImageStorageField("s3_path_style", value === "path")}
                    disabled={!config?.image_storage?.enabled}
                  >
                    <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white shadow-none">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="path">Path-style（R2 必须）</SelectItem>
                      <SelectItem value="virtual">Virtual-host style</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            )}

            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2 md:col-span-3">
                <label className="text-sm text-stone-700">公开访问前缀</label>
                <Input
                  value={String(config?.image_storage?.public_base_url || "")}
                  onChange={(event) => setImageStorageField("public_base_url", event.target.value)}
                  placeholder="https://pub-xxxx.r2.dev"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                  disabled={!config?.image_storage?.enabled}
                />
                <p className="text-xs text-stone-500">
                  留空时返回本应用 /images/... 代理地址；填入后直接返回公开图片地址，
                  远端目录会自动拼在后面。R2 建议把桶设为公开读并填 r2.dev 域或自定义域
                  （R2 出网流量免费）。
                </p>
              </div>
            </div>

            <div className="space-y-3 rounded-lg border border-stone-100 bg-stone-50/60 p-3">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <label className="flex items-center gap-3 text-sm text-stone-700">
                  <Checkbox
                    checked={Boolean(config?.image_storage?.remote_cleanup_enabled)}
                    onCheckedChange={(checked) => setImageStorageField("remote_cleanup_enabled", Boolean(checked))}
                    disabled={!config?.image_storage?.enabled}
                  />
                  自动清理远端过期图片
                </label>
                <div className="flex items-center gap-2">
                  <Input
                    type="number"
                    min={1}
                    max={3650}
                    value={Number(config?.image_storage?.remote_retention_days || 1)}
                    onChange={(event) => setImageStorageField("remote_retention_days", Number(event.target.value) || 1)}
                    disabled={!config?.image_storage?.enabled || !config?.image_storage?.remote_cleanup_enabled}
                    className="h-9 w-24 rounded-xl border-stone-200 bg-white"
                  />
                  <span className="text-sm text-stone-600">天后删除</span>
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9 rounded-xl border-stone-200 bg-white px-3 text-stone-700"
                    onClick={() => void applyRemoteLifecycle()}
                    disabled={isApplyingRemoteLifecycle || !config?.image_storage?.enabled}
                  >
                    {isApplyingRemoteLifecycle ? <LoaderCircle className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                    立即应用规则
                  </Button>
                </div>
              </div>
              <p className="text-xs leading-6 text-stone-500">
                保存后自动在桶上下发一条生命周期规则（保留天数 = 上面的天数），
                由 R2 服务端删除过期图片 —— 不占应用资源、没有扫描延迟，服务停了也照清。
                R2 通常在过期时间后 24 小时内完成删除。桶上你其它规则不受影响。
              </p>
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <label className="flex items-center gap-3 text-sm text-stone-700">
                  <Checkbox
                    checked={Boolean(config?.image_storage?.remote_cleanup_fallback)}
                    onCheckedChange={(checked) => setImageStorageField("remote_cleanup_fallback", Boolean(checked))}
                    disabled={!config?.image_storage?.enabled}
                  />
                  额外用应用侧扫描兜底
                </label>
                <Button
                  type="button"
                  variant="outline"
                  className="h-9 rounded-xl border-stone-200 bg-white px-3 text-stone-700"
                  onClick={() => void cleanupRemoteImages()}
                  disabled={isCleaningRemoteImages || !config?.image_storage?.remote_cleanup_fallback}
                >
                  {isCleaningRemoteImages ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  立即扫描清理
                </Button>
              </div>
              <p className="text-xs leading-6 text-stone-500">
                兜底扫描：每 30 分钟列一遍桶、按上传时间删过期对象。
                桶大了很贵也很慢，一般不需要开 —— 服务端规则已经够了。
              </p>
              {remoteLifecycle ? (
                <div className="rounded-md border border-stone-100 bg-white px-3 py-2 text-xs text-stone-600">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-stone-800">桶上当前规则</span>
                    <Button
                      type="button"
                      variant="ghost"
                      className="h-6 px-2 text-xs text-stone-500"
                      onClick={() => void loadRemoteLifecycle()}
                    >
                      <RefreshCw className="size-3" />
                      刷新
                    </Button>
                  </div>
                  {remoteLifecycle.error ? (
                    <p className="mt-1 text-red-600">读取失败：{remoteLifecycle.error}</p>
                  ) : !remoteLifecycle.supported ? (
                    <p className="mt-1 text-stone-500">{remoteLifecycle.reason || "当前远端协议不支持生命周期规则"}</p>
                  ) : remoteLifecycle.rules.length === 0 ? (
                    <p className="mt-1 text-stone-500">桶上还没有生命周期规则。</p>
                  ) : (
                    <ul className="mt-1 space-y-0.5">
                      {remoteLifecycle.rules.map((rule) => (
                        <li key={rule.id || rule.prefix} className="flex flex-wrap gap-2">
                          <span className="text-stone-800">{rule.id || "(无 ID)"}</span>
                          <span className="text-stone-500">
                            前缀 {rule.prefix || "(全部)"} · {rule.days ? `${rule.days} 天后删除` : "无过期条件"}
                          </span>
                          {rule.id === "chatgpt2api-expire" ? (
                            <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700">本应用</span>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : null}
            </div>
          </div>
          <div className="space-y-4 rounded-xl border border-stone-200 bg-white px-4 py-3 md:col-span-2">
            <label className="flex items-center gap-3 text-sm text-stone-700">
              <Checkbox
                checked={Boolean(config?.ai_review?.enabled)}
                onCheckedChange={(checked) => setAIReviewField("enabled", Boolean(checked))}
              />
              启用 AI 审核
            </label>
            <p className="text-xs leading-6 text-stone-500">
              开启后会在请求进入生图账号前先调用审核模型，审核不通过会直接拒绝，减少违规提示词触达账号造成风控或封号的风险。
            </p>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">Base URL</label>
                <Input value={String(config?.ai_review?.base_url || "")} onChange={(event) => setAIReviewField("base_url", event.target.value)} placeholder="https://api.openai.com" className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">API Key</label>
                <Input value={String(config?.ai_review?.api_key || "")} onChange={(event) => setAIReviewField("api_key", event.target.value)} placeholder="sk-..." className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">Model</label>
                <Input value={String(config?.ai_review?.model || "")} onChange={(event) => setAIReviewField("model", event.target.value)} placeholder="gpt-5.4-mini" className="h-10 rounded-xl border-stone-200 bg-white" />
              </div>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">审核提示词</label>
              <Textarea value={String(config?.ai_review?.prompt || "")} onChange={(event) => setAIReviewField("prompt", event.target.value)} placeholder="判断用户请求是否允许。只回答 ALLOW 或 REJECT。" className="min-h-24 rounded-xl border-stone-200 bg-white text-xs shadow-none" />
            </div>
          </div>
        </div>

        <div className="flex justify-end">
          <Button
            className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
            onClick={() => void saveConfig()}
            disabled={isSavingConfig}
          >
            {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
