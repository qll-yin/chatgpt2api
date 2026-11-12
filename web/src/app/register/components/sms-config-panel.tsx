"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Plug, RefreshCw, Save } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  fetchSmsAllCountries,
  fetchSmsConfig,
  fetchSmsCountries,
  saveSmsConfig,
  testSms,
  type SmsConfig,
  type SmsCountry,
} from "@/lib/register-api";

const DEFAULTS: SmsConfig = {
  sms_enabled: "0",
  sms_provider: "smsbower",
  sms_api_key: "",
  sms_country: "52",
  sms_service: "dr",
  sms_max_price: "",
  sms_fixed_price: "",
  sms_reuse_phone: "0",
  sms_phone_success_max: "3",
  sms_auto_country: "0",
  sms_strict_whitelist: "0",
  sms_allowed_countries: "",
  sms_auto_min_stock: "20",
  sms_auto_max_price: "",
  sms_max_phone_attempts: "",
  sms_per_phone_timeout: "80",
};

/**
 * SMS 接码配置。
 *
 * 为什么必须能配：部分账号在 codex auth 阶段会命中 add-phone，
 * 必须接码才能过 —— 接码没配好，取 refresh_token 就会卡在这一步。
 */
export function SmsConfigPanel() {
  const [config, setConfig] = useState<SmsConfig>(DEFAULTS);
  const [saved, setSaved] = useState<SmsConfig>(DEFAULTS);
  const [countries, setCountries] = useState<SmsCountry[]>([]);
  const [countrySource, setCountrySource] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [loadingCountries, setLoadingCountries] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { config: raw } = await fetchSmsConfig();
      const next = { ...DEFAULTS, ...raw };
      setConfig(next);
      setSaved(next);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadCountries = useCallback(async (live: boolean) => {
    setLoadingCountries(true);
    try {
      const data = live
        ? await fetchSmsCountries()
        : await fetchSmsAllCountries(config.sms_provider);
      setCountries(data.countries || []);
      setCountrySource(
        live ? "live" : (data as { source?: string }).source || "static",
      );
      if (!data.countries?.length) {
        toast.info("没有取到国家列表");
      }
    } catch (error) {
      toast.error(`获取国家列表失败：${(error as Error).message ?? error}`);
    } finally {
      setLoadingCountries(false);
    }
  }, [config.sms_provider]);

  const update = <K extends keyof SmsConfig>(key: K, value: SmsConfig[K]) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const toggle = (key: keyof SmsConfig, value: boolean) => {
    update(key, (value ? "1" : "0") as SmsConfig[typeof key]);
  };

  const allowedList = config.sms_allowed_countries
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean);

  const toggleCountry = (id: string) => {
    const next = allowedList.includes(id)
      ? allowedList.filter((c) => c !== id)
      : [...allowedList, id];
    update("sms_allowed_countries", next.join(","));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload: Partial<SmsConfig> = { ...config };
      // api_key 留空 = 不修改（后端见到 '***' 或空会跳过）
      if (!payload.sms_api_key) payload.sms_api_key = "***";
      const data = await saveSmsConfig(payload);
      setConfig({ ...DEFAULTS, ...data.config });
      setSaved({ ...DEFAULTS, ...data.config });
      toast.success("已保存");
    } catch (error) {
      toast.error(`保存失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      const result = await testSms();
      toast.success(result.message || "连接正常");
    } catch (error) {
      toast.error(`测试失败：${(error as Error).message ?? error}`);
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">接码配置</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={config.sms_enabled === "1"}
              onCheckedChange={(v) => toggle("sms_enabled", Boolean(v))}
            />
            启用接码
            <span className="text-xs text-muted-foreground">
              （命中 add-phone 时才会用；不开的话取 refresh_token 遇到要接码的号就会卡住）
            </span>
          </label>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="sms-provider">接码平台</Label>
              <Select
                value={config.sms_provider}
                onValueChange={(v) => update("sms_provider", v)}
              >
                <SelectTrigger id="sms-provider">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="smsbower">SmsBower</SelectItem>
                  <SelectItem value="herosms">HeroSMS</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-key">API Key</Label>
              <Input
                id="sms-key"
                type="password"
                placeholder={saved.sms_api_key === "***" ? "已设置（留空则不修改）" : "粘贴接码平台 API Key"}
                value={config.sms_api_key === "***" ? "" : config.sms_api_key}
                onChange={(e) => update("sms_api_key", e.target.value)}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-service">服务代码</Label>
              <Input
                id="sms-service"
                value={config.sms_service}
                onChange={(e) => update("sms_service", e.target.value)}
                placeholder="dr"
              />
              <p className="text-xs text-muted-foreground">OpenAI 用 dr</p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-country">国家（手动指定）</Label>
              <Input
                id="sms-country"
                value={config.sms_country}
                onChange={(e) => update("sms_country", e.target.value)}
                placeholder="52"
              />
              <p className="text-xs text-muted-foreground">
                52 = 泰国（OpenAI 走 SMS 的稳定国家）
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-max-price">号码最高单价</Label>
              <Input
                id="sms-max-price"
                value={config.sms_max_price}
                onChange={(e) => update("sms_max_price", e.target.value)}
                placeholder="留空 = 不限"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-fixed-price">固定单价</Label>
              <Input
                id="sms-fixed-price"
                value={config.sms_fixed_price}
                onChange={(e) => update("sms_fixed_price", e.target.value)}
                placeholder="留空 = 不限"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-success-max">同号最多复用几次</Label>
              <Input
                id="sms-success-max"
                value={config.sms_phone_success_max}
                onChange={(e) => update("sms_phone_success_max", e.target.value)}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-timeout">单号等待超时（秒）</Label>
              <Input
                id="sms-timeout"
                value={config.sms_per_phone_timeout}
                onChange={(e) => update("sms_per_phone_timeout", e.target.value)}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-attempts">单次注册最多尝试几个号</Label>
              <Input
                id="sms-attempts"
                value={config.sms_max_phone_attempts}
                onChange={(e) => update("sms_max_phone_attempts", e.target.value)}
                placeholder="留空 = 用平台默认"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="sms-min-stock">自动选国家的最低库存</Label>
              <Input
                id="sms-min-stock"
                value={config.sms_auto_min_stock}
                onChange={(e) => update("sms_auto_min_stock", e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-2 border-t pt-3">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={config.sms_reuse_phone === "1"}
                onCheckedChange={(v) => toggle("sms_reuse_phone", Boolean(v))}
              />
              同号复用（省钱）
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={config.sms_auto_country === "1"}
                onCheckedChange={(v) => toggle("sms_auto_country", Boolean(v))}
              />
              自动选最优国家（按价格 + 库存）
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={config.sms_strict_whitelist === "1"}
                onCheckedChange={(v) => toggle("sms_strict_whitelist", Boolean(v))}
              />
              严格白名单（只用 OpenAI 支持的国家，即泰国）
            </label>
          </div>

          <div className="flex flex-wrap justify-end gap-2 border-t pt-3">
            <Button variant="secondary" onClick={handleTest} disabled={testing}>
              {testing ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Plug className="size-4" />
              )}
              测试连通性
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Save className="size-4" />
              )}
              保存
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="h-fit">
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle className="text-base">国家列表</CardTitle>
          <div className="flex gap-1">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void loadCountries(true)}
              disabled={loadingCountries}
            >
              <RefreshCw className="size-4" />
              实时
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void loadCountries(false)}
              disabled={loadingCountries}
            >
              全部
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            {countrySource === "live"
              ? "实时列表（带价格与库存）"
              : countrySource === "static"
                ? "静态字典（平台没配 key 或查询失败时的回落）"
                : "点「实时」或「全部」加载"}
            。勾选后加入自动选号的白名单。
          </p>
          <div className="max-h-[420px] space-y-1 overflow-y-auto">
            {countries.length === 0 ? (
              <p className="py-6 text-center text-xs text-muted-foreground">
                {loadingCountries ? "加载中…" : "还没加载"}
              </p>
            ) : (
              countries.map((c) => {
                const checked = allowedList.includes(c.id);
                return (
                  <label
                    key={c.id}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-stone-100 dark:hover:bg-stone-800"
                  >
                    <Checkbox
                      checked={checked}
                      onCheckedChange={() => toggleCountry(c.id)}
                    />
                    <span className="font-mono">{c.id}</span>
                    <span className="flex-1">{c.name_cn}</span>
                    {c.price != null ? (
                      <span className="text-muted-foreground">
                        {c.price}/{c.count}
                      </span>
                    ) : null}
                    {c.openai_sms_safe ? (
                      <Badge variant="secondary">OpenAI 可用</Badge>
                    ) : null}
                  </label>
                );
              })
            )}
          </div>
          {allowedList.length > 0 ? (
            <p className="text-xs text-muted-foreground">
              白名单：{allowedList.join(", ")}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
