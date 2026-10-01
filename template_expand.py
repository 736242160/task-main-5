#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板展开工具（纯 Python 标准库，单文件）。

支持的三种构造：
  1. 变量引用：{变量名}                例如：{收件人}、{日期}
  2. 条件段：  {if 变量名} ... {endif}  变量为真时展开，为假时整段丢弃
  3. 循环段：  {for 项 in 列表} ... {endfor}  对列表逐项展开，循环体内用 {项} 引用当前项

变量定义来自 JSON 文件，变量的值里可以继续引用其他变量（逐层展开）。

错误检测：
  - 循环引用（a 引用 b、b 又引用 a）：报错并中断该引用链，不会死循环
  - 条件段 / 循环段未闭合：报告未闭合标签的位置（行、列）
  - 引用未定义的变量：报告变量名与位置（行、列）

用法：
  python3 template_expand.py 模板.txt 变量.json     # 展开结果输出到 stdout，错误报告输出到 stderr
  python3 template_expand.py --demo                 # 运行内置示例（含正常示例与错误示例）

输入示例（模板.txt）：
  尊敬的{收件人}：
  {问候语}
  {if 有附件}随信附上：
  {for 文件 in 附件}  - {文件}
  {endfor}{endif}祝好！
  {署名} {日期}

输入示例（变量.json）：
  {
    "收件人": "王老师",
    "问候语": "您好，{收件人}！",
    "有附件": "是",
    "附件": ["报价单.pdf", "合同草案.docx"],
    "署名": "李雷",
    "日期": "2026-10-01"
  }

输出示例（stdout）：
  尊敬的王老师：
  您好，王老师！
  随信附上：
    - 报价单.pdf
    - 合同草案.docx
  祝好！
  李雷 2026-10-01
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

TAG_RE = re.compile(r"\{([^{}]*)\}")
IF_RE = re.compile(r"^if\s+(\S+)$")
FOR_RE = re.compile(r"^for\s+(\S+)\s+in\s+(\S+)$")

# 展开后的文本去掉空白、转小写后命中这些词，视为“假”
FALSE_WORDS = {"", "0", "false", "no", "off", "none", "否", "假", "无", "空"}


# ---------------------------------------------------------------- 语法树节点

@dataclass
class TextNode:
    text: str


@dataclass
class VarNode:
    name: str
    line: int
    col: int


@dataclass
class IfNode:
    name: str
    line: int
    col: int
    body: list = field(default_factory=list)


@dataclass
class ForNode:
    var: str
    list_name: str
    line: int
    col: int
    body: list = field(default_factory=list)


# ---------------------------------------------------------------- 工具函数

def offset_to_line_col(text, offset):
    """把字符偏移量换算成 1 起始的 (行, 列)。"""
    line = text.count("\n", 0, offset) + 1
    last_nl = text.rfind("\n", 0, offset)
    return line, offset - last_nl


def make_error(kind, message, source=None, line=None, col=None, name=None):
    err = {"类型": kind, "信息": message}
    if name is not None:
        err["变量"] = name
    if line is not None:
        err["位置"] = "%s:第%d行第%d列" % (source, line, col)
    return err


# ---------------------------------------------------------------- 解析

def parse_template(text, source, errors):
    """把模板文本解析成节点列表；语法错误记入 errors 并尽量恢复继续解析。"""
    root = []
    stack = []  # 尚未闭合的 IfNode / ForNode

    def current_body():
        return stack[-1].body if stack else root

    pos = 0
    for m in TAG_RE.finditer(text):
        if m.start() > pos:
            current_body().append(TextNode(text[pos:m.start()]))
        line, col = offset_to_line_col(text, m.start())
        tag = m.group(1).strip()

        if not tag:
            errors.append(make_error("语法错误", "空的 {} 标签", source, line, col))
        elif tag == "endif":
            if stack and isinstance(stack[-1], IfNode):
                stack.pop()
            elif stack:
                errors.append(make_error(
                    "语法错误", "{endif} 与未闭合的循环段不匹配", source, line, col))
                stack.pop()
            else:
                errors.append(make_error(
                    "语法错误", "{endif} 没有匹配的 {if ...}", source, line, col))
        elif tag == "endfor":
            if stack and isinstance(stack[-1], ForNode):
                stack.pop()
            elif stack:
                errors.append(make_error(
                    "语法错误", "{endfor} 与未闭合的条件段不匹配", source, line, col))
                stack.pop()
            else:
                errors.append(make_error(
                    "语法错误", "{endfor} 没有匹配的 {for ...}", source, line, col))
        elif tag == "if" or tag.startswith("if ") or tag.startswith("if\t"):
            mi = IF_RE.match(tag)
            if mi:
                node = IfNode(mi.group(1), line, col)
                current_body().append(node)
                stack.append(node)
            else:
                errors.append(make_error(
                    "语法错误", "if 标签写法应为 {if 变量名}：{%s}" % tag,
                    source, line, col))
        elif tag == "for" or tag.startswith("for ") or tag.startswith("for\t"):
            mf = FOR_RE.match(tag)
            if mf:
                node = ForNode(mf.group(1), mf.group(2), line, col)
                current_body().append(node)
                stack.append(node)
            else:
                errors.append(make_error(
                    "语法错误", "for 标签写法应为 {for 项 in 列表}：{%s}" % tag,
                    source, line, col))
        elif re.search(r"\s", tag):
            errors.append(make_error(
                "语法错误", "无法识别的标签：{%s}" % tag, source, line, col))
        else:
            current_body().append(VarNode(tag, line, col))
        pos = m.end()

    if pos < len(text):
        current_body().append(TextNode(text[pos:]))

    for node in stack:  # 解析结束仍未闭合的段
        if isinstance(node, IfNode):
            kind, open_tag = "条件段", "{if %s}" % node.name
        else:
            kind, open_tag = "循环段", "{for %s in %s}" % (node.var, node.list_name)
        errors.append(make_error(
            "未闭合", "%s未闭合：%s 缺少对应的结束标签" % (kind, open_tag),
            source, node.line, node.col))
    return root


# ---------------------------------------------------------------- 展开

class Expander:
    def __init__(self, variables, errors):
        self.variables = variables
        self.errors = errors

    def lookup(self, name, env, source, line, col):
        """返回 (原始值, 是否存在)。循环变量优先于全局变量。"""
        if name in env:
            return env[name], True
        if name in self.variables:
            return self.variables[name], True
        self.errors.append(make_error(
            "未定义变量", "引用了未定义的变量“%s”" % name,
            source, line, col, name=name))
        return "", False

    def expand_scalar(self, name, env, stack, source, line, col):
        """把变量展开成文本；stack 记录展开链，用于检测循环引用。"""
        if name in stack:
            chain = " -> ".join(stack + [name])
            self.errors.append(make_error(
                "循环引用", "检测到循环引用：%s" % chain,
                source, line, col, name=name))
            return ""
        raw, ok = self.lookup(name, env, source, line, col)
        if not ok:
            return ""
        if isinstance(raw, list):
            self.errors.append(make_error(
                "类型错误", "“%s”是列表，不能直接作为文本引用（可用 for 循环展开）" % name,
                source, line, col, name=name))
            return ""
        if isinstance(raw, bool):
            raw = "true" if raw else "false"
        elif not isinstance(raw, str):
            raw = str(raw)
        # 变量的值里可能继续引用其他变量：递归解析并展开
        nodes = parse_template(raw, "变量[%s]" % name, self.errors)
        return self.expand_nodes(nodes, env, stack + [name])

    def is_truthy(self, name, env, stack, source, line, col):
        raw, ok = self.lookup(name, env, source, line, col)
        if not ok:
            return False
        if isinstance(raw, list):
            return len(raw) > 0
        text = self.expand_scalar(name, env, stack, source, line, col)
        return text.strip().lower() not in FALSE_WORDS

    def expand_nodes(self, nodes, env, stack, source="<模板>"):
        out = []
        for node in nodes:
            if isinstance(node, TextNode):
                out.append(node.text)
            elif isinstance(node, VarNode):
                out.append(self.expand_scalar(
                    node.name, env, stack, source, node.line, node.col))
            elif isinstance(node, IfNode):
                if self.is_truthy(node.name, env, stack, source, node.line, node.col):
                    out.append(self.expand_nodes(node.body, env, stack, source))
            elif isinstance(node, ForNode):
                raw, ok = self.lookup(
                    node.list_name, env, source, node.line, node.col)
                if not ok:
                    continue
                if not isinstance(raw, list):
                    self.errors.append(make_error(
                        "类型错误", "“%s”不是列表，无法循环" % node.list_name,
                        source, node.line, node.col, name=node.list_name))
                    continue
                for item in raw:
                    child_env = dict(env)
                    child_env[node.var] = item
                    out.append(self.expand_nodes(node.body, child_env, stack, source))
        return "".join(out)


def expand(template_text, variables):
    """展开模板，返回 (结果文本, 错误列表)。"""
    errors = []
    nodes = parse_template(template_text, "<模板>", errors)
    result = Expander(variables, errors).expand_nodes(nodes, {}, [])
    return result, errors


# ---------------------------------------------------------------- 输出

def format_errors(errors):
    lines = ["共 %d 个错误：" % len(errors)]
    for i, e in enumerate(errors, 1):
        parts = ["%d. [%s] %s" % (i, e["类型"], e["信息"])]
        if "位置" in e:
            parts.append("（%s）" % e["位置"])
        lines.append("".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------- 内置示例

DEMO_TEMPLATE = """\
尊敬的{收件人}：

{问候语}
{if 有附件}
随信附上以下材料：
{for 文件 in 附件}  - {文件}
{endfor}{endif}
期待您的回复。

{署名}
{日期}
"""

DEMO_VARS = {
    "收件人": "王老师",
    "问候语": "您好，{收件人}！很高兴再次与您联系。",
    "有附件": "是",
    "附件": ["报价单.pdf", "合同草案.docx", "产品手册.pdf"],
    "署名": "李雷",
    "日期": "2026-10-01",
}

BAD_TEMPLATE = """\
{甲}、{乙}、{丙}
{for x in 不是列表}{x}{endfor}
{if 未闭合的条件}
这一段缺少 endif。
"""

BAD_VARS = {
    "甲": "{乙}",
    "乙": "{甲}",
    "不是列表": "普通文本",
}


def run_demo():
    print("=" * 60)
    print("示例 1：正常展开")
    print("=" * 60)
    print("---- 模板 ----")
    print(DEMO_TEMPLATE)
    print("---- 变量 ----")
    print(json.dumps(DEMO_VARS, ensure_ascii=False, indent=2))
    result, errors = expand(DEMO_TEMPLATE, DEMO_VARS)
    print("---- 展开结果 ----")
    print(result)
    print("---- 错误报告 ----")
    print(format_errors(errors) if errors else "无错误")

    print()
    print("=" * 60)
    print("示例 2：错误检测（循环引用 / 未定义变量 / 未闭合 / 非列表循环）")
    print("=" * 60)
    print("---- 模板 ----")
    print(BAD_TEMPLATE)
    print("---- 变量 ----")
    print(json.dumps(BAD_VARS, ensure_ascii=False, indent=2))
    result, errors = expand(BAD_TEMPLATE, BAD_VARS)
    print("---- 展开结果 ----")
    print(result)
    print("---- 错误报告 ----")
    print(format_errors(errors) if errors else "无错误")


# ---------------------------------------------------------------- 命令行入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="模板展开工具：变量引用 {名}、条件段 {if 名}...{endif}、"
                    "循环段 {for 项 in 列表}...{endfor}")
    parser.add_argument("template", nargs="?",
                        help="模板文件路径，'-' 表示从标准输入读取")
    parser.add_argument("vars", nargs="?",
                        help="变量定义 JSON 文件（缺省视为空变量表）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例")
    args = parser.parse_args(argv)

    if args.demo or not args.template:
        run_demo()
        return 0

    if args.template == "-":
        template_text = sys.stdin.read()
    else:
        with open(args.template, "r", encoding="utf-8") as f:
            template_text = f.read()

    variables = {}
    if args.vars:
        with open(args.vars, "r", encoding="utf-8") as f:
            variables = json.load(f)
        if not isinstance(variables, dict):
            print("错误：变量 JSON 的顶层必须是对象（名 -> 值）", file=sys.stderr)
            return 2

    result, errors = expand(template_text, variables)
    sys.stdout.write(result)
    if errors:
        print("\n【错误报告】", file=sys.stderr)
        print(format_errors(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
