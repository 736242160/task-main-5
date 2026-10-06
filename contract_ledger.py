#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contract_ledger.py — 合同分期收款履约台账工具（纯 Python 标准库，单文件）

用法:
    python3 contract_ledger.py 输入文件 [更多输入文件...]
    python3 contract_ledger.py --demo          # 运行内置示例（含分期重算）

输入格式（每行一条事件，自上而下顺序处理，跨流状态在同一台账上延续）:
    CONTRACT  <合同号> <总额> <期1>,<期2>,...     期次格式: 金额 或 YYYY-MM-DD:金额
    PAY       <合同号> <金额> <YYYY-MM-DD>
    OVERDUE   <合同号> <逾期天数>
    TERMINATE <合同号> [YYYY-MM-DD]
    # 注释（整行或行尾）；空行忽略。金额单位为元，最多两位小数。

自定规则（实现约定）:
    1. 逐期核销: 收款按期次顺序冲销（先冲最早未付清期），一笔收款可跨多期分配；
       填满全部未付期次后仍有剩余的，剩余部分拦截不入账，并报告超收额。
    2. 逾期级联: OVERDUE 将最早未付清期标记为逾期（记录逾期天数），其后所有
       未付期次级联标记为"级联预警"；期次付清后标记自动消除。
    3. 提前终止重算: 所有未付清期次作废（重算为 0）；
       重算后应付 = 已完全核销期次的计划额之和；
       应退 = 累计已收 - 重算后应付（因超收已被拦截，应退 >= 0）。
    4. 拦截与报告: 已终止合同再收款、引用不存在合同、重复终止、总额与分期
       计划不一致、重复定义合同等，均拦截并计入错误清单，不影响其他合同。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


# ---------------------------------------------------------------- 基础工具

def parse_money(text: str) -> int:
    """把元为单位的字符串解析为整数分，非法则抛 ValueError。"""
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"金额格式非法: {text!r}")
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"金额格式非法: {text!r}")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"金额最多两位小数: {text!r}")
    cents = int(cents)
    if cents < 0:
        raise ValueError(f"金额不能为负: {text!r}")
    return cents


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_date(text: str) -> str:
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"日期格式非法(应为 YYYY-MM-DD): {text!r}")


# ---------------------------------------------------------------- 数据模型

@dataclass
class Installment:
    seq: int
    due: str | None
    amount: int                 # 计划金额（分）
    allocated: int = 0          # 已核销（分）
    overdue_days: int = 0       # >0 表示逾期
    cascaded: bool = False      # 级联预警
    cancelled: bool = False     # 终止后作废

    @property
    def outstanding(self) -> int:
        return self.amount - self.allocated

    @property
    def paid_off(self) -> bool:
        return self.allocated >= self.amount

    def status_text(self) -> str:
        if self.cancelled:
            return "已作废(重算为0)"
        if self.paid_off:
            return "已付清"
        base = "部分付款" if self.allocated > 0 else "待收"
        if self.overdue_days:
            return f"逾期{self.overdue_days}天·{base}"
        if self.cascaded:
            return f"级联预警·{base}"
        return base


@dataclass
class Contract:
    cid: str
    total: int
    installments: list = field(default_factory=list)
    received: int = 0           # 累计已入账（分）
    terminated: bool = False
    term_date: str | None = None
    earned: int = 0             # 终止重算后应付
    refund: int = 0             # 终止重算应退

    def open_installments(self):
        return [i for i in self.installments if not i.cancelled and not i.paid_off]

    def overall_status(self) -> str:
        if self.terminated:
            return "已终止"
        if all(i.paid_off for i in self.installments):
            return "已结清"
        return "履约中"


# ---------------------------------------------------------------- 台账引擎

class Ledger:
    def __init__(self):
        self.contracts: dict[str, Contract] = {}
        self.order: list[str] = []
        self.errors: list[str] = []
        self.logs: list[str] = []

    # ---- 事件入口 ----

    def process_line(self, raw: str, lineno: int, source: str):
        line = raw.split("#", 1)[0].strip()
        if not line:
            return
        tokens = line.split()
        cmd, args = tokens[0].upper(), tokens[1:]
        handler = {
            "CONTRACT": self._do_contract,
            "PAY": self._do_pay,
            "OVERDUE": self._do_overdue,
            "TERMINATE": self._do_terminate,
        }.get(cmd)
        if handler is None:
            self._err(lineno, source, f"未知指令 {tokens[0]!r}，已忽略")
            return
        try:
            handler(args, lineno, source)
        except ValueError as exc:
            self._err(lineno, source, str(exc))

    # ---- 各流处理 ----

    def _do_contract(self, args, lineno, source):
        if len(args) != 3:
            raise ValueError("CONTRACT 需要: <合同号> <总额> <期1,期2,...>")
        cid, total_text, plan_text = args
        if cid in self.contracts:
            self._err(lineno, source, f"合同 {cid} 重复定义，已忽略")
            return
        total = parse_money(total_text)
        installments = []
        for seq, item in enumerate(plan_text.split(","), start=1):
            item = item.strip()
            if not item:
                raise ValueError("分期计划中存在空期次")
            if ":" in item:
                due_text, amount_text = item.split(":", 1)
                due = parse_date(due_text.strip())
            else:
                due, amount_text = None, item
            amount = parse_money(amount_text.strip())
            if amount <= 0:
                raise ValueError(f"第{seq}期金额必须大于0")
            installments.append(Installment(seq=seq, due=due, amount=amount))
        planned = sum(i.amount for i in installments)
        if planned != total:
            self._err(lineno, source,
                      f"合同 {cid} 总额 {fmt(total)} 与分期计划合计 {fmt(planned)} 不一致，合同未建立")
            return
        self.contracts[cid] = Contract(cid=cid, total=total, installments=installments)
        self.order.append(cid)
        self.logs.append(f"[{source}:{lineno}] 建约 {cid} 总额 {fmt(total)} 共{len(installments)}期")

    def _do_pay(self, args, lineno, source):
        if len(args) != 3:
            raise ValueError("PAY 需要: <合同号> <金额> <YYYY-MM-DD>")
        cid, amount_text, date_text = args
        amount = parse_money(amount_text)
        date = parse_date(date_text)
        if amount <= 0:
            raise ValueError("收款金额必须大于0")
        contract = self._get_contract(cid, lineno, source)
        if contract is None:
            return
        if contract.terminated:
            self._err(lineno, source,
                      f"合同 {cid} 已于 {contract.term_date} 终止，收款 {fmt(amount)} 全额拦截")
            return
        remaining = amount
        parts = []
        for inst in contract.open_installments():
            if remaining <= 0:
                break
            take = min(remaining, inst.outstanding)
            inst.allocated += take
            remaining -= take
            parts.append(f"期{inst.seq}+{fmt(take)}")
        applied = amount - remaining
        contract.received += applied
        self.logs.append(
            f"[{source}:{lineno}] 收款 {cid} {fmt(amount)} {date} -> "
            + ("、".join(parts) if parts else "无可核销期次")
            + (f"；超收 {fmt(remaining)} 已拦截" if remaining else ""))
        if remaining > 0:
            self._err(lineno, source,
                      f"合同 {cid} 收款 {fmt(amount)} 超出全部未付期次应付，"
                      f"超收 {fmt(remaining)} 已拦截（入账 {fmt(applied)}）")

    def _do_overdue(self, args, lineno, source):
        if len(args) != 2:
            raise ValueError("OVERDUE 需要: <合同号> <逾期天数>")
        cid, days_text = args
        try:
            days = int(days_text)
        except ValueError:
            raise ValueError(f"逾期天数格式非法: {days_text!r}")
        if days <= 0:
            raise ValueError("逾期天数必须为正整数")
        contract = self._get_contract(cid, lineno, source)
        if contract is None:
            return
        if contract.terminated:
            self._err(lineno, source, f"合同 {cid} 已终止，逾期事件忽略")
            return
        open_insts = contract.open_installments()
        if not open_insts:
            self._err(lineno, source, f"合同 {cid} 已无未付期次，逾期事件忽略")
            return
        first = open_insts[0]
        first.overdue_days = days
        for inst in open_insts[1:]:
            inst.cascaded = True
        self.logs.append(
            f"[{source}:{lineno}] 逾期 {cid} {days}天 -> 期{first.seq} 标记逾期，"
            f"期{','.join(str(i.seq) for i in open_insts[1:]) or '无'} 级联预警")

    def _do_terminate(self, args, lineno, source):
        if len(args) not in (1, 2):
            raise ValueError("TERMINATE 需要: <合同号> [YYYY-MM-DD]")
        cid = args[0]
        date = parse_date(args[1]) if len(args) == 2 else "(未指定日期)"
        contract = self._get_contract(cid, lineno, source)
        if contract is None:
            return
        if contract.terminated:
            self._err(lineno, source,
                      f"合同 {cid} 已于 {contract.term_date} 终止，重复终止已拦截")
            return
        cancelled = []
        for inst in contract.installments:
            if not inst.paid_off:
                inst.cancelled = True
                cancelled.append(inst)
        contract.terminated = True
        contract.term_date = date
        # 重算：应付 = 已完全核销期次计划额之和；应退 = 已收 - 重算后应付
        contract.earned = sum(i.amount for i in contract.installments if i.paid_off)
        contract.refund = contract.received - contract.earned
        self.logs.append(
            f"[{source}:{lineno}] 终止 {cid} {date} -> 作废期次: "
            f"{','.join('期%d' % i.seq for i in cancelled) or '无'}；"
            f"重算后应付 {fmt(contract.earned)}，累计已收 {fmt(contract.received)}，"
            f"应退 {fmt(contract.refund)}")

    # ---- 辅助 ----

    def _get_contract(self, cid, lineno, source):
        contract = self.contracts.get(cid)
        if contract is None:
            self._err(lineno, source, f"引用了不存在的合同 {cid}，事件已忽略")
        return contract

    def _err(self, lineno, source, message):
        self.errors.append(f"[{source}:{lineno}] {message}")

    # ---- 输出 ----

    def render(self) -> str:
        out = ["===== 合同履约状态 ====="]
        for cid in self.order:
            c = self.contracts[cid]
            out.append(f"\n合同 {c.cid}  总额 {fmt(c.total)}  累计已收 {fmt(c.received)}"
                       f"  状态: {c.overall_status()}")
            for inst in c.installments:
                due = inst.due or "-"
                out.append(f"  期{inst.seq:<3} 到期 {due}  计划 {fmt(inst.amount):>10}"
                           f"  已核销 {fmt(inst.allocated):>10}  {inst.status_text()}")
            if c.terminated:
                out.append(f"  >> 提前终止({c.term_date}) 重算后应付 {fmt(c.earned)}"
                           f"  应退 {fmt(c.refund)}")
        out.append("\n===== 处理日志 =====")
        out.extend(self.logs or ["(无)"])
        out.append("\n===== 错误/拦截清单 =====")
        out.extend(self.errors or ["(无)"])
        return "\n".join(out)


# ---------------------------------------------------------------- 内置示例

DEMO_INPUT = """\
# ---- 合同定义流 ----
CONTRACT C001 10000 2026-01-31:4000,2026-02-28:3000,2026-03-31:3000
CONTRACT C002 6000 2000,2000,2000
CONTRACT C003 5000 2500,2500
CONTRACT C004 1000 600,600            # 总额与计划不一致 -> 报错不建约
# ---- 收款流：逐期核销 + 跨期分配 ----
PAY C001 4000 2026-01-30              # 恰好核销第1期
PAY C001 5000 2026-02-15              # 跨期：第2期3000 + 第3期2000
PAY C002 1500 2026-02-01              # 第1期部分付款
# ---- 逾期流：级联标记 ----
OVERDUE C002 30                       # 期1逾期30天，期2/期3级联预警
# ---- 超收拦截 ----
PAY C001 2000 2026-03-01              # C001仅剩1000应付，超收1000被拦截
# ---- 提前终止：剩余期重算 ----
TERMINATE C002 2026-03-05             # 未付清期次作废，重算应退
PAY C002 1000 2026-03-06              # 已终止合同再收款 -> 拦截
TERMINATE C002                        # 重复终止 -> 拦截
# ---- 引用不存在合同 ----
PAY C999 100 2026-03-01
OVERDUE C999 10
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="合同分期收款履约台账工具")
    parser.add_argument("inputs", nargs="*", help="输入文件（可多个，按给定顺序处理）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例（含分期重算）")
    args = parser.parse_args(argv)

    ledger = Ledger()
    if args.demo:
        for lineno, raw in enumerate(DEMO_INPUT.splitlines(), start=1):
            ledger.process_line(raw, lineno, "<demo>")
    for path in args.inputs:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    ledger.process_line(raw, lineno, path)
        except OSError as exc:
            print(f"无法读取输入文件 {path}: {exc}", file=sys.stderr)
            return 2
    if not args.demo and not args.inputs:
        parser.print_help()
        return 0
    print(ledger.render())
    return 1 if ledger.errors else 0


if __name__ == "__main__":
    sys.exit(main())
