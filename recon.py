#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recon.py — 集团报表勾稽工具（纯 Python 标准库，单文件）

用法:
    python3 recon.py 组织定义.txt 上报流.txt 汇总流.txt
    python3 recon.py            # 不带参数则运行内置勾稽示例

输入格式（行内以空白分隔，# 开头为注释，空行忽略）:

  组织定义:  节点 上级 [汇总规则]
      上级填 - 表示根节点。汇总规则: 纯汇总(默认, 汇总=各子节点有效值之和)
      或 含本级(汇总=本级自有明细+各子节点有效值之和)。

  上报流:    节点 期间 明细项 数值
      同一 (节点, 期间, 明细项) 重复上报将被拦截。
      叶子节点的有效值=本级明细合计; 纯汇总内部节点的本级明细不计入有效值
      (会给出警告), 含本级内部节点的本级明细计入有效值。

  汇总流:    节点 期间 汇总数值
      同一 (节点, 期间) 重复汇总将被拦截。

输出:
  1) 错误报告: 树成环 / 引用不存在节点 / 重复上报 / 漏报(有子无报) /
     纯汇总节点含本级明细 / 跨期状态延续提示
  2) 勾稽差异清单: 按 (期间, 节点) 列出上报汇总、级联重算应得、差异与构成
  3) 勾稽通过清单

级联重算自底向上进行, 孙级到祖级自动逐层汇总; 跨期处理按期间排序,
已上报登记等状态跨期延续。
"""
import sys
from collections import defaultdict

EPS = 1e-6


class OrgTree:
    """组织树: 校验重复定义、引用不存在节点、上级循环。"""

    def __init__(self):
        self.parent = {}
        self.rule = {}
        self.children = defaultdict(list)
        self.cycle_nodes = set()
        self.errors = []

    def load(self, lines):
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                self.errors.append("[组织定义] 第%d行格式错误: %s" % (lineno, line))
                continue
            node, parent = parts[0], parts[1]
            rule = parts[2] if len(parts) > 2 else "纯汇总"
            if node in self.parent:
                self.errors.append("[组织定义] 第%d行节点重复定义: %s" % (lineno, node))
                continue
            if rule not in ("纯汇总", "含本级"):
                self.errors.append(
                    "[组织定义] 第%d行未知汇总规则 '%s', 已按纯汇总处理" % (lineno, rule))
                rule = "纯汇总"
            self.parent[node] = None if parent in ("-", "") else parent
            self.rule[node] = rule
        for node, par in self.parent.items():
            if par is None:
                continue
            if par not in self.parent:
                self.errors.append(
                    "[组织定义] 节点 '%s' 引用不存在的上级节点 '%s', 已按根节点处理"
                    % (node, par))
                self.parent[node] = None
            else:
                self.children[par].append(node)
        self._detect_cycles()

    def _detect_cycles(self):
        state = {}  # 0=未访问 1=在路径上 2=已完成
        for start in self.parent:
            if state.get(start, 0):
                continue
            path, node = [], start
            while node is not None and state.get(node, 0) == 0:
                state[node] = 1
                path.append(node)
                node = self.parent.get(node)
            if node is not None and state.get(node) == 1:
                cyc = path[path.index(node):]
                self.cycle_nodes.update(cyc)
                self.errors.append(
                    "[树成环] 检测到上级循环: %s" % " -> ".join(cyc + [node]))
            for n in path:
                state[n] = 2


class ReconEngine:
    """勾稽引擎: 摄取上报流/汇总流, 级联重算并输出差异。"""

    def __init__(self, org):
        self.org = org
        self.own = defaultdict(float)        # (节点,期间) -> 本级明细合计
        self.items = defaultdict(dict)       # (节点,期间) -> {明细项: 数值}
        self.summaries = {}                  # (节点,期间) -> 上报汇总值
        self.errors = []
        self.warnings = []
        self.infos = []
        self.diffs = []                      # 勾稽差异清单
        self.passed = []                     # 勾稽通过清单

    def load_reports(self, lines):
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 4:
                self.errors.append("[上报流] 第%d行格式错误(应为: 节点 期间 明细项 数值): %s"
                                   % (lineno, line))
                continue
            node, period, item, val_s = parts
            if node not in self.org.parent:
                self.errors.append("[上报流] 第%d行引用不存在节点 '%s', 已跳过"
                                   % (lineno, node))
                continue
            if node in self.org.cycle_nodes:
                self.errors.append("[上报流] 第%d行节点 '%s' 位于上级循环内, 已跳过"
                                   % (lineno, node))
                continue
            try:
                val = float(val_s)
            except ValueError:
                self.errors.append("[上报流] 第%d行数值无法解析: %s" % (lineno, val_s))
                continue
            key = (node, period)
            if item in self.items[key]:
                self.errors.append(
                    "[重复上报] 节点 '%s' 期间 '%s' 明细项 '%s' 重复上报(原值 %.2f, 新值 %.2f), 已拦截"
                    % (node, period, item, self.items[key][item], val))
                continue
            self.items[key][item] = val
            self.own[key] += val

    def load_summaries(self, lines):
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3:
                self.errors.append("[汇总流] 第%d行格式错误(应为: 节点 期间 数值): %s"
                                   % (lineno, line))
                continue
            node, period, val_s = parts
            if node not in self.org.parent:
                self.errors.append("[汇总流] 第%d行引用不存在节点 '%s', 已跳过"
                                   % (lineno, node))
                continue
            if node in self.org.cycle_nodes:
                self.errors.append("[汇总流] 第%d行节点 '%s' 位于上级循环内, 已跳过"
                                   % (lineno, node))
                continue
            try:
                val = float(val_s)
            except ValueError:
                self.errors.append("[汇总流] 第%d行数值无法解析: %s" % (lineno, val_s))
                continue
            key = (node, period)
            if key in self.summaries:
                self.errors.append(
                    "[重复汇总] 节点 '%s' 期间 '%s' 重复汇总(原值 %.2f, 新值 %.2f), 已拦截"
                    % (node, period, self.summaries[key], val))
                continue
            self.summaries[key] = val

    def effective(self, node, period, memo, stack):
        """级联重算节点有效值: 叶子=本级明细; 含本级=本级+子级; 纯汇总=子级。"""
        key = (node, period)
        if key in memo:
            return memo[key]
        if node in stack:  # 环保护(环已在加载期报告)
            return 0.0
        stack.add(node)
        child_sum = 0.0
        for ch in self.org.children.get(node, ()):
            child_sum += self.effective(ch, period, memo, stack)
        stack.discard(node)
        own = self.own.get(key, 0.0)
        if not self.org.children.get(node):
            total = own
        elif self.org.rule.get(node) == "含本级":
            total = own + child_sum
        else:
            total = child_sum
        memo[key] = total
        return total

    def composition(self, node, period, memo):
        child_sum = sum(self.effective(ch, period, memo, set())
                        for ch in self.org.children.get(node, ()))
        own = self.own.get((node, period), 0.0)
        if not self.org.children.get(node):
            return "本级明细 %.2f" % own
        if self.org.rule.get(node) == "含本级":
            return "本级 %.2f + 子级 %.2f" % (own, child_sum)
        return "子级合计 %.2f" % child_sum

    def reconcile(self):
        periods = sorted({p for (_, p) in self.own} | {p for (_, p) in self.summaries})
        reported_prev = set()  # 跨期状态: 此前各期有明细上报的节点
        for period in periods:
            memo = {}
            reported_now = {n for (n, p) in self.own if p == period}
            for node in sorted(self.org.parent):
                if node in self.org.cycle_nodes:
                    continue
                has_child = bool(self.org.children.get(node))
                key = (node, period)
                if has_child and node not in reported_now and key not in self.summaries:
                    self.errors.append(
                        "[漏报] 节点 '%s' 期间 '%s' 有子节点但本期既无明细上报也无汇总上报(有子无报)"
                        % (node, period))
                if (has_child and self.org.rule.get(node) == "纯汇总"
                        and abs(self.own.get(key, 0.0)) > EPS):
                    self.warnings.append(
                        "[规则区分] 纯汇总节点 '%s' 期间 '%s' 存在本级明细 %.2f, 按规则不计入汇总"
                        % (node, period, self.own[key]))
                if key in self.summaries:
                    expected = self.effective(node, period, memo, set())
                    actual = self.summaries[key]
                    diff = actual - expected
                    row = (period, node, actual, expected, diff,
                           self.composition(node, period, memo))
                    if abs(diff) > EPS:
                        self.diffs.append(row)
                    else:
                        self.passed.append(row)
            # 跨期状态延续: 此前有报、本期未报的节点提示
            for node in sorted(reported_prev - reported_now):
                self.infos.append(
                    "[跨期延续] 节点 '%s' 此前期间有明细上报, 期间 '%s' 未上报, 请确认是否漏报"
                    % (node, period))
            reported_prev |= reported_now

    def report(self, out=sys.stdout):
        w = lambda s="": print(s, file=out)
        w("=" * 72)
        w("错误报告")
        w("=" * 72)
        all_err = self.org.errors + self.errors
        if all_err:
            for e in all_err:
                w("  " + e)
        else:
            w("  (无错误)")
        if self.warnings:
            w("-" * 72)
            w("警告(规则区分)")
            for x in self.warnings:
                w("  " + x)
        if self.infos:
            w("-" * 72)
            w("提示(跨期状态延续)")
            for x in self.infos:
                w("  " + x)
        w("")
        w("=" * 72)
        w("勾稽差异清单(上报汇总 != 级联重算应得)")
        w("=" * 72)
        header = "%-10s %-10s %14s %14s %14s  %s" % (
            "期间", "节点", "上报汇总", "重算应得", "差异", "构成")
        if self.diffs:
            w(header)
            w("-" * 72)
            for period, node, actual, expected, diff, comp in sorted(self.diffs):
                w("%-10s %-10s %14.2f %14.2f %+14.2f  %s"
                  % (period, node, actual, expected, diff, comp))
        else:
            w("  (无差异)")
        w("")
        w("=" * 72)
        w("勾稽通过清单")
        w("=" * 72)
        if self.passed:
            w(header)
            w("-" * 72)
            for period, node, actual, expected, diff, comp in sorted(self.passed):
                w("%-10s %-10s %14.2f %14.2f %+14.2f  %s"
                  % (period, node, actual, expected, diff, comp))
        else:
            w("  (无)")
        w("")
        w("汇总: 错误 %d 条, 警告 %d 条, 提示 %d 条, 勾稽差异 %d 条, 勾稽通过 %d 条"
          % (len(all_err), len(self.warnings), len(self.infos),
             len(self.diffs), len(self.passed)))


def run(org_lines, report_lines, summary_lines, out=sys.stdout):
    org = OrgTree()
    org.load(org_lines)
    eng = ReconEngine(org)
    eng.load_reports(report_lines)
    eng.load_summaries(summary_lines)
    eng.reconcile()
    eng.report(out)
    return 1 if (org.errors or eng.errors or eng.diffs) else 0


DEMO_ORG = """\
# 节点 上级 汇总规则
集团 - 纯汇总
事业部A 集团 纯汇总
事业部B 集团 纯汇总
公司A1 事业部A 含本级
A1车间 公司A1 纯汇总
公司A2 事业部A 纯汇总
公司A3 事业部A 纯汇总
公司B1 事业部B 纯汇总
子公司C 事业部B 纯汇总
孙公司C1 子公司C 纯汇总
循环甲 循环乙
循环乙 循环甲
挂靠节点 不存在上级
"""

DEMO_REPORTS = """\
# 节点 期间 明细项 数值
公司A1 2026-01 销售收入 100
公司A1 2026-01 其他收入 20
A1车间 2026-01 车间收入 80
公司A2 2026-01 销售收入 200
公司A2 2026-01 销售收入 200
公司A3 2026-01 销售收入 30
公司B1 2026-01 销售收入 300
孙公司C1 2026-01 销售收入 50
幽灵公司 2026-01 销售收入 1
公司A1 2026-02 销售收入 110
A1车间 2026-02 车间收入 80
公司A2 2026-02 销售收入 210
公司B1 2026-02 销售收入 310
子公司C 2026-02 临时调整 5
孙公司C1 2026-02 销售收入 60
"""

DEMO_SUMMARIES = """\
# 节点 期间 汇总数值
公司A1 2026-01 200
事业部A 2026-01 430
事业部A 2026-01 430
事业部B 2026-01 350
集团 2026-01 999
公司A1 2026-02 190
事业部A 2026-02 400
事业部B 2026-02 370
集团 2026-02 770
"""


def main(argv):
    if len(argv) == 4:
        with open(argv[1], encoding="utf-8") as f:
            org_lines = f.readlines()
        with open(argv[2], encoding="utf-8") as f:
            report_lines = f.readlines()
        with open(argv[3], encoding="utf-8") as f:
            summary_lines = f.readlines()
        return run(org_lines, report_lines, summary_lines)
    if len(argv) == 1:
        print("未提供输入文件, 运行内置勾稽示例 "
              "(正式用法: python3 recon.py 组织定义.txt 上报流.txt 汇总流.txt)\n")
        return run(DEMO_ORG.splitlines(), DEMO_REPORTS.splitlines(),
                   DEMO_SUMMARIES.splitlines())
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
