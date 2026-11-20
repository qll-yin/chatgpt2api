"use client";

import { LoaderCircle, PlayCircle, Save, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  fetchFailGuardStatus,
  runFailGuardNow,
  updateSettingsConfig,
  type FailGuardStatus,
  type SettingsConfig,
} from "@/lib/api";

import { useSettingsStore } from "../store";

/**
 * 生成失败巡检：累计失败次数达阈值的账号自动标「异常」。
 *
 * ⚠️ 这里刻意用「局部保存」——只提交这三个字段，不动别的配置。
 * 后端 config.update 是合并语义，不存在的键保持原值。
 */
export function FailGuardCard() {
  const loadConfig = useSettingsStore((state) => state.loadConfig);
  const [status, setStatus] = useState<FailGuardStatus | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [threshold, setThreshold] = useState("20");
  const [intervalMinute, setIntervalMinute] = useState("60");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRunning, setIsRunning] = useState(false);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await fetchFailGuardStatus();
      setStatus(data);
      setEnabled(Boolean(data.enabled));
      setThreshold(String(data.threshold ?? 20));
      setIntervalMinute(String(data.interval_minute ?? 60));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载失败巡检配置失败");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async () => {
    setIsSaving(true);
    try {
      const payload = {
        fail_guard_enabled: enabled,
        fail_guard_threshold: Math.max(1, Number(threshold) || 20),
        fail_guard_interval_minute: Math.max(1, Number(intervalMinute) || 60),
      } as unknown as SettingsConfig;
      await updateSettingsConfig(payload);
      // 让 store 里的全局配置同步，避免别的卡片保存时把这三项覆盖回旧值
      await loadConfig();
      await load();
      toast.success("失败巡检设置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleRun = async () => {
    setIsRunning(true);
    try {
      const result = await runFailGuardNow();
      await load();
      if (result.marked.length === 0) {
        toast.success(`已检测 ${result.checked} 个账号，没有达到阈值的账号`);
      } else {
        toast.warning(`已把 ${result.marked.length} 个账号标为异常`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "执行失败");
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-5 p-6">
        <div className="flex items-start gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-amber-50 text-amber-600">
            <ShieldAlert className="size-5" />
          </div>
          <div className="space-y-1">
            <h2 className="text-base font-semibold">生成失败自动标异常</h2>
            <p className="text-sm text-muted-foreground">
              定时巡检号池里每个账号的<strong>累计生成失败次数</strong>，达到阈值就把状态改成「异常」，
              不再参与取号。额度不会被清零 —— 你手动改回「正常」后可以继续用（累计失败数不清零，
              如果它还是坏的，下一轮会再次被标异常）。
            </p>
          </div>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center p-6">
            <LoaderCircle className="size-5 animate-spin text-stone-400" />
          </div>
        ) : (
          <>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={enabled} onCheckedChange={(checked) => setEnabled(Boolean(checked))} />
              启用定时巡检（默认关闭）
            </label>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label className="text-sm text-muted-foreground" htmlFor="fail-guard-threshold">
                  失败次数阈值
                </label>
                <Input
                  id="fail-guard-threshold"
                  value={threshold}
                  onChange={(event) => setThreshold(event.target.value)}
                  inputMode="numeric"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                />
                <p className="text-xs text-muted-foreground">累计失败 ≥ 该值 → 标异常（默认 20）</p>
              </div>
              <div className="space-y-1.5">
                <label className="text-sm text-muted-foreground" htmlFor="fail-guard-interval">
                  巡检间隔（分钟）
                </label>
                <Input
                  id="fail-guard-interval"
                  value={intervalMinute}
                  onChange={(event) => setIntervalMinute(event.target.value)}
                  inputMode="numeric"
                  className="h-10 rounded-xl border-stone-200 bg-white"
                />
                <p className="text-xs text-muted-foreground">默认 60 分钟一次；改完不用重启，下一轮生效</p>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => void handleSave()} disabled={isSaving} className="h-10 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800">
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
                保存
              </Button>
              <Button variant="outline" onClick={() => void handleRun()} disabled={isRunning} className="h-10 rounded-xl border-stone-200 bg-white">
                {isRunning ? <LoaderCircle className="size-4 animate-spin" /> : <PlayCircle className="size-4" />}
                立即检测一次
              </Button>
              {status?.last?.ran_at ? (
                <span className="text-xs text-muted-foreground">
                  上次检测：{status.last.ran_at}（检查 {status.last.checked} 个，标记 {status.last.marked.length} 个）
                </span>
              ) : null}
            </div>

            {status?.last?.marked?.length ? (
              <div className="rounded-xl bg-stone-50 p-3 text-xs text-stone-600 dark:bg-white/5">
                <div className="mb-1 font-medium text-stone-700 dark:text-stone-200">上次被标记的账号</div>
                <ul className="space-y-0.5">
                  {status.last.marked.slice(0, 10).map((item) => (
                    <li key={`${item.email}-${item.fail}`}>
                      {item.email || "(无邮箱)"} · 失败 {item.fail} 次
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
