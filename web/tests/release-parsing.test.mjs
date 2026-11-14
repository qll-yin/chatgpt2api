import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

function loadParseChangelog() {
  const source = readFileSync(new URL("../src/lib/release.ts", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
  });
  const exports = {};
  vm.runInNewContext(outputText, {
    exports,
    module: { exports },
  });
  return exports.parseChangelog;
}

const parseChangelog = loadParseChangelog();

test("版本号与日期要拆开", () => {
  const releases = parseChangelog(
    ["# Changelog", "", "## 1.11.0 - 2026-11-14", "", "+ [新增] 某某功能", ""].join("\n"),
  );
  assert.equal(releases.length, 1);
  assert.equal(releases[0].version, "1.11.0");
  assert.equal(releases[0].date, "2026-11-14");
  assert.equal(releases[0].items.length, 1);
  assert.equal(releases[0].items[0].type, "新增");
});

test("版本号要能与纯版本号比较（徽章靠这个匹配）", () => {
  // 「最新」「当前」徽章比的是 webConfig.appVersion（纯版本号，如 "1.11.0"）。
  // 解析结果里若带着日期，就永远匹配不上。
  const releases = parseChangelog(
    ["## 1.11.0 - 2026-11-14", "+ [新增] x", "", "## 1.10.0 - 2026-11-11", "+ [修复] y"].join("\n"),
  );
  assert.equal(releases[0].version, "1.11.0");
  assert.equal(releases[1].version, "1.10.0");
  assert.ok(releases.some((r) => r.version === "1.11.0"));
});

test("没有日期的标题整串当版本号", () => {
  const releases = parseChangelog(["## Unreleased", "+ [新增] x"].join("\n"));
  assert.equal(releases[0].version, "Unreleased");
  assert.equal(releases[0].date, "");
});

test("没有条目的版本段会被过滤掉", () => {
  const releases = parseChangelog(["## 1.9.0 - 2026-01-01", "", "## 1.8.0 - 2025-12-01", "+ [修复] y"].join("\n"));
  assert.equal(releases.length, 1);
  assert.equal(releases[0].version, "1.8.0");
});

test("条目类型与内容都能取到", () => {
  const releases = parseChangelog(
    ["## 1.0.0 - 2026-01-01", "+ [新增] 内容一", "+ [修复] 内容二", "+ [优化] 内容三"].join("\n"),
  );
  // vm 沙箱里的数组来自另一个 realm，deepEqual 会因原型不同而失败 → 逐项断言
  assert.equal(releases[0].items.length, 3);
  assert.equal(releases[0].items[0].type, "新增");
  assert.equal(releases[0].items[1].type, "修复");
  assert.equal(releases[0].items[2].type, "优化");
  assert.equal(releases[0].items[1].content, "内容二");
});

test("CRLF 行尾不能把版本号解析坏", () => {
  // ⚠️ 仓库在 Windows 上 checkout 出来是 CRLF，标题行会带一个 "\r"。
  //    正则里的 "." 不匹配行终止符 —— 不 trim 的话整行标题会塞进 version，
  //    date 恒为空，前端「最新」「当前」徽章就永远匹配不上。
  const releases = parseChangelog(
    ["## 1.11.0 - 2026-11-14\r", "+ [新增] x\r"].join("\n"),
  );
  assert.equal(releases[0].version, "1.11.0");
  assert.equal(releases[0].date, "2026-11-14");
});

test("解析仓库真实的 CHANGELOG：首条版本号不带日期", () => {
  const raw = readFileSync(new URL("../../CHANGELOG.md", import.meta.url), "utf8");
  const releases = parseChangelog(raw);
  assert.ok(releases.length > 0);
  assert.ok(!releases[0].version.includes("-"), "版本号里不该带日期");
  assert.ok(/^\d+\.\d+\.\d+$/.test(releases[0].version), "版本号格式应为 x.y.z");
  assert.match(releases[0].date, /^\d{4}-\d{2}-\d{2}$/);
});
