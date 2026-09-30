#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板展开工具（纯 Python 标准库，单文件）

支持的三种构造
--------------
1. 变量引用：  {变量名}                例如：{收件人}、{日期}
2. 条件段：    {#if 变量名} ... {/if}  变量为真时展开，否则整段丢弃
3. 循环段：    {#each 列表变量} ... {/each}
               循环体内用 {item} 引用当前项，{index} 引用序号（从 0 开始）

其他规则
--------
- 变量值本身可以引用其他变量，逐层递归展开；
- 循环引用（a 引用 b、b 又引用 a）会报错，不会死循环；
- 条件段 / 循环段未闭合时，报告起始标记的位置（行:列）；
- 引用未定义变量时，报告变量名与位置；
- 字面量花括号用 {{ 和 }} 转义；
- 变量定义来自 JSON 文件（对象）：值可以是字符串、数字、布尔、列表、null。

用法
----
    python3 template_expand.py 模板文件 变量定义.json [-o 输出文件]
    python3 template_expand.py --demo          # 运行内置示例（含错误示例）

输入输出示例
------------
模板（template.txt）：

    尊敬的{收件人}：

    {问候语}
    {#if 是否会员}您是尊贵的会员，本次账单享受 9 折优惠。
    {/if}账单明细：
    {#each 账单项目}  {index}. {item}
    {/each}合计：{合计} 元
    日期：{日期}

变量（vars.json）：

    {
      "收件人": "张三",
      "问候语": "您好，{收件人}！以下是您本月的账单。",
      "是否会员": true,
      "账单项目": ["水费 30 元", "电费 52 元"],
      "合计": 82,
      "日期": "2026-09-30"
    }

运行：python3 template_expand.py template.txt vars.json

输出：

    尊敬的张三：

    您好，张三！以下是您本月的账单。
    您是尊贵的会员，本次账单享受 9 折优惠。
    账单明细：
      0. 水费 30 元
      1. 电费 52 元
    合计：82 元
    日期：2026-09-30

若有错误，展开结果照常输出（出错部分按空处理），错误清单打印到 stderr，
并以退出码 1 结束。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 错误与语法树节点
# ---------------------------------------------------------------------------

@dataclass
class TemplateError:
    source: str   # 出错位置所在的来源（模板文件 / 某个变量的值）
    line: int
    col: int
    message: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}:{self.col}: {self.message}"


@dataclass
class Text:
    text: str


@dataclass
class Var:
    name: str
    source: str
    line: int
    col: int


@dataclass
class If:
    name: str
    body: list
    source: str
    line: int
    col: int


@dataclass
class Each:
    name: str
    body: list
    source: str
    line: int
    col: int


@dataclass
class _Frame:
    kind: str                 # 'root' | 'if' | 'each'
    name: str
    line: int
    col: int
    children: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 解析器：文本 -> 语法树，同时收集语法错误（未闭合、标记不匹配等）
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, text: str, source: str, errors: list):
        self.text = text
        self.source = source
        self.errors = errors
        self.frames = [_Frame("root", "", 0, 0)]
        self.buf = []
        self.i = 0
        self.line = 1
        self.col = 1

    def _error(self, line: int, col: int, message: str) -> None:
        self.errors.append(TemplateError(self.source, line, col, message))

    def _flush(self) -> None:
        if self.buf:
            self.frames[-1].children.append(Text("".join(self.buf)))
            self.buf.clear()

    def parse(self) -> list:
        text = self.text
        n = len(text)
        while self.i < n:
            ch = text[self.i]
            if ch == "{" and self.i + 1 < n and text[self.i + 1] == "{":
                self.buf.append("{")
                self.i += 2
                self.col += 2
                continue
            if ch == "}" and self.i + 1 < n and text[self.i + 1] == "}":
                self.buf.append("}")
                self.i += 2
                self.col += 2
                continue
            if ch == "{":
                self._parse_tag()
                continue
            self.buf.append(ch)
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.i += 1
        self._flush()
        for frame in self.frames[1:]:
            if frame.kind == "if":
                opener, desc = f"{{#if {frame.name}}}", "条件段"
            else:
                opener, desc = f"{{#each {frame.name}}}", "循环段"
            self._error(frame.line, frame.col,
                        f"{desc} '{opener}' 未闭合（缺少对应的 '{{/{frame.kind}}}'）")
        return self.frames[0].children

    def _parse_tag(self) -> None:
        text = self.text
        tag_line, tag_col = self.line, self.col
        end = text.find("}", self.i + 1)
        if end == -1:
            self._error(tag_line, tag_col, "花括号 '{' 未闭合")
            self.buf.append("{")
            self.i += 1
            self.col += 1
            return
        tag = text[self.i + 1:end].strip()
        segment = text[self.i:end + 1]
        newlines = segment.count("\n")
        if newlines:
            self.line += newlines
            self.col = len(segment) - segment.rfind("\n")
        else:
            self.col += len(segment)
        self.i = end + 1
        self._handle_tag(tag, tag_line, tag_col)

    def _handle_tag(self, tag: str, line: int, col: int) -> None:
        if not tag:
            self._error(line, col, "空标记 '{}'")
            return
        parts = tag.split(None, 1)
        head = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if head in ("#if", "#each"):
            kind = head[1:]
            if not rest:
                desc = "条件段" if kind == "if" else "循环段"
                self._error(line, col, f"{desc}标记 '{{{head}}}' 缺少变量名")
                return
            self._flush()
            self.frames.append(_Frame(kind, rest, line, col))
        elif tag in ("/if", "/each"):
            kind = tag[1:]
            if len(self.frames) > 1 and self.frames[-1].kind == kind:
                self._flush()
                frame = self.frames.pop()
                if kind == "if":
                    node = If(frame.name, frame.children, self.source, frame.line, frame.col)
                else:
                    node = Each(frame.name, frame.children, self.source, frame.line, frame.col)
                self.frames[-1].children.append(node)
            else:
                self._error(line, col, f"闭合标记 '{{{tag}}}' 没有匹配的起始标记")
        elif head.startswith("#"):
            self._error(line, col, f"无法识别的标记 '{{{tag}}}'")
        elif head.startswith("/"):
            self._error(line, col, f"闭合标记 '{{{tag}}}' 没有匹配的起始标记")
        else:
            if any(c.isspace() for c in tag):
                self._error(line, col, f"变量名中不允许出现空白字符: '{{{tag}}}'")
                return
            self._flush()
            self.frames[-1].children.append(Var(tag, self.source, line, col))


# ---------------------------------------------------------------------------
# 展开器：语法树 + 变量定义 -> 输出文本，同时收集求值错误
# ---------------------------------------------------------------------------

_MISSING = object()


class Expander:
    def __init__(self, variables: dict):
        self.variables = variables
        self.errors: list[TemplateError] = []
        self._cache: dict = {}

    def expand(self, template_text: str, source: str = "<模板>") -> str:
        nodes = _Parser(template_text, source, self.errors).parse()
        return self._expand_nodes(nodes, [], ())

    def _expand_nodes(self, nodes: list, scopes: list, resolving: tuple) -> str:
        out = []
        for node in nodes:
            if isinstance(node, Text):
                out.append(node.text)
            elif isinstance(node, Var):
                value = self._resolve(node, scopes, resolving)
                if value is not None:
                    out.append(value)
            elif isinstance(node, If):
                raw = self._lookup(node.name, node.source, node.line, node.col, scopes)
                if raw is _MISSING:
                    continue
                if self._truthy(raw, node, scopes, resolving):
                    out.append(self._expand_nodes(node.body, scopes, resolving))
            elif isinstance(node, Each):
                raw = self._lookup(node.name, node.source, node.line, node.col, scopes)
                if raw is _MISSING:
                    continue
                if not isinstance(raw, list):
                    self.errors.append(TemplateError(
                        node.source, node.line, node.col,
                        f"变量 '{node.name}' 不是列表，无法用于循环段"))
                    continue
                for idx, item in enumerate(raw):
                    scope = {"item": item, "index": idx}
                    out.append(self._expand_nodes(node.body, scopes + [scope], resolving))
        return "".join(out)

    def _lookup(self, name, source, line, col, scopes):
        for scope in reversed(scopes):          # 循环局部变量优先
            if name in scope:
                return scope[name]
        if name in self.variables:
            return self.variables[name]
        self.errors.append(TemplateError(source, line, col,
                                         f"引用了未定义的变量 '{name}'"))
        return _MISSING

    def _resolve(self, node: Var, scopes, resolving):
        raw = self._lookup(node.name, node.source, node.line, node.col, scopes)
        if raw is _MISSING:
            return None
        return self._to_string(raw, node.name, node.source, node.line, node.col,
                               scopes, resolving)

    def _to_string(self, raw, name, source, line, col, scopes, resolving):
        if isinstance(raw, str):
            if name in resolving:
                chain = " -> ".join(list(resolving) + [name])
                self.errors.append(TemplateError(source, line, col,
                                                 f"检测到循环引用: {chain}"))
                return None
            key = (name, raw)
            nodes = self._cache.get(key)
            if nodes is None:
                nodes = _Parser(raw, f"<变量 {name}>", self.errors).parse()
                self._cache[key] = nodes
            return self._expand_nodes(nodes, scopes, resolving + (name,))
        if isinstance(raw, bool):
            return "true" if raw else "false"
        if raw is None:
            return ""
        if isinstance(raw, (int, float)):
            return str(raw)
        if isinstance(raw, list):
            parts = []
            for item in raw:
                s = self._to_string(item, name, source, line, col, scopes, resolving)
                parts.append("" if s is None else s)
            return "、".join(parts)
        return str(raw)

    def _truthy(self, raw, node: If, scopes, resolving) -> bool:
        if isinstance(raw, str):
            expanded = self._to_string(raw, node.name, node.source,
                                       node.line, node.col, scopes, resolving)
            return bool(expanded and expanded.strip())
        return bool(raw)


# ---------------------------------------------------------------------------
# 内置示例
# ---------------------------------------------------------------------------

DEMO_TEMPLATE = """\
尊敬的{收件人}：

{问候语}
{#if 是否会员}您是尊贵的会员，本次账单享受 9 折优惠。
{/if}账单明细：
{#each 账单项目}  {index}. {item}
{/each}合计：{合计} 元
日期：{日期}
"""

DEMO_VARIABLES = {
    "收件人": "张三",
    "问候语": "您好，{收件人}！以下是您本月的账单。",
    "是否会员": True,
    "账单项目": ["水费 30 元", "电费 52 元"],
    "合计": 82,
    "日期": "2026-09-30",
}

DEMO_BAD_TEMPLATE = """\
{甲}和{乙}
{未知变量}
{#if 开关}这一段的条件段没有闭合
"""

DEMO_BAD_VARIABLES = {
    "甲": "{乙}",          # 甲 -> 乙 -> 甲，循环引用
    "乙": "{甲}",
    "开关": True,
}


def _print_errors(errors) -> None:
    print("错误报告：")
    for err in errors:
        print(f"  {err}")


def run_demo() -> int:
    print("=" * 60)
    print("示例 1：正常展开")
    print("=" * 60)
    print("--- 模板 ---")
    print(DEMO_TEMPLATE)
    print("--- 变量 ---")
    print(json.dumps(DEMO_VARIABLES, ensure_ascii=False, indent=2))
    expander = Expander(DEMO_VARIABLES)
    result = expander.expand(DEMO_TEMPLATE)
    print("--- 展开结果 ---")
    print(result)
    if expander.errors:
        _print_errors(expander.errors)
    else:
        print("（无错误）")

    print()
    print("=" * 60)
    print("示例 2：错误报告（循环引用 / 未定义变量 / 未闭合条件段）")
    print("=" * 60)
    print("--- 模板 ---")
    print(DEMO_BAD_TEMPLATE)
    print("--- 变量 ---")
    print(json.dumps(DEMO_BAD_VARIABLES, ensure_ascii=False, indent=2))
    expander = Expander(DEMO_BAD_VARIABLES)
    result = expander.expand(DEMO_BAD_TEMPLATE)
    print("--- 展开结果（出错部分按空处理） ---")
    print(result)
    if expander.errors:
        _print_errors(expander.errors)
    else:
        print("（无错误）")
    return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="模板展开工具：变量引用 {名}、条件段 {#if 名}...{/if}、"
                    "循环段 {#each 列表}...{/each}")
    ap.add_argument("template", nargs="?", help="模板文件路径")
    ap.add_argument("variables", nargs="?", help="变量定义 JSON 文件路径")
    ap.add_argument("-o", "--output", help="输出文件（默认写到标准输出）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        return run_demo()
    if not args.template or not args.variables:
        ap.error("需要提供模板文件和变量定义 JSON 文件（或使用 --demo 查看示例）")

    with open(args.template, encoding="utf-8") as f:
        template_text = f.read()
    with open(args.variables, encoding="utf-8") as f:
        variables = json.load(f)
    if not isinstance(variables, dict):
        print("错误：变量定义 JSON 的顶层必须是对象", file=sys.stderr)
        return 2

    expander = Expander(variables)
    result = expander.expand(template_text, source=args.template)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
    else:
        sys.stdout.write(result)

    if expander.errors:
        print("\n错误报告：", file=sys.stderr)
        for err in expander.errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
