#!/usr/bin/env python3
"""合同分期收款履约管理工具（纯 Python 标准库，单文件）。

输入指令流（按行顺序处理，跨流状态延续）：
    CONTRACT  <合同号> <总额> <每期金额,逗号分隔>
    PAY       <合同号> <金额> <YYYY-MM-DD>
    OVERDUE   <合同号> <逾期天数>
    TERMINATE <合同号>
    # 井号开头为注释，空行忽略

业务规则（自定，确定可复现）：
    1. 核销：收款按期号从小到大逐期核销；当期结清后余额自动滚入下一期，
       支持一笔收款跨多期分配。
    2. 超收：单笔收款超过合同剩余应付总额时，整笔拦截不入账，
       报告超收额（= 收款额 - 剩余应付）。
    3. 逾期：OVERDUE 作用于当前最早未结清期，记录逾期天数；
       逾期天数 > 宽限期 GRACE_DAYS(30) 时，后续所有未结清期级联标记为
       CASCADED_OVERDUE；该期之后被结清时级联标记自动清除。
    4. 提前终止：TERMINATE 将所有未结清期取消（CANCELLED），剩余未付
       金额合并重算为单一结算期（期号 S，状态 SETTLEMENT，立即到期），
       合同状态变为 TERMINATED。
    5. 拦截与报告：已终止合同再收款、重复终止、引用不存在合同、
       金额非法、总额与分期计划不符，均记入错误清单，不影响其他合同。

用法：
    python3 contract_installments.py 输入文件
    python3 contract_installments.py          # 从 stdin 读
    python3 contract_installments.py --demo   # 运行内置示例（含分期重算）
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

GRACE_DAYS = 30  # 逾期宽限期（天），超过则级联标记后续期


# ---------------------------------------------------------------- 数据模型

@dataclass
class Installment:
    seq: str            # 期号："1".."N"，终止重算后的结算期为 "S"
    amount: Decimal     # 当期应付
    paid: Decimal = Decimal("0")
    cancelled: bool = False
    overdue_days: int = 0       # >0 表示被标记逾期
    cascaded: bool = False      # 被上游逾期级联标记

    @property
    def outstanding(self) -> Decimal:
        return Decimal("0") if self.cancelled else self.amount - self.paid

    @property
    def status(self) -> str:
        if self.cancelled:
            return "CANCELLED"
        if self.paid >= self.amount:
            return "PAID"
        if self.seq == "S":
            return "SETTLEMENT"
        if self.overdue_days > 0:
            return "OVERDUE"
        if self.cascaded:
            return "CASCADED_OVERDUE"
        if self.paid > 0:
            return "PARTIAL"
        return "PENDING"


@dataclass
class Contract:
    cid: str
    total: Decimal
    installments: list = field(default_factory=list)
    terminated: bool = False
    payments: list = field(default_factory=list)  # (date, amount, [(seq, alloc), ...])

    @property
    def paid_total(self) -> Decimal:
        return sum((i.paid for i in self.installments), Decimal("0"))

    @property
    def remaining(self) -> Decimal:
        return sum((i.outstanding for i in self.installments), Decimal("0"))

    @property
    def status(self) -> str:
        if self.terminated:
            return "TERMINATED"
        if self.remaining == 0:
            return "COMPLETED"
        return "ACTIVE"


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self):
        self.contracts = {}
        self.errors = []  # (行号, 类别, 描述)

    def _err(self, lineno, category, msg):
        self.errors.append((lineno, category, msg))

    # ---- 合同定义 ----
    def add_contract(self, lineno, cid, total, plan):
        if cid in self.contracts:
            self._err(lineno, "重复定义", f"合同 {cid} 已存在，忽略本次定义")
            return
        if total <= 0 or not plan or any(p <= 0 for p in plan):
            self._err(lineno, "非法金额", f"合同 {cid} 总额或分期金额必须为正数")
            return
        if sum(plan) != total:
            self._err(lineno, "计划不符",
                      f"合同 {cid} 分期合计 {sum(plan)} 与总额 {total} 不一致，合同未建立")
            return
        c = Contract(cid=cid, total=total)
        c.installments = [Installment(seq=str(i + 1), amount=p) for i, p in enumerate(plan)]
        self.contracts[cid] = c

    # ---- 收款核销（逐期、可跨期）----
    def pay(self, lineno, cid, amount, pay_date):
        c = self.contracts.get(cid)
        if c is None:
            self._err(lineno, "合同不存在", f"PAY 引用了不存在的合同 {cid}，收款 {amount} 未入账")
            return
        if c.terminated:
            self._err(lineno, "合同已终止",
                      f"合同 {cid} 已终止，收款 {amount}（{pay_date}）被拦截")
            return
        if amount <= 0:
            self._err(lineno, "非法金额", f"合同 {cid} 收款金额 {amount} 非正数，被拦截")
            return
        if amount > c.remaining:
            self._err(lineno, "超收拦截",
                      f"合同 {cid} 收款 {amount} 超过剩余应付 {c.remaining}，"
                      f"整笔拦截，超收额 {amount - c.remaining}")
            return
        left = amount
        alloc = []
        for inst in c.installments:
            if left <= 0:
                break
            if inst.cancelled or inst.outstanding <= 0:
                continue
            take = min(left, inst.outstanding)
            inst.paid += take
            left -= take
            alloc.append((inst.seq, take))
            if inst.paid >= inst.amount:
                inst.overdue_days = 0
                inst.cascaded = False
        c.payments.append((pay_date, amount, alloc))

    # ---- 逾期标记（级联）----
    def overdue(self, lineno, cid, days):
        c = self.contracts.get(cid)
        if c is None:
            self._err(lineno, "合同不存在", f"OVERDUE 引用了不存在的合同 {cid}")
            return
        if c.terminated:
            self._err(lineno, "合同已终止", f"合同 {cid} 已终止，逾期标记无效")
            return
        current = next((i for i in c.installments if not i.cancelled and i.outstanding > 0), None)
        if current is None:
            self._err(lineno, "无逾期对象", f"合同 {cid} 已全部结清，无逾期对象")
            return
        current.overdue_days = days
        if days > GRACE_DAYS:
            seen = False
            for inst in c.installments:
                if inst is current:
                    seen = True
                    continue
                if seen and not inst.cancelled and inst.outstanding > 0:
                    inst.cascaded = True

    # ---- 提前终止（剩余期重算）----
    def terminate(self, lineno, cid):
        c = self.contracts.get(cid)
        if c is None:
            self._err(lineno, "合同不存在", f"TERMINATE 引用了不存在的合同 {cid}")
            return
        if c.terminated:
            self._err(lineno, "重复终止", f"合同 {cid} 已终止，本次 TERMINATE 重复，被拦截")
            return
        rest = c.remaining
        if rest == 0:
            self._err(lineno, "无需终止", f"合同 {cid} 已全部结清，终止无效")
            return
        for inst in c.installments:
            if inst.outstanding > 0:
                inst.cancelled = True
        c.installments.append(Installment(seq="S", amount=rest))
        c.terminated = True


# ---------------------------------------------------------------- 解析

def parse_amount(lineno, text, errors):
    try:
        return Decimal(text)
    except InvalidOperation:
        errors.append((lineno, "非法金额", f"无法解析金额 {text!r}"))
        return None


def run(lines):
    engine = Engine()
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        op, args = parts[0].upper(), parts[1:]
        if op == "CONTRACT" and len(args) >= 3:
            total = parse_amount(lineno, args[1], engine.errors)
            plan = []
            ok = total is not None
            for p in args[2].split(","):
                v = parse_amount(lineno, p, engine.errors)
                if v is None:
                    ok = False
                else:
                    plan.append(v)
            if ok:
                engine.add_contract(lineno, args[0], total, plan)
        elif op == "PAY" and len(args) == 3:
            amount = parse_amount(lineno, args[1], engine.errors)
            try:
                pay_date = date.fromisoformat(args[2])
            except ValueError:
                engine._err(lineno, "非法日期", f"无法解析日期 {args[2]!r}")
                continue
            if amount is not None:
                engine.pay(lineno, args[0], amount, pay_date)
        elif op == "OVERDUE" and len(args) == 2:
            try:
                days = int(args[1])
                if days <= 0:
                    raise ValueError
            except ValueError:
                engine._err(lineno, "非法天数", f"逾期天数 {args[1]!r} 不是正整数")
                continue
            engine.overdue(lineno, args[0], days)
        elif op == "TERMINATE" and len(args) == 1:
            engine.terminate(lineno, args[0])
        else:
            engine._err(lineno, "语法错误", f"无法识别的指令: {line!r}")
    return engine


# ---------------------------------------------------------------- 报告

def report(engine):
    out = ["=" * 68, "合同履约状态", "=" * 68]
    for cid, c in engine.contracts.items():
        out.append(f"\n合同 {cid}  状态={c.status}  总额={c.total}  "
                   f"已收={c.paid_total}  剩余应付={c.remaining}")
        out.append(f"  {'期号':<4} {'应付':>10} {'已收':>10} {'未收':>10}  状态")
        for i in c.installments:
            extra = f" (逾期{i.overdue_days}天)" if i.overdue_days and not i.cancelled else ""
            out.append(f"  {i.seq:<4} {i.amount:>10} {i.paid:>10} {i.outstanding:>10}  "
                       f"{i.status}{extra}")
        for pay_date, amount, alloc in c.payments:
            detail = " + ".join(f"期{seq}:{amt}" for seq, amt in alloc)
            out.append(f"  收款 {pay_date} 金额={amount}  核销: {detail}")
    out.append("\n" + "=" * 68)
    out.append("错误清单")
    out.append("=" * 68)
    if not engine.errors:
        out.append("（无错误）")
    for lineno, category, msg in engine.errors:
        out.append(f"  行{lineno:<3} [{category}] {msg}")
    return "\n".join(out)


# ---------------------------------------------------------------- 内置示例

DEMO = """\
# ---------- 内置示例：覆盖全部规则 ----------
# C1 正常逐期核销 + 跨期收款（一笔 2500 覆盖第2期余款和第3期）
CONTRACT C1 6000 1000,2000,3000
PAY C1 1000 2026-01-10
PAY C1 2500 2026-02-10
# C2 逾期 45 天 > 宽限期 30 天，级联标记后续未结清期
CONTRACT C2 9000 3000,3000,3000
OVERDUE C2 45
# C3 提前终止：已收 2000，剩余 8000 重算为单一结算期 S
CONTRACT C3 10000 2000,3000,5000
PAY C3 2000 2026-01-15
TERMINATE C3
# C4 超收拦截：剩余 4000，收 4500，整笔拦截，报告超收额 500
CONTRACT C4 5000 1000,4000
PAY C4 1000 2026-01-20
PAY C4 4500 2026-02-01
# 各类拦截：终止后再收款 / 重复终止 / 引用不存在合同
PAY C3 8000 2026-03-01
TERMINATE C3
PAY C9 100 2026-01-01
OVERDUE C9 10
TERMINATE C9
"""


def main():
    ap = argparse.ArgumentParser(description="合同分期收款履约管理工具")
    ap.add_argument("input", nargs="?", help="输入文件（缺省读 stdin）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例（含分期重算）")
    args = ap.parse_args()
    if args.demo:
        print("输入指令流：")
        print(DEMO)
        engine = run(DEMO.splitlines())
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            engine = run(f)
    else:
        engine = run(sys.stdin)
    print(report(engine))


if __name__ == "__main__":
    main()
