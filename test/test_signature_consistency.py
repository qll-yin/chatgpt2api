"""静态扫描：模块级函数调用与定义签名是否一致。

⚠️ 为什么需要这个测试
-------------------
从旧注册机合并过来时，改过大量函数签名（比如 finish_run 的
``status: str`` 改成 ``ok: bool``），但**调用点没跟着改**。

这类 bug 极其隐蔽：
  · 语法完全正常，类型检查也过（Python 没类型检查）
  · 只在**那条代码路径**被走到时才抛 TypeError
  · 抛出来的异常还可能被上层 try/except 吞掉

实际事故：``db.finish_run(run_id, "failed", err, category=category)``
抛 ``TypeError: got an unexpected keyword argument 'category'``，
于是 run 永远停在 "running"，worker 死等 1800 秒，
前端一直显示「运行中」—— 看着像"在跑"，其实什么都没发生。

所以这里用 AST 把所有调用点跟定义对一遍，签名对不上直接失败。
"""
from __future__ import annotations

import ast
import inspect
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 需要检查的模块 → 调用时用的别名
TARGETS = {
    "services.storage.register_store": "db",
    "services.storage.db": "db",
    "services.register_registrar": "registrar",
    "services.register_service": "register_service",
    "services.register_batch": "register_batch",
    "services.proxy_test": "proxy_test",
    "services.account_service": "account_service",
}


def _iter_source_files() -> list[Path]:
    out: list[Path] = []
    for rel in ("services", "api", "utils"):
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            out.append(path)
    main = ROOT / "main.py"
    if main.is_file():
        out.append(main)
    return out


def _collect_definitions() -> dict[tuple[str, str], inspect.Signature]:
    defs: dict[tuple[str, str], inspect.Signature] = {}
    for mod in TARGETS:
        try:
            module = __import__(mod, fromlist=["*"])
        except Exception:  # noqa: BLE001
            continue
        for name, obj in vars(module).items():
            if not inspect.isfunction(obj):
                continue
            try:
                defs[(mod, name)] = inspect.signature(obj)
            except (ValueError, TypeError):
                continue
    return defs


class SignatureConsistencyTests(unittest.TestCase):
    def test_no_signature_mismatch(self):
        defs = _collect_definitions()
        self.assertTrue(defs, "没收集到任何函数定义，检查器本身坏了")

        problems: list[str] = []
        for path in _iter_source_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            except SyntaxError:
                continue
            alias_map = _build_alias_map(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                    continue
                alias = func.value.id
                target_mod = alias_map.get(alias)
                if target_mod not in TARGETS:
                    continue
                sig = defs.get((target_mod, func.attr))
                if sig is None:
                    continue
                msg = _check_call(node, sig)
                if msg:
                    rel = path.relative_to(ROOT).as_posix()
                    problems.append(f"{rel}:{node.lineno}  {alias}.{func.attr}()  →  {msg}")

        if problems:
            self.fail(
                "发现 %d 处调用与定义签名不匹配（改签名时漏改调用点）：\n  %s"
                % (len(problems), "\n  ".join(sorted(set(problems))))
            )


def _build_alias_map(tree: ast.AST) -> dict[str, str]:
    """模块别名 → 绝对模块路径。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    out[alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = f"{node.module}.{alias.name}"
                else:
                    out[alias.name] = f"{node.module}.{alias.name}"
    return out


def _check_call(node: ast.Call, sig: inspect.Signature) -> str:
    params = list(sig.parameters.values())
    positional = list(node.args)
    keywords = {k.arg: k for k in node.keywords if k.arg}
    has_star_kwargs = any(k.arg is None for k in node.keywords)
    has_var_positional = any(p.kind == p.VAR_POSITIONAL for p in params)
    has_var_keyword = any(p.kind == p.VAR_KEYWORD for p in params)

    if not has_var_keyword and not has_star_kwargs:
        for name in keywords:
            if name not in sig.parameters:
                return f"多余关键字 '{name}'"

    # 位置参数上限 = 最后一个 positional/keyword-only 之前的参数个数
    max_positional = len([
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ])
    if not has_var_positional and not has_star_kwargs:
        if len(positional) > max_positional:
            return (
                f"位置参数过多：传了 {len(positional)} 个，"
                f"定义只接受 {max_positional} 个位置参数"
            )

    # 同一个参数既按位置又按关键字传
    for index, param in enumerate(params):
        if index < len(positional) and param.name in keywords:
            return f"'{param.name}' 同时按位置和关键字传"

    # 必需参数缺失
    for index, param in enumerate(params):
        if param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            continue
        if index < len(positional):
            continue
        if param.name in keywords:
            continue
        if param.default is not param.empty:
            continue
        return f"缺参数 '{param.name}'"

    for param in params:
        if param.kind == param.KEYWORD_ONLY and param.default is param.empty:
            if param.name not in keywords:
                return f"缺关键字参数 '{param.name}'"


    return ""


if __name__ == "__main__":
    unittest.main()
