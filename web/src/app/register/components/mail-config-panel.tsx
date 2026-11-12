"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Plug, Save } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  fetchMailConfig,
  fetchMailProviders,
  saveMailConfig,
  testMailConnection,
  type MailProvider,
} from "@/lib/register-api";

/**
 * 邮箱配置。
 *
 * 表单按 provider 自己声明的 config_fields 动态渲染 ——
 * 加一种邮箱不用改前端。
 *
 * 关于「长效 / 短效」：这不是 provider 的固有属性，而是**配置项的结果**
 * （Remail 的 purchase = 长效可重复收件、code = 短效一次性）。
 * 所以这里不硬编码任何「这是短效邮箱」的结论 —— 用户在「服务模式」
 * 下拉框里选哪个，就决定了这个来源当下的行为，旁边的说明文字会跟着变。
 */
export function MailConfigPanel() {
  const [providers, setProviders] = useState<MailProvider[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [mailSource, setMailSource] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [{ providers: list }, { config }] = await Promise.all([
        fetchMailProviders(),
        fetchMailConfig(),
      ]);
      setProviders(list);
      setSaved(config || {});
      setMailSource(config?.mail_source || list[0]?.kind || "");
      // 密码类字段回填成空 —— 后端返回的是 '***' 掩码，
      // 填进去会把真值覆盖掉
      const next: Record<string, string> = { ...(config || {}) };
      for (const p of list) {
        for (const f of p.config_fields ?? []) {
          if (f.type === "password") next[f.key] = "";
          // select 没存过值时默认选第一项，省得每次手动选
          if (f.type === "select" && !next[f.key] && f.options?.length) {
            next[f.key] = f.options[0];
          }
        }
      }
      setValues(next);
    } catch (error) {
      toast.error(`加载失败：${(error as Error).message ?? error}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const current = providers.find((p) => p.kind === mailSource);
  const fields = current?.config_fields ?? [];

  const setValue = (key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload: Record<string, string> = { ...values, mail_source: mailSource };
      // 密码字段留空 = 不修改（后端见到 '***' 或空会跳过）
      await saveMailConfig(payload);
      toast.success("已保存");
      await load();
    } catch (error) {
      toast.error(`保存失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      const result = await testMailConnection();
      toast.success(result.message || "连通正常");
    } catch (error) {
      toast.error(`测试失败：${(error as Error).message ?? error}`);
    } finally {
      setTesting(false);
    }
  };

  const missing = fields
    .filter((f) => f.required)
    .filter((f) => {
      const v = (values[f.key] ?? "").trim();
      if (v) return false;
      // 密码类：已存过（后端返回 '***'）就视为已填
      if (f.type === "password" && saved[f.key] === "***") return false;
      return true;
    });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">邮箱来源</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label>当前来源</Label>
            <Select
              value={mailSource || providers[0]?.kind || "__none__"}
              onValueChange={setMailSource}
            >
              <SelectTrigger>
                <SelectValue placeholder="选择邮箱来源" />
              </SelectTrigger>
              <SelectContent>
                {providers.map((p) => (
                  <SelectItem key={p.kind} value={p.kind}>
                    {p.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* 能力说明：来自 provider 自己的声明，不是猜的 */}
          {current ? (
            <div className="flex flex-wrap gap-2">
              <Badge variant={current.pooled ? "outline" : "secondary"}>
                {current.pooled ? "号池型：需先导入号，用完要补" : "自建型：自动生成地址"}
              </Badge>
              <Badge variant="outline">
                {current.ephemeral ? "每次新地址" : "固定地址"}
              </Badge>
            </div>
          ) : null}

          {current?.pooled ? (
            <p className="rounded-md bg-stone-100 p-2 text-xs text-stone-700 dark:bg-stone-900 dark:text-stone-300">
              {current.display_name} 不需要在这里配置，去「批量注册」里把号导进邮箱池即可。
            </p>
          ) : null}

          {fields.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {loading ? "加载中…" : "该来源没有需要配置的项"}
            </p>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {fields.map((field) => (
                <div key={field.key} className="space-y-1.5">
                  <Label htmlFor={`mail-${field.key}`}>
                    {field.label || field.key}
                    {field.required ? (
                      <span className="ml-1 text-red-500">*</span>
                    ) : null}
                  </Label>

                  {/* type=select 的配置项（如 Remail 的服务模式 / 库存策略）
                      用下拉框，候选项由 provider 在 config_fields[].options 里声明 */}
                  {field.type === "select" ? (
                    <Select
                      // ⚠️ Radix 的 Select 不接受空串 value —— 会抛错并让整棵树崩掉。
                      //    没存过值时选第一个候选项。
                      value={values[field.key] || field.options?.[0] || "__none__"}
                      onValueChange={(v) => setValue(field.key, v)}
                    >
                      <SelectTrigger id={`mail-${field.key}`}>
                        <SelectValue placeholder={field.placeholder || "请选择"} />
                      </SelectTrigger>
                      <SelectContent>
                        {(field.options ?? [])
                          .filter((opt) => opt !== "")
                          .map((opt) => (
                            <SelectItem key={opt} value={opt}>
                              {opt}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      id={`mail-${field.key}`}
                      type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
                      placeholder={
                        field.type === "password" && saved[field.key] === "***"
                          ? "已设置（留空则不修改）"
                          : field.placeholder
                      }
                      value={values[field.key] ?? ""}
                      onChange={(e) => setValue(field.key, e.target.value)}
                    />
                  )}

                  {field.help ? (
                    <p className="text-xs text-muted-foreground">{field.help}</p>
                  ) : null}
                </div>
              ))}
            </div>
          )}

          {missing.length > 0 ? (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              还没填：{missing.map((f) => f.label || f.key).join("、")}
            </p>
          ) : null}

          <div className="flex justify-end gap-2 border-t pt-3">
            {current && !current.pooled ? (
              <Button variant="secondary" onClick={handleTest} disabled={testing}>
                {testing ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <Plug className="size-4" />
                )}
                测试连通性
              </Button>
            ) : null}
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
    </div>
  );
}
