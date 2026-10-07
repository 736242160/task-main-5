#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fx_settlement.py — 跨境结算汇率折算 / 重估 / 风险检查工具（纯 Python 标准库，单文件）

输入为行式指令流（文件或 stdin），三类流可任意交错，状态跨流延续：
    CCY       <币种>                     定义币种
    SETTLE    <币种>                     设定记账基准币（缺省为第一个定义的币种）
    THRESHOLD <小数>                     偏差阈值（缺省 0.005，即 0.5%）
    RATE      <基准币> <目标币> <汇率>    新报价（同币种对重复报价一律拦截）
    UPDATE    <基准币> <目标币> <汇率>    更新已有报价（其后的折算/重估使用新汇率）
    TXN       <交易号> <币种> <金额>      交易，按当前最新汇率折算为基准币入账
    REVAL     <日期>                     按当前最新汇率重估全部存量交易，差额入账
    CHECK                                立即执行偏差/套利检查（流结束时自动再执行一次）

约定与自定规则：
  * 汇率矩阵双向维护：报 RATE A B r 即同时登记 A->B = r 与 B->A = 1/r。
  * 同币种对只允许 RATE 一次，其后只能 UPDATE；重复 RATE 拦截并报告。
  * 间接汇率：对每个有直接报价的币种对 (A,B)，枚举中间币 K，
    间接汇率 = rate(A,K)*rate(K,B)，与直接汇率偏差超过阈值即报告并定位币种对与路径。
  * 三角套利环：对每三个互通币种检查 rate(a,b)*rate(b,c)*rate(c,a) 偏离 1 的程度。
  * 折算路径：无直接报价时经中间币 BFS 寻径折算（间接折算）。
  * 重估差额入账规则：差额 = 新折算值 - 账面折算值，按 0.01 四舍五入；
    差额 > 0（收益）：借 货币性项目-币种 / 贷 汇兑损益；
    差额 < 0（损失）：借 汇兑损益 / 贷 货币性项目-币种。差额为零不出分录。

输出：折算清单、重估清单（含入账分录）、错误与风险报告。
"""

import argparse
import itertools
import sys
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext

getcontext().prec = 28

CENT = Decimal("0.01")
RATE_Q = Decimal("0.000001")
DEV_Q = Decimal("0.000001")


def fmt_rate(d):
    return str(d.quantize(RATE_Q).normalize())


def fmt_money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_pct(d):
    return str((d * 100).quantize(Decimal("0.0001"))) + "%"


class FxEngine:
    def __init__(self, threshold):
        self.currencies = []          # 保序
        self.curr_set = set()
        self.rates = {}               # (base, target) -> Decimal，双向维护
        self.settle = None
        self.threshold = threshold
        self.txns = {}                # 交易号 -> dict（跨流延续的存量）
        self.conversions = []         # 折算清单
        self.revals = []              # 重估清单
        self.journal = []             # 重估入账分录
        self.errors = []              # 错误报告
        self.findings = []            # 风险报告（偏差/套利）
        self._seen_findings = set()

    # ---------- 基础维护 ----------

    def error(self, lineno, msg):
        self.errors.append("第%d行: %s" % (lineno, msg))

    def define_ccy(self, lineno, ccy):
        if ccy in self.curr_set:
            self.error(lineno, "币种 %s 重复定义" % ccy)
            return
        self.currencies.append(ccy)
        self.curr_set.add(ccy)
        if self.settle is None:
            self.settle = ccy

    def _set_matrix(self, base, target, rate):
        """双向汇率矩阵维护：正反向同时登记。"""
        self.rates[(base, target)] = rate
        self.rates[(target, base)] = Decimal(1) / rate

    def add_rate(self, lineno, base, target, rate_s, update=False):
        verb = "UPDATE" if update else "RATE"
        for c in (base, target):
            if c not in self.curr_set:
                self.error(lineno, "%s 引用不存在币种 %s" % (verb, c))
                return
        if base == target:
            self.error(lineno, "%s 同币种自报价 %s/%s 无意义" % (verb, base, target))
            return
        try:
            rate = Decimal(rate_s)
        except InvalidOperation:
            self.error(lineno, "%s 汇率 %r 不是合法数字" % (verb, rate_s))
            return
        if rate <= 0:
            self.error(lineno, "%s 汇率必须为正数" % verb)
            return
        exists = (base, target) in self.rates
        if update and not exists:
            self.error(lineno, "UPDATE %s/%s 失败：无既有报价，请用 RATE" % (base, target))
            return
        if not update and exists:
            self.error(lineno,
                       "重复报价拦截：币种对 %s/%s 已有报价 %s，如需调整请用 UPDATE"
                       % (base, target, fmt_rate(self.rates[(base, target)])))
            return
        self._set_matrix(base, target, rate)
        self.run_checks("第%d行后" % lineno)

    # ---------- 折算 ----------

    def find_conversion(self, src, dst):
        """返回 (汇率, 路径列表)；无直接报价时经中间币 BFS 寻径。找不到返回 (None, None)。"""
        if src == dst:
            return Decimal(1), [src]
        prev = {src: None}
        queue = deque([src])
        while queue:
            cur = queue.popleft()
            for (a, b), r in self.rates.items():
                if a == cur and b not in prev:
                    prev[b] = (cur, r)
                    if b == dst:
                        rate = Decimal(1)
                        path = [dst]
                        node = dst
                        while prev[node] is not None:
                            p, pr = prev[node]
                            rate *= pr
                            path.append(p)
                            node = p
                        path.reverse()
                        return rate, path
                    queue.append(b)
        return None, None

    def do_txn(self, lineno, tid, ccy, amount_s):
        if tid in self.txns:
            self.error(lineno, "交易号 %s 重复" % tid)
            return
        if ccy not in self.curr_set:
            self.error(lineno, "TXN %s 引用不存在币种 %s" % (tid, ccy))
            return
        try:
            amount = Decimal(amount_s)
        except InvalidOperation:
            self.error(lineno, "TXN %s 金额 %r 不是合法数字" % (tid, amount_s))
            return
        rate, path = self.find_conversion(ccy, self.settle)
        if rate is None:
            self.error(lineno, "TXN %s 无法折算：%s 到基准币 %s 无汇率路径"
                       % (tid, ccy, self.settle))
            return
        value = amount * rate
        self.txns[tid] = {"ccy": ccy, "amount": amount,
                          "booked_rate": rate, "booked_value": value}
        self.conversions.append({
            "tid": tid, "ccy": ccy, "amount": amount,
            "path": "->".join(path), "rate": rate, "value": value,
        })

    # ---------- 重估 ----------

    def do_reval(self, lineno, date):
        if not self.txns:
            self.errors.append("第%d行: REVAL %s 时无存量交易" % (lineno, date))
            return
        for tid, t in self.txns.items():
            rate, path = self.find_conversion(t["ccy"], self.settle)
            if rate is None:
                self.error(lineno, "REVAL %s：交易 %s 的币种 %s 到 %s 汇率路径断裂"
                           % (date, tid, t["ccy"], self.settle))
                continue
            new_value = t["amount"] * rate
            diff = new_value - t["booked_value"]
            qdiff = diff.quantize(CENT, rounding=ROUND_HALF_UP)
            entry = "-"
            if qdiff > 0:
                entry = ("借:货币性项目-%s %s / 贷:汇兑损益 %s"
                         % (t["ccy"], fmt_money(qdiff), fmt_money(qdiff)))
            elif qdiff < 0:
                entry = ("借:汇兑损益 %s / 贷:货币性项目-%s %s"
                         % (fmt_money(-qdiff), t["ccy"], fmt_money(-qdiff)))
            if qdiff != 0:
                self.journal.append((date, tid, entry))
            self.revals.append({
                "date": date, "tid": tid, "ccy": t["ccy"], "amount": t["amount"],
                "old_rate": t["booked_rate"], "new_rate": rate,
                "old_value": t["booked_value"], "new_value": new_value,
                "diff": diff, "entry": entry,
            })
            t["booked_rate"] = rate
            t["booked_value"] = new_value

    # ---------- 风险检查 ----------

    def run_checks(self, tag):
        cs = sorted(self.curr_set)
        # 间接汇率 vs 直接汇率：定位偏差超限的币种对与中间路径
        for a, b in itertools.combinations(cs, 2):
            if (a, b) not in self.rates:
                continue
            direct = self.rates[(a, b)]
            for k in cs:
                if k in (a, b):
                    continue
                if (a, k) in self.rates and (k, b) in self.rates:
                    indirect = self.rates[(a, k)] * self.rates[(k, b)]
                    dev = abs(indirect / direct - 1)
                    if dev > self.threshold:
                        key = ("DEV", a, b, k, dev.quantize(DEV_Q))
                        if key not in self._seen_findings:
                            self._seen_findings.add(key)
                            self.findings.append(
                                "[%s] [汇率偏差] 币种对 %s/%s：直接汇率 %s，"
                                "经中间币 %s 间接折算 %s（路径 %s->%s->%s），"
                                "偏差 %s 超过阈值 %s"
                                % (tag, a, b, fmt_rate(direct), k, fmt_rate(indirect),
                                   a, k, b, fmt_pct(dev), fmt_pct(self.threshold)))
        # 三角套利环：a->b->c->a 乘积偏离 1
        for a, b, c in itertools.combinations(cs, 3):
            for x, y, z in ((a, b, c), (a, c, b)):
                if (x, y) in self.rates and (y, z) in self.rates and (z, x) in self.rates:
                    prod = self.rates[(x, y)] * self.rates[(y, z)] * self.rates[(z, x)]
                    dev = abs(prod - 1)
                    if dev > self.threshold:
                        key = ("ARB", x, y, z, prod.quantize(DEV_Q))
                        if key not in self._seen_findings:
                            self._seen_findings.add(key)
                            self.findings.append(
                                "[%s] [三角套利] 环 %s->%s->%s->%s 汇率乘积 %s，"
                                "偏离 1 达 %s（阈值 %s）"
                                % (tag, x, y, z, x, fmt_rate(prod),
                                   fmt_pct(dev), fmt_pct(self.threshold)))

    # ---------- 指令流 ----------

    def process_line(self, lineno, line):
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "CCY" and len(args) == 1:
                self.define_ccy(lineno, args[0])
            elif cmd == "SETTLE" and len(args) == 1:
                if args[0] not in self.curr_set:
                    self.error(lineno, "SETTLE 引用不存在币种 %s" % args[0])
                else:
                    self.settle = args[0]
            elif cmd == "THRESHOLD" and len(args) == 1:
                self.threshold = Decimal(args[0])
            elif cmd == "RATE" and len(args) == 3:
                self.add_rate(lineno, args[0], args[1], args[2], update=False)
            elif cmd == "UPDATE" and len(args) == 3:
                self.add_rate(lineno, args[0], args[1], args[2], update=True)
            elif cmd == "TXN" and len(args) == 3:
                self.do_txn(lineno, args[0], args[1], args[2])
            elif cmd == "REVAL" and len(args) == 1:
                self.do_reval(lineno, args[0])
            elif cmd == "CHECK" and len(args) == 0:
                self.run_checks("第%d行 CHECK" % lineno)
            else:
                self.error(lineno, "无法识别的指令或参数个数错误: %s" % line)
        except InvalidOperation:
            self.error(lineno, "数字解析失败: %s" % line)

    # ---------- 输出 ----------

    def report(self, out):
        w = lambda s="": out.write(s + "\n")
        w("========== 折算清单（基准币: %s） ==========" % self.settle)
        w("%-8s %-6s %14s  %-14s %12s  %14s" % ("交易号", "币种", "原币金额", "折算路径", "适用汇率", "基准币金额"))
        for c in self.conversions:
            w("%-8s %-6s %14s  %-14s %12s  %14s" % (
                c["tid"], c["ccy"], fmt_money(c["amount"]), c["path"],
                fmt_rate(c["rate"]), fmt_money(c["value"])))
        if not self.conversions:
            w("（无）")
        w()
        w("========== 重估清单 ==========")
        w("%-12s %-8s %-6s %14s %12s %12s %14s %14s %12s  %s" % (
            "日期", "交易号", "币种", "原币金额", "原汇率", "新汇率",
            "账面值", "重估值", "差额", "入账分录"))
        for r in self.revals:
            w("%-12s %-8s %-6s %14s %12s %12s %14s %14s %12s  %s" % (
                r["date"], r["tid"], r["ccy"], fmt_money(r["amount"]),
                fmt_rate(r["old_rate"]), fmt_rate(r["new_rate"]),
                fmt_money(r["old_value"]), fmt_money(r["new_value"]),
                fmt_money(r["diff"]), r["entry"]))
        if not self.revals:
            w("（无）")
        w()
        w("========== 错误与风险报告 ==========")
        if not self.errors and not self.findings:
            w("（无错误，无风险事项）")
        for e in self.errors:
            w("[错误] " + e)
        for f in self.findings:
            w(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨境结算汇率折算/重估/风险检查工具（纯标准库）")
    ap.add_argument("input", nargs="?", help="指令流文件（缺省读 stdin）")
    ap.add_argument("--threshold", default="0.005",
                    help="偏差阈值，小数形式（缺省 0.005 即 0.5%%）")
    args = ap.parse_args(argv)

    engine = FxEngine(Decimal(args.threshold))
    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            lines = fh.readlines()
    else:
        lines = sys.stdin.readlines()
    for lineno, line in enumerate(lines, 1):
        engine.process_line(lineno, line)
    engine.run_checks("期末")
    engine.report(sys.stdout)
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
