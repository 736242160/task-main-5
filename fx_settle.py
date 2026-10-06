#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fx_settle.py — 跨境结算汇率折算 / 重估 / 校验单文件工具（纯 Python 标准库）

指令流（按行处理，四类流可混排，状态跨流延续；# 后为注释）：

  币种定义流：
    CUR    <币种>                    定义币种（如 CUR USD）
    BASE   <币种>                    设定记账基准币（折算/重估的目标币）
    TOL    <小数>                    偏差阈值（默认 0.005 = 0.5%）

  汇率流（1 基准币 = 汇率 目标币，矩阵自动维护双向）：
    RATE   <基准币> <目标币> <汇率>   首次报价；同币种对重复报价将被拦截
    UPDATE <基准币> <目标币> <汇率>   更新既有报价（供后续重估使用）

  交易流：
    TX     <交易号> <币种> <金额>     按最新汇率折算为基准币入账

  重估流：
    REVAL  <日期>                    全部存量交易按最新汇率重估，差额计入重估损益

  其他：
    CHECK                            立即执行 间接/直接偏差 与 三角套利环 检查
    REPORT                           立即输出全部清单与报告

输入结束后自动执行一次 CHECK 与 REPORT。

重估差额入账规则（自定）：
  差额 = 按最新汇率折算的新账面值 - 原账面值；正为收益、负为损失。
  逐笔调整交易账面值与账面汇率，差额按重估日期汇总计入“重估损益”科目。

用法：
  python3 fx_settle.py 脚本文件      # 从文件读取指令流
  python3 fx_settle.py               # 从标准输入读取
  python3 fx_settle.py --demo        # 运行内置样例（含汇率偏差定位）
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import combinations

CENT = Decimal("0.01")
RATE_Q = Decimal("0.000001")
PCT_Q = Decimal("0.0001")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_amount(x: Decimal) -> str:
    return format(q2(x), "f")


def fmt_signed(x: Decimal) -> str:
    v = q2(x)
    return ("+" if v >= 0 else "-") + format(abs(v), "f")


def fmt_rate(x: Decimal) -> str:
    s = format(x.quantize(RATE_Q, rounding=ROUND_HALF_UP), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_pct(x: Decimal) -> str:
    return format((x * 100).quantize(PCT_Q, rounding=ROUND_HALF_UP), "f") + "%"


class Engine:
    def __init__(self) -> None:
        self.currencies: set[str] = set()
        self.base: str | None = None
        self.tol = Decimal("0.005")
        self.rates: dict[tuple[str, str], Decimal] = {}  # (基准币, 目标币) -> 汇率，含反向
        self.txs: dict[str, dict] = {}
        self.tx_order: list[str] = []
        self.errors: list[str] = []
        self.conversions: list[str] = []
        self.revals: list[tuple[str, list[str], Decimal]] = []
        self.reval_pnl = Decimal("0")
        self.deviation_reports: list[str] = []
        self.triangle_reports: list[str] = []

    # ---------- 基础工具 ----------

    def err(self, lineno: int, msg: str) -> None:
        self.errors.append(f"第{lineno}行: {msg}")

    @staticmethod
    def parse_decimal(s: str) -> Decimal | None:
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    def find_path(self, src: str, dst: str) -> list[str] | None:
        """在汇率矩阵上用 BFS 找 src -> dst 的最短折算路径。"""
        if src == dst:
            return [src]
        adj: dict[str, list[str]] = {}
        for (u, v) in self.rates:
            adj.setdefault(u, []).append(v)
        prev: dict[str, str | None] = {src: None}
        queue = deque([src])
        while queue:
            u = queue.popleft()
            for v in adj.get(u, []):
                if v in prev:
                    continue
                prev[v] = u
                if v == dst:
                    path = [dst]
                    while path[-1] is not None:
                        path.append(prev[path[-1]])
                    path.pop()
                    return list(reversed(path))
                queue.append(v)
        return None

    def path_rate(self, path: list[str]) -> Decimal:
        r = Decimal(1)
        for u, v in zip(path, path[1:]):
            r *= self.rates[(u, v)]
        return r

    # ---------- 指令处理 ----------

    def cmd_cur(self, lineno: int, ccy: str) -> None:
        if ccy in self.currencies:
            self.err(lineno, f"币种 {ccy} 重复定义")
            return
        self.currencies.add(ccy)

    def cmd_base(self, lineno: int, ccy: str) -> None:
        if ccy not in self.currencies:
            self.err(lineno, f"BASE 引用不存在币种 {ccy}")
            return
        self.base = ccy

    def cmd_tol(self, lineno: int, s: str) -> None:
        v = self.parse_decimal(s)
        if v is None or v <= 0:
            self.err(lineno, f"TOL 阈值非法: {s}")
            return
        self.tol = v

    def cmd_rate(self, lineno: int, b: str, q: str, s: str, update: bool) -> None:
        for c in (b, q):
            if c not in self.currencies:
                self.err(lineno, f"{'UPDATE' if update else 'RATE'} 引用不存在币种 {c}")
                return
        if b == q:
            self.err(lineno, f"币种对相同: {b}/{q}")
            return
        r = self.parse_decimal(s)
        if r is None or r <= 0:
            self.err(lineno, f"汇率非法: {s}")
            return
        exists = (b, q) in self.rates or (q, b) in self.rates
        if not update and exists:
            self.err(lineno, f"重复报价拦截: {b}/{q} 已存在报价 "
                             f"{fmt_rate(self.rates.get((b, q), 1 / self.rates[(q, b)]))}，"
                             f"如需调整请用 UPDATE")
            return
        if update and not exists:
            self.err(lineno, f"UPDATE 失败: {b}/{q} 无既有报价，请先用 RATE")
            return
        self.rates[(b, q)] = r
        self.rates[(q, b)] = Decimal(1) / r  # 双向维护

    def cmd_tx(self, lineno: int, txid: str, ccy: str, s: str) -> None:
        if self.base is None:
            self.err(lineno, "尚未设定 BASE 记账基准币，无法折算")
            return
        if ccy not in self.currencies:
            self.err(lineno, f"TX {txid} 引用不存在币种 {ccy}")
            return
        if txid in self.txs:
            self.err(lineno, f"交易号重复: {txid}")
            return
        amount = self.parse_decimal(s)
        if amount is None:
            self.err(lineno, f"TX {txid} 金额非法: {s}")
            return
        path = self.find_path(ccy, self.base)
        if path is None:
            self.err(lineno, f"TX {txid} 币种 {ccy} 到基准币 {self.base} 无汇率路径，无法折算")
            return
        rate = self.path_rate(path)
        value = amount * rate
        self.txs[txid] = {"ccy": ccy, "amount": amount,
                          "booked_rate": rate, "booked_value": value}
        self.tx_order.append(txid)
        self.conversions.append(
            f"TX {txid} {ccy} {fmt_amount(amount)} -> {self.base} {fmt_amount(value)} "
            f"@ {fmt_rate(rate)} ({'->'.join(path)})")

    def cmd_reval(self, lineno: int, date: str) -> None:
        if self.base is None:
            self.err(lineno, "尚未设定 BASE 记账基准币，无法重估")
            return
        entries: list[str] = []
        total = Decimal(0)
        for txid in self.tx_order:
            tx = self.txs[txid]
            path = self.find_path(tx["ccy"], self.base)
            if path is None:
                self.err(lineno, f"REVAL {date} 交易 {txid} 币种 {tx['ccy']} 无汇率路径，跳过重估")
                continue
            rate = self.path_rate(path)
            new_value = tx["amount"] * rate
            diff = new_value - tx["booked_value"]
            entries.append(
                f"  {txid} {tx['ccy']} {fmt_amount(tx['amount'])} "
                f"账面 {fmt_amount(tx['booked_value'])} -> 重估 {fmt_amount(new_value)} "
                f"@ {fmt_rate(rate)} ({'->'.join(path)}) 差额 {fmt_signed(diff)} {self.base}")
            tx["booked_rate"] = rate
            tx["booked_value"] = new_value
            total += diff
        self.reval_pnl += total
        self.revals.append((date, entries, total))

    # ---------- 校验 ----------

    def check_deviations(self) -> None:
        """间接汇率（经中间币）与直接汇率偏差超限 -> 定位币种对与中间币。"""
        self.deviation_reports.clear()
        seen: set[frozenset] = set()
        for (b, q) in sorted(self.rates):
            key = frozenset((b, q))
            if key in seen:
                continue
            seen.add(key)
            direct = self.rates[(b, q)]
            for m in sorted(self.currencies):
                if m in (b, q):
                    continue
                r1 = self.rates.get((b, m))
                r2 = self.rates.get((m, q))
                if r1 is None or r2 is None:
                    continue
                indirect = r1 * r2
                dev = abs(indirect - direct) / direct
                if dev > self.tol:
                    self.deviation_reports.append(
                        f"币种对 {b}/{q}: 直接 {fmt_rate(direct)}，"
                        f"经 {m} 间接 {fmt_rate(indirect)} "
                        f"({b}->{m}->{q})，偏差 {fmt_pct(dev)} > 阈值 {fmt_pct(self.tol)}")

    def check_triangles(self) -> None:
        """三角套利环: a->b->c->a 汇率乘积偏离 1 超限。"""
        self.triangle_reports.clear()
        for a, b, c in combinations(sorted(self.currencies), 3):
            r_ab = self.rates.get((a, b))
            r_bc = self.rates.get((b, c))
            r_ca = self.rates.get((c, a))
            if r_ab is None or r_bc is None or r_ca is None:
                continue
            product = r_ab * r_bc * r_ca
            dev = abs(product - 1)
            if dev > self.tol:
                self.triangle_reports.append(
                    f"套利环 {a}->{b}->{c}->{a}: 乘积 {fmt_rate(product)}，"
                    f"偏离 1 达 {fmt_pct(dev)} > 阈值 {fmt_pct(self.tol)}")

    def run_checks(self) -> None:
        self.check_deviations()
        self.check_triangles()

    # ---------- 报告 ----------

    def report(self, out=sys.stdout) -> None:
        p = lambda s="": print(s, file=out)
        p("== 折算清单 ==")
        if self.conversions:
            for line in self.conversions:
                p(line)
        else:
            p("(无)")
        p()
        p("== 重估清单 ==")
        if self.revals:
            for date, entries, total in self.revals:
                p(f"REVAL {date}")
                for e in entries:
                    p(e)
                p(f"  重估差额入账(重估损益-{date}): {fmt_signed(total)} {self.base}")
            p(f"重估损益累计: {fmt_signed(self.reval_pnl)} {self.base}")
        else:
            p("(无)")
        p()
        p("== 间接/直接汇率偏差报告 ==")
        if self.deviation_reports:
            for line in self.deviation_reports:
                p(line)
        else:
            p("(无超限偏差)")
        p()
        p("== 三角套利环报告 ==")
        if self.triangle_reports:
            for line in self.triangle_reports:
                p(line)
        else:
            p("(无套利环)")
        p()
        p("== 错误报告 ==")
        if self.errors:
            for line in self.errors:
                p(line)
        else:
            p("(无错误)")

    # ---------- 指令分发 ----------

    def run_line(self, lineno: int, line: str) -> None:
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "CUR" and len(args) == 1:
                self.cmd_cur(lineno, args[0])
            elif cmd == "BASE" and len(args) == 1:
                self.cmd_base(lineno, args[0])
            elif cmd == "TOL" and len(args) == 1:
                self.cmd_tol(lineno, args[0])
            elif cmd == "RATE" and len(args) == 3:
                self.cmd_rate(lineno, *args, update=False)
            elif cmd == "UPDATE" and len(args) == 3:
                self.cmd_rate(lineno, *args, update=True)
            elif cmd == "TX" and len(args) == 3:
                self.cmd_tx(lineno, *args)
            elif cmd == "REVAL" and len(args) == 1:
                self.cmd_reval(lineno, args[0])
            elif cmd == "CHECK" and not args:
                self.run_checks()
                self._print_checks()
            elif cmd == "REPORT" and not args:
                self.run_checks()
                self.report()
            else:
                self.err(lineno, f"无法识别的指令或参数个数错误: {line}")
        except Exception as exc:  # 单行失败不中断后续流
            self.err(lineno, f"处理异常: {exc}")

    def _print_checks(self, out=sys.stdout) -> None:
        p = lambda s="": print(s, file=out)
        p("== 间接/直接汇率偏差报告 ==")
        p("\n".join(self.deviation_reports) if self.deviation_reports else "(无超限偏差)")
        p()
        p("== 三角套利环报告 ==")
        p("\n".join(self.triangle_reports) if self.triangle_reports else "(无套利环)")
        p()


DEMO_SCRIPT = """\
# ===== 币种定义流 =====
CUR CNY
CUR USD
CUR EUR
CUR JPY
BASE CNY
TOL 0.005
# ===== 汇率流（1 基准币 = 汇率 目标币）=====
RATE USD CNY 7.1000
RATE EUR CNY 7.8000
RATE USD EUR 0.9000
RATE USD JPY 150.00
# ===== 交易流 =====
TX T001 USD 1000.00
TX T002 EUR 500.00
TX T003 GBP 100.00
TX T004 JPY 15000
# 同币种对重复报价（应被拦截）
RATE USD CNY 7.2000
# ===== 汇率更新 =====
UPDATE USD CNY 7.2500
# ===== 重估流 =====
REVAL 2026-10-06
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨境结算汇率折算/重估/校验工具（纯标准库）")
    ap.add_argument("script", nargs="?", help="指令流文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置样例（含汇率偏差定位）")
    ns = ap.parse_args(argv)

    if ns.demo:
        text = DEMO_SCRIPT
        print("== 输入指令流（内置样例）==")
        print(text)
    elif ns.script:
        with open(ns.script, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    engine = Engine()
    for lineno, line in enumerate(text.splitlines(), 1):
        engine.run_line(lineno, line)
    engine.run_checks()
    engine.report()
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
