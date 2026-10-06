#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
group_recon.py — 集团报表勾稽工具（纯 Python 标准库，单文件）

用法：
    python3 group_recon.py 数据文件          # 从文件读取
    python3 group_recon.py < 数据文件        # 从标准输入读取
    python3 group_recon.py --demo            # 运行内置勾稽示例

输入格式（行指令，# 开头为注释，空白分隔）：
    ORG  <节点> <上级节点|->                组织定义；上级为 - 表示根节点
    RPT  <节点> <期间> <明细项> <数值>      上报流：节点本级自有明细
    SUM  <节点> <期间> <明细项> <数值>      汇总流：节点上报的汇总数

勾稽规则（自定规则：汇总数含本级自有数，输出中区分列示）：
    应有汇总(节点) = 本级自有明细(RPT) + Σ 应有汇总(直接子节点)   —— 级联重算
    差异 = 上报汇总(SUM) - 应有汇总
期间按字典序排列，跨期累计余额自动延续（上期末累计 = 本期初累计）。

检测能力：
    1. 父子级勾稽（父汇总 = 本级自有 + 子级应有汇总之和）
    2. 勾稽不一致定位到节点 + 期间 + 明细项
    3. 跨多级（孙->父->祖）级联重算
    4. 漏报节点（有子节点活动但本级未报）
    5. 重复上报同一节点同期间同明细项 -> 拦截
    6. 汇总数中本级自有数与下级汇总数分列
    7. 组织树成环（上级循环）检测
    8. 引用不存在节点检测
    9. 跨期状态延续（累计余额期初/期末结转）
   10. 输出勾稽差异清单
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")


def fmt(value: Decimal) -> str:
    """Decimal -> 普通十进制字符串（避免科学计数法）。"""
    return format(value, "f")


class Recon:
    def __init__(self) -> None:
        self.org_pairs: list[tuple[str, str, int]] = []   # (node, parent, lineno)
        self.parent: dict[str, str | None] = {}
        self.children: dict[str, list[str]] = defaultdict(list)
        self.rpt: dict[tuple[str, str, str], Decimal] = {}  # (node, period, item)
        self.sums: dict[tuple[str, str, str], Decimal] = {}
        self.periods: set[str] = set()
        self.items_by_period: dict[str, set[str]] = defaultdict(set)
        self.active_by_period: dict[str, set[str]] = defaultdict(set)
        self.errors: list[tuple[str, str]] = []           # (类别, 消息)
        self.cycle_nodes: set[str] = set()
        self._exp_memo: dict[tuple[str, str, str], Decimal] = {}

    # ---------------- 解析 ----------------
    def parse(self, text: str) -> None:
        raw_flows: list[tuple[str, list[str], int]] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            directive = parts[0].upper()
            if directive == "ORG":
                if len(parts) != 3:
                    self.errors.append(("格式错误", f"第{lineno}行: ORG 需要 2 个参数: {raw.strip()}"))
                    continue
                self.org_pairs.append((parts[1], parts[2], lineno))
            elif directive in ("RPT", "SUM"):
                if len(parts) != 5:
                    self.errors.append(("格式错误", f"第{lineno}行: {directive} 需要 4 个参数: {raw.strip()}"))
                    continue
                raw_flows.append((directive, parts[1:], lineno))
            else:
                self.errors.append(("格式错误", f"第{lineno}行: 未知指令 {parts[0]!r}"))
        self._resolve_org()
        self._load_flows(raw_flows)

    def _resolve_org(self) -> None:
        for node, parent, _lineno in self.org_pairs:
            if node in self.parent:
                self.errors.append(("组织重复", f"节点 {node!r} 被重复定义"))
                continue
            self.parent[node] = None if parent == "-" else parent
        for node, parent, lineno in self.org_pairs:
            if parent != "-" and parent not in self.parent:
                self.errors.append(
                    ("引用不存在节点",
                     f"第{lineno}行: 节点 {node!r} 的上级 {parent!r} 未在组织定义中存在，已按根节点处理"))
                self.parent[node] = None
        for node, parent in self.parent.items():
            if parent is not None:
                self.children[parent].append(node)
        self._detect_cycles()

    def _detect_cycles(self) -> None:
        reported: set[tuple[str, ...]] = set()
        for start in self.parent:
            seen: set[str] = set()
            node: str | None = start
            while node is not None and node not in seen:
                seen.add(node)
                node = self.parent.get(node)
            if node is not None:
                loop: list[str] = []
                cur: str | None = node
                while cur is not None and cur not in loop:
                    loop.append(cur)
                    cur = self.parent.get(cur)
                # 规范旋转去重：同一环只报告一次
                idx = min(range(len(loop)), key=lambda i: loop[i])
                canon = tuple(loop[idx:] + loop[:idx])
                if canon in reported:
                    continue
                reported.add(canon)
                self.cycle_nodes.update(loop)
                self.errors.append(("树成环", f"上级循环: {' -> '.join(canon)} -> {canon[0]}"))

    def _load_flows(self, raw_flows: list[tuple[str, list[str], int]]) -> None:
        for directive, (node, period, item, num), lineno in raw_flows:
            if node not in self.parent:
                self.errors.append(
                    ("引用不存在节点", f"第{lineno}行: {directive} 引用了未定义节点 {node!r}，该行已忽略"))
                continue
            try:
                value = Decimal(num)
            except InvalidOperation:
                self.errors.append(("格式错误", f"第{lineno}行: 数值 {num!r} 无法解析，该行已忽略"))
                continue
            table = self.rpt if directive == "RPT" else self.sums
            key = (node, period, item)
            if key in table:
                kind = "上报明细" if directive == "RPT" else "汇总数"
                self.errors.append(
                    ("重复上报",
                     f"第{lineno}行: 节点 {node!r} 期间 {period} 明细项 {item!r} 的{kind}重复上报，已拦截（保留首值）"))
                continue
            table[key] = value
            self.periods.add(period)
            self.items_by_period[period].add(item)
            self.active_by_period[period].add(node)

    # ---------------- 勾稽计算 ----------------
    def expected(self, node: str, period: str, item: str,
                 _visiting: frozenset[str] = frozenset()) -> Decimal:
        """应有汇总 = 本级自有 + Σ 子节点应有汇总（级联重算，含环保护）。"""
        key = (node, period, item)
        if key in self._exp_memo:
            return self._exp_memo[key]
        if node in _visiting:  # 环上节点：已报错，按 0 处理避免死循环
            return ZERO
        visiting = _visiting | {node}
        total = self.rpt.get(key, ZERO)
        for child in self.children.get(node, ()):
            total += self.expected(child, period, item, visiting)
        self._exp_memo[key] = total
        return total

    def check_missing_reports(self) -> None:
        """漏报：有子节点活动但本级（RPT 与 SUM）均未报的节点。"""
        for period in sorted(self.periods):
            active = self.active_by_period[period]
            for node in sorted(self.parent):
                if node in active or not self.children.get(node):
                    continue
                if any(child in active for child in self.children[node]):
                    self.errors.append(
                        ("漏报节点",
                         f"期间 {period}: 节点 {node!r} 存在下级上报但本级未提交任何明细/汇总"))

    def diff_list(self) -> list[dict]:
        """勾稽差异清单：上报汇总 vs 应有汇总（本级自有 + 下级合计）。"""
        rows = []
        for (node, period, item), reported in sorted(self.sums.items()):
            own = self.rpt.get((node, period, item), ZERO)
            children_sum = sum(
                (self.expected(c, period, item) for c in self.children.get(node, ())),
                ZERO,
            )
            expected = own + children_sum
            diff = reported - expected
            if diff != ZERO:
                rows.append({
                    "period": period, "node": node, "item": item,
                    "reported": reported, "expected": expected,
                    "own": own, "children_sum": children_sum, "diff": diff,
                })
        return rows

    def cross_period(self) -> list[dict]:
        """跨期状态延续：按 (节点, 明细项) 累计，期初 = 上期期末。"""
        nodes_items: set[tuple[str, str]] = set()
        for node, _period, item in list(self.rpt) + list(self.sums):
            nodes_items.add((node, item))
        rows = []
        for node, item in sorted(nodes_items):
            cum_exp = ZERO
            cum_rep = ZERO
            has_reported = any((node, p, item) in self.sums for p in self.periods)
            for period in sorted(self.periods):
                exp = self.expected(node, period, item)
                rep = self.sums.get((node, period, item))
                if exp == ZERO and rep is None:
                    continue
                opening_exp, opening_rep = cum_exp, cum_rep
                cum_exp += exp
                if rep is not None:
                    cum_rep += rep
                rows.append({
                    "node": node, "item": item, "period": period,
                    "opening_exp": opening_exp, "period_exp": exp, "closing_exp": cum_exp,
                    "opening_rep": opening_rep if has_reported else None,
                    "period_rep": rep, "closing_rep": cum_rep if has_reported else None,
                    "cum_diff": (cum_rep - cum_exp) if has_reported else None,
                })
        return rows

    # ---------------- 报告输出 ----------------
    def render(self) -> str:
        self.check_missing_reports()
        out: list[str] = []
        w = out.append

        w("=" * 72)
        w("一、错误报告")
        w("=" * 72)
        if self.errors:
            by_cat: dict[str, list[str]] = defaultdict(list)
            for cat, msg in self.errors:
                by_cat[cat].append(msg)
            for cat in ("树成环", "引用不存在节点", "重复上报", "漏报节点", "组织重复", "格式错误"):
                for msg in by_cat.get(cat, []):
                    w(f"  [{cat}] {msg}")
            w(f"  共 {len(self.errors)} 项")
        else:
            w("  无错误。")
        w("")

        w("=" * 72)
        w("二、勾稽差异清单（上报汇总 - 应有汇总；应有汇总 = 本级自有 + 下级合计）")
        w("=" * 72)
        diffs = self.diff_list()
        if diffs:
            w(f"  {'期间':<10}{'节点':<10}{'明细项':<8}{'上报汇总':>12}{'应有汇总':>12}"
              f"{'差异':>12}{'本级自有':>12}{'下级合计':>12}")
            for r in diffs:
                w(f"  {r['period']:<10}{r['node']:<10}{r['item']:<8}"
                  f"{fmt(r['reported']):>12}{fmt(r['expected']):>12}{fmt(r['diff']):>12}"
                  f"{fmt(r['own']):>12}{fmt(r['children_sum']):>12}")
            w(f"  共 {len(diffs)} 笔差异")
        else:
            w("  全部勾稽相符，无差异。")
        w("")

        w("=" * 72)
        w("三、跨期累计勾稽（期初累计 = 上期期末累计，状态跨期延续）")
        w("=" * 72)
        rows = self.cross_period()
        if rows:
            w(f"  {'节点':<10}{'明细项':<8}{'期间':<10}{'期初累计':>12}{'本期应有':>12}"
              f"{'期末应有':>12}{'本期上报':>12}{'期末上报':>12}{'累计差异':>12}")
            for r in rows:
                rep_p = fmt(r["period_rep"]) if r["period_rep"] is not None else "-"
                rep_c = fmt(r["closing_rep"]) if r["closing_rep"] is not None else "-"
                cum = fmt(r["cum_diff"]) if r["cum_diff"] is not None else "-"
                w(f"  {r['node']:<10}{r['item']:<8}{r['period']:<10}"
                  f"{fmt(r['opening_exp']):>12}{fmt(r['period_exp']):>12}{fmt(r['closing_exp']):>12}"
                  f"{rep_p:>12}{rep_c:>12}{cum:>12}")
        else:
            w("  无数据。")
        w("")

        w("=" * 72)
        w("四、勾稽结论")
        w("=" * 72)
        n_err = len(self.errors)
        n_diff = len(diffs)
        status = "勾稽相符" if (n_err == 0 and n_diff == 0) else "勾稽不符"
        w(f"  结果: {status}  |  错误 {n_err} 项  |  差异 {n_diff} 笔  |  "
          f"期间数 {len(self.periods)}  |  节点数 {len(self.parent)}")
        return "\n".join(out)


DEMO = """\
# ---------- 组织定义 ----------
ORG 集团 -
ORG 华东 集团
ORG 华南 集团
ORG 上海 华东
ORG 杭州 华东
ORG 广州 华南

# ---------- 2025Q1 上报流 ----------
RPT 上海 2025Q1 收入 100
RPT 上海 2025Q1 成本 60
RPT 杭州 2025Q1 收入 80
RPT 杭州 2025Q1 成本 50
RPT 广州 2025Q1 收入 70
RPT 广州 2025Q1 成本 40
RPT 华东 2025Q1 收入 20        # 华东本级自有数（汇总数含本级自有）
# ---------- 2025Q1 汇总流 ----------
SUM 华东 2025Q1 收入 200       # 应有 100+80+20=200 相符
SUM 华东 2025Q1 成本 110       # 应有 60+50=110 相符
SUM 华南 2025Q1 收入 70
SUM 华南 2025Q1 成本 40
SUM 集团 2025Q1 收入 260       # 级联重算应有 200+70=270，差异 -10
SUM 集团 2025Q1 成本 150       # 应有 110+40=150 相符

# ---------- 2025Q2 上报流 ----------
RPT 上海 2025Q2 收入 110
RPT 杭州 2025Q2 收入 90
RPT 广州 2025Q2 收入 75
SUM 华东 2025Q2 收入 200       # 应有 110+90=200 相符
SUM 集团 2025Q2 收入 275       # 应有 200+75=275 相符
# 华南 2025Q2 有下级广州上报但本级未报 -> 漏报
RPT 上海 2025Q2 收入 110       # 重复上报 -> 拦截
RPT 外星公司 2025Q2 收入 1     # 引用不存在节点 -> 报告
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="集团报表勾稽工具（纯标准库单文件）")
    ap.add_argument("input", nargs="?", help="输入数据文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置勾稽示例")
    args = ap.parse_args(argv)

    if args.demo:
        text = DEMO
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    recon = Recon()
    recon.parse(text)
    print(recon.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
