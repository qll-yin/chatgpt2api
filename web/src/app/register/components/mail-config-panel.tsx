"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Save } from "lucide-react";
import { toast } from "sonner";

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
  fetchMailProviders,
  fetchRegisterSettings,
  saveRegisterSettings,
  type MailProvider,
} from "@/lib/register-api";

/**
 * 邮箱配置。
 *
 * 表单按 provider 自己声明的 config_fields 动态渲染 ——
 * 加一种邮箱不用改前端。
 */
export function MailConfigPanel() {
  const [providers, setProviders] = useState<MailProvider[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [mailSource, setMailSource] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [{ providers: list }, settings] = await Promise.all([
        fetchMailProviders(),
        fetchRegisterSettings(),
      ]);
      setProviders(list);
      setValues(settings.values || {});
      setMailSource(settings.values?.mail_source || list[0]?.kind || "");
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
      await saveRegisterSettings(payload);
      toast.success("已保存");
    } catch (error) {
      toast.error(`保存失败：${(error as Error).message ?? error}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">邮箱来源</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label>当前来源</Label>
            <Select value={mailSource} onValueChange={setMailSource}>
              <SelectTrigger>
                <SelectValue placeholder="选择邮箱来源" />
              </SelectTrigger>
              <SelectContent>
                {providers.map((p) => (
                  <SelectItem key={p.kind} value={p.kind}>
                    {p.display_name}
                    {p.ephemeral ? "（短效）" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {current?.ephemeral ? (
            <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
              这是短效（一次性）邮箱：只保留一封邮件，取不到验证码就会失效退款。
              批量注册时不要勾选「获取 refresh_token」。
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
                  <Label htmlFor={`mail-${field.key}`}>{field.label || field.key}</Label>
                  <Input
                    id={`mail-${field.key}`}
                    type={field.secret ? "password" : field.type === "number" ? "number" : "text"}
                    placeholder={field.placeholder}
                    value={values[field.key] ?? ""}
                    onChange={(e) => setValue(field.key, e.target.value)}
                  />
                </div>
              ))}
            </div>
          )}

          <div className="flex justify-end border-t pt-3">
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
