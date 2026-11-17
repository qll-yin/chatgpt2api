"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Play, Save, Zap } from "lucide-react";
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
  checkAutoRegisterNow,
  fetchAutoRegisterSettings,
  fetchMailProviders,
  saveAutoRegisterSettings,
  triggerAutoRegisterNow,
  type AutoRegisterSettings,
  type AutoRegisterState,
  type MailProvider,
  type PoolProxyMode,
} from "@/lib/register-api";

const DEFAULTS: AutoRegisterSettings = {
  enabled: false,
  threshold: 10,
  target_batch: 20,
  check_interval_minute: 30,
  concurrency: 3,
  mail_source: "",
  register_options: {},
  cooldown_minute: 30,
  max_retries: 3,
  quota_threshold: 0,
  pool_proxy_mode: "static" as PoolProxyMode,
};

export function AutoRegisterPanel() {
  const [settings, setSettings] = useState<AutoRegisterSettings>(DEFAULTS);
  const [providers, setProviders] = useState<MailProvider[]>([]);
  const [state, setState] = useState<AutoRegisterState | null>(null);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchAutoRegisterSettings();
      setSettings({ ...DEFAULTS, ...data.settings });
      setState(data.state);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  // 邮箱来源清单 —— 守护触发注册时也要知道用哪个来源
  useEffect(() => {
    void (async () => {
      try {
        const { providers: list } = await fetchMailProviders();
        setProviders([...list].sort((a, b) => Number(b.ready) - Number(a.ready)));
      } catch {
        /* 忽略 */
      }
    })();
  }, []);

  const update = <K extends keyof AutoRegisterSettings>(
    key: K,
    value: AutoRegisterSettings[K],
  ) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const data = await saveAutoRegisterSettings(settings);
      setSettings({ ...DEFAULTS, ...data.settings });
      toast.success("已保存，守护进程会立即重新检查");
    } catch (error) {
      toast.error(`保存失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  const handleCheck = async () => {
    setChecking(true);
    try {
      const result = await checkAutoRegisterNow();
      toast.success(
        result.skipped
          ? `未触发：${result.reason ?? ""}`
          : result.triggered
            ? "已触发注册"
            : "触发失败",
      );
      await load();
    } catch (error) {
      toast.error(`检查失败：${(error as Error).message ?? error}`);
    } finally {
      setChecking(false);
    }
  };

  const handleTrigger = async () => {
    setChecking(true);
    try {
      const result = await triggerAutoRegisterNow();
      if (result.ok) toast.success("已触发注册");
      else toast.error(result.error || "触发失败");
      await load();
    } catch (error) {
      toast.error(`触发失败：${(error as Error).message ?? error}`);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_minmax(280px,360px)]">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">号池自动补充</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            定时检查号池里的可用账号数，低于阈值时自动触发注册，让号池维持在目标水位。
            注册能力就在本服务内（进程内直接启动批量注册），不依赖任何外部注册机服务。
          </p>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={settings.enabled}
              onCheckedChange={(v) => update("enabled", Boolean(v))}
            />
            启用自动补充
          </label>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="auto-threshold">可用账号低于此值触发</Label>
              <Input
                id="auto-threshold"
                type="number"
                min={0}
                value={settings.threshold}
                onChange={(e) => update("threshold", Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-quota-threshold">剩余额度低于此值触发（0 = 不启用）</Label>
              <Input
                id="auto-quota-threshold"
                type="number"
                min={0}
                value={settings.quota_threshold}
                onChange={(e) => update("quota_threshold", Number(e.target.value))}
              />
              <p className="text-xs text-muted-foreground">
                与号池管理页的「剩余额度」同口径。账号数达标但额度不达标时，同样会触发。
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-pool-proxy">入池绑什么代理</Label>
              <select
                id="auto-pool-proxy"
                className="h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm text-stone-700 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
                value={settings.pool_proxy_mode}
                onChange={(e) =>
                  update("pool_proxy_mode", e.target.value as PoolProxyMode)
                }
              >
                <option value="static">入池时携带静态代理</option>
                <option value="register">入池时携带注册所用代理</option>
                <option value="none">入池不带代理</option>
              </select>
              <p className="text-xs text-muted-foreground">
                静态代理池里没有可用代理时以空代理入池。动态代理不会进号池。
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-batch">每次注册成功数</Label>
              <Input
                id="auto-batch"
                type="number"
                min={1}
                value={settings.target_batch}
                onChange={(e) => update("target_batch", Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-concurrency">注册并发数</Label>
              <Input
                id="auto-concurrency"
                type="number"
                min={1}
                max={20}
                value={settings.concurrency}
                onChange={(e) => update("concurrency", Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-source">注册用的邮箱来源</Label>
              <Select
                value={settings.mail_source || "__default__"}
                onValueChange={(v) =>
                  update("mail_source", v === "__default__" ? "" : v)
                }
              >
                <SelectTrigger id="auto-source">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__default__">跟随「邮箱配置」</SelectItem>
                  {providers.map((p) => (
                    <SelectItem key={p.kind} value={p.kind} disabled={!p.ready}>
                      {p.display_name}
                      {p.ready ? "" : "（未就绪）"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                守护触发批量注册时用哪个邮箱来源。留空则用「邮箱配置」里选的那个。
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-interval">检测间隔（分钟）</Label>
              <Input
                id="auto-interval"
                type="number"
                min={1}
                value={settings.check_interval_minute}
                onChange={(e) => update("check_interval_minute", Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-cooldown">触发后冷却（分钟）</Label>
              <Input
                id="auto-cooldown"
                type="number"
                min={0}
                value={settings.cooldown_minute}
                onChange={(e) => update("cooldown_minute", Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="auto-retries">失败重试次数</Label>
              <Input
                id="auto-retries"
                type="number"
                min={0}
                max={20}
                value={settings.max_retries}
                onChange={(e) => update("max_retries", Number(e.target.value))}
              />
            </div>
          </div>

          <div className="flex flex-wrap justify-end gap-2 border-t pt-3">
            <Button variant="secondary" onClick={handleCheck} disabled={checking}>
              {checking ? <LoaderCircle className="size-4 animate-spin" /> : <Zap className="size-4" />}
              立即检查
            </Button>
            <Button variant="secondary" onClick={handleTrigger} disabled={checking}>
              <Play className="size-4" />
              强制触发
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">守护状态</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Badge variant={settings.enabled ? "secondary" : "outline"}>
              {settings.enabled ? "已启用" : "未启用"}
            </Badge>
            {state?.last_error ? (
              <span className="text-xs text-destructive">{state.last_error}</span>
            ) : null}
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
            <dt className="text-muted-foreground">上次检查</dt>
            <dd className="text-right tabular-nums">
              {state?.last_check_at ? new Date(state.last_check_at * 1000).toLocaleTimeString() : "—"}
            </dd>
            <dt className="text-muted-foreground">检查时可用数</dt>
            <dd className="text-right tabular-nums">{state?.last_check_active ?? "—"}</dd>
            <dt className="text-muted-foreground">上次触发</dt>
            <dd className="text-right tabular-nums">
              {state?.last_trigger_at ? new Date(state.last_trigger_at * 1000).toLocaleTimeString() : "—"}
            </dd>
            <dt className="text-muted-foreground">触发次数</dt>
            <dd className="text-right tabular-nums">{state?.trigger_count ?? 0}</dd>
            <dt className="text-muted-foreground">上次结果</dt>
            <dd className="text-right text-xs text-muted-foreground">
              {state?.last_trigger_result || "—"}
            </dd>
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}
