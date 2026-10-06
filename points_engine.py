#!/usr/bin/env python3
"""会员积分引擎（纯标准库，单文件）。

功能：积分累计/消耗/冲正、等级按阈值浮动（含跨档级联）、积分过期自动扣减、
错误拦截与报告、跨流状态延续、输出余额与等级清单。

用法：
    python3 points_engine.py                # 运行内置演示（含等级升降样例）
    python3 points_engine.py s1.txt s2.txt  # 顺序处理多个流，状态跨流延续

输入流格式（# 后为注释）：
    MEMBER <名称> <初始等级>
    YYYY-MM-DD EARN <会员> <分值>
    YYYY-MM-DD CONSUME <会员> <分值>
    YYYY-MM-DD REVERSE <会员> <原交易号Tx>

规则说明：
    - 等级由当前余额按 LEVELS 阈值表确定，余额变动后立即重算。
    - 跨档升/降时，对途经的每一档级联发放/收回权益（BENEFITS）。
    - 每笔获得积分有效期 VALIDITY_DAYS 天，到期后随下一事件自动扣减并报告。
    - 消耗按“先到期先扣”(FIFO) 扣减；余额不足一律拦截并报告。
    - 冲正获得 = 全额扣回原分值（余额不足则拦截）；冲正消耗 = 全额退回
      （作为新积分批次，有效期自冲正日起算）。冲正引起的等级回降会级联重算。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------- 可调规则 ----------------
LEVELS = [("青铜", 0), ("白银", 500), ("黄金", 2000), ("铂金", 5000)]  # 阈值表（升序）
VALIDITY_DAYS = 180  # 积分有效期（天）
BENEFITS = {  # 各等级权益（级联变化的依据）
    "青铜": ["生日券"],
    "白银": ["生日券", "98折"],
    "黄金": ["生日券", "95折", "免费升舱"],
    "铂金": ["生日券", "9折", "免费升舱", "专属客服"],
}
# ------------------------------------------


@dataclass
class Lot:
    """积分批次：按到期日 FIFO 扣减。"""

    txid: str
    remaining: int
    expires_on: date


@dataclass
class Tx:
    txid: str
    kind: str  # EARN / CONSUME / REVERSAL
    member: str
    amount: int
    day: date
    ref: str = ""  # 冲正指向的原交易号
    reversed: bool = False


@dataclass
class Member:
    name: str
    level: str
    balance: int = 0
    lots: list = field(default_factory=list)


class Engine:
    def __init__(self):
        self.members: dict[str, Member] = {}
        self.txs: dict[str, Tx] = {}
        self.seq = 0
        self.current_date: date | None = None
        self.events: list[str] = []   # 事件日志（含等级变化与级联）
        self.expired: list[str] = []  # 过期报告
        self.errors: list[str] = []   # 错误报告

    # ---- 等级 ----
    @staticmethod
    def level_of(balance: int) -> str:
        name = LEVELS[0][0]
        for lname, threshold in LEVELS:
            if balance >= threshold:
                name = lname
        return name

    def _relevel(self, m: Member, day: date, why: str):
        new = self.level_of(m.balance)
        if new == m.level:
            return
        order = [n for n, _ in LEVELS]
        old_i, new_i = order.index(m.level), order.index(new)
        if new_i > old_i:  # 升级：依次进入新档位，发放各档权益
            rng, direction, action = range(old_i + 1, new_i + 1), "升级", "获得权益"
        else:  # 降级：依次退出高档位，收回各档权益
            rng, direction, action = range(old_i, new_i, -1), "降级", "收回权益"
        for i in rng:
            for b in BENEFITS.get(order[i], []):
                self.events.append(f"{day} {m.name} 级联{direction}[{order[i]}] {action}:{b}")
        self.events.append(
            f"{day} {m.name} {direction}: {m.level} -> {new}（{why}，余额={m.balance}）"
        )
        m.level = new

    # ---- 过期 ----
    def _expire(self, day: date):
        for m in self.members.values():
            changed = False
            for lot in m.lots:
                if lot.remaining > 0 and lot.expires_on < day:
                    deducted = lot.remaining
                    m.balance -= deducted
                    self.expired.append(
                        f"{day} {m.name} 积分过期扣减 {deducted}"
                        f"（来源{lot.txid}，到期日{lot.expires_on}），余额={m.balance}"
                    )
                    lot.remaining = 0
                    changed = True
            m.lots = [l for l in m.lots if l.remaining > 0]
            if changed:
                self._relevel(m, day, "积分过期")

    def _advance(self, day: date):
        if self.current_date is None or day > self.current_date:
            self._expire(day)
            self.current_date = day

    # ---- 交易 ----
    def _new_tx(self, kind: str, name: str, amount: int, day: date, ref: str = "") -> Tx:
        self.seq += 1
        tx = Tx(f"T{self.seq}", kind, name, amount, day, ref=ref)
        self.txs[tx.txid] = tx
        return tx

    @staticmethod
    def _deduct(m: Member, amount: int):
        """先到期先扣（FIFO）。"""
        need = amount
        for lot in sorted(m.lots, key=lambda l: l.expires_on):
            if need <= 0:
                break
            take = min(lot.remaining, need)
            lot.remaining -= take
            need -= take
        m.lots = [l for l in m.lots if l.remaining > 0]
        m.balance -= amount

    def earn(self, name: str, amount: int, day: date):
        m = self.members.get(name)
        if m is None:
            self.errors.append(f"{day} EARN {name} {amount} -> 错误：会员不存在")
            return
        if amount <= 0:
            self.errors.append(f"{day} EARN {name} {amount} -> 错误：分值必须为正数")
            return
        tx = self._new_tx("EARN", name, amount, day)
        expires = day + timedelta(days=VALIDITY_DAYS)
        m.lots.append(Lot(tx.txid, amount, expires))
        m.balance += amount
        self.events.append(f"{day} {name} 获得 {amount}（{tx.txid}，{expires}到期），余额={m.balance}")
        self._relevel(m, day, f"获得{amount}分")

    def consume(self, name: str, amount: int, day: date):
        m = self.members.get(name)
        if m is None:
            self.errors.append(f"{day} CONSUME {name} {amount} -> 错误：会员不存在")
            return
        if amount <= 0:
            self.errors.append(f"{day} CONSUME {name} {amount} -> 错误：分值必须为正数")
            return
        if m.balance < amount:
            self.errors.append(
                f"{day} CONSUME {name} {amount} -> 错误：余额不足（当前{m.balance}），已拦截"
            )
            return
        tx = self._new_tx("CONSUME", name, amount, day)
        self._deduct(m, amount)
        self.events.append(f"{day} {name} 消耗 {amount}（{tx.txid}），余额={m.balance}")
        self._relevel(m, day, f"消耗{amount}分")

    def reverse(self, name: str, txid: str, day: date):
        m = self.members.get(name)
        if m is None:
            self.errors.append(f"{day} REVERSE {name} {txid} -> 错误：会员不存在")
            return
        tx = self.txs.get(txid)
        if tx is None or tx.member != name:
            self.errors.append(f"{day} REVERSE {name} {txid} -> 错误：交易不存在或不属于该会员")
            return
        if tx.reversed:
            self.errors.append(f"{day} REVERSE {name} {txid} -> 错误：该交易已被冲正，禁止重复冲正")
            return
        if tx.kind == "EARN":
            if m.balance < tx.amount:
                self.errors.append(
                    f"{day} REVERSE {name} {txid} -> 错误：余额不足以冲正获得"
                    f"（需扣{tx.amount}，当前{m.balance}），已拦截"
                )
                return
            self._deduct(m, tx.amount)
            note = f"冲正获得{txid}，扣回{tx.amount}"
        elif tx.kind == "CONSUME":
            rtx = self._new_tx("REVERSAL", name, tx.amount, day, ref=txid)
            m.lots.append(Lot(rtx.txid, tx.amount, day + timedelta(days=VALIDITY_DAYS)))
            m.balance += tx.amount
            note = f"冲正消耗{txid}，退回{tx.amount}（新批次{rtx.txid}）"
        else:
            self.errors.append(f"{day} REVERSE {name} {txid} -> 错误：冲正交易不可再冲正")
            return
        tx.reversed = True
        self._new_tx("REVERSAL", name, tx.amount, day, ref=txid)
        self.events.append(f"{day} {name} {note}，余额={m.balance}")
        self._relevel(m, day, f"冲正{txid}")

    # ---- 流处理（可多次调用，状态延续） ----
    def process_stream(self, lines, source: str = "<stream>"):
        level_names = {n for n, _ in LEVELS}
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            try:
                if parts[0].upper() == "MEMBER":
                    _, name, level = parts
                    if name in self.members:
                        self.errors.append(f"{source}:{lineno} 错误：会员重复定义 {name}")
                    elif level not in level_names:
                        self.errors.append(f"{source}:{lineno} 错误：未知等级 {level}")
                    else:
                        self.members[name] = Member(name, level)
                        self.events.append(f"定义会员 {name}，初始等级 {level}")
                    continue
                day = date.fromisoformat(parts[0])
                if self.current_date and day < self.current_date:
                    self.errors.append(f"{source}:{lineno} 错误：日期回退 {day}，已跳过")
                    continue
                self._advance(day)
                cmd = parts[1].upper()
                if cmd == "EARN":
                    self.earn(parts[2], int(parts[3]), day)
                elif cmd == "CONSUME":
                    self.consume(parts[2], int(parts[3]), day)
                elif cmd == "REVERSE":
                    self.reverse(parts[2], parts[3], day)
                else:
                    self.errors.append(f"{source}:{lineno} 错误：未知指令 {cmd}")
            except (ValueError, IndexError):
                self.errors.append(f"{source}:{lineno} 错误：无法解析行：{raw.strip()}")

    # ---- 报告 ----
    def report(self) -> str:
        out = ["===== 事件日志 ====="]
        out += self.events or ["（无）"]
        out += ["", "===== 积分过期报告 ====="]
        out += self.expired or ["（无）"]
        out += ["", "===== 错误报告 ====="]
        out += self.errors or ["（无）"]
        out += ["", "===== 会员清单（余额与等级） ====="]
        for m in self.members.values():
            out.append(f"{m.name}\t等级={m.level}\t余额={m.balance}")
        return "\n".join(out)


DEMO_STREAM = """\
# 等级升降样例（阈值：青铜0/白银500/黄金2000/铂金5000，有效期180天）
MEMBER 张三 青铜
MEMBER 李四 白银
2026-01-01 EARN 张三 5500      # T1 青铜->铂金：连跳白银/黄金两级，权益级联发放
2026-01-05 CONSUME 张三 2000   # T2 余额3500：铂金->黄金，级联收回铂金权益
2026-01-06 REVERSE 张三 T2     # 冲正消耗：退回2000，黄金->铂金
2026-01-07 REVERSE 张三 T1     # 冲正获得：扣回5500，铂金->青铜，级联收回全部权益
2026-01-08 REVERSE 张三 T1     # 错误：重复冲正
2026-01-10 EARN 李四 300       # 初始白银但余额仅300：按余额浮动回降青铜
2026-02-01 CONSUME 张三 100    # 错误：余额不足，拦截
2026-02-02 EARN 王五 100       # 错误：会员不存在
2026-08-01 EARN 李四 100       # 先自动过期扣减此前的300分，再入账100
"""


def main(argv):
    engine = Engine()
    if argv:
        for path in argv:  # 多流顺序处理，状态跨流延续
            with open(path, encoding="utf-8") as f:
                engine.process_stream(f, source=path)
    else:
        engine.process_stream(DEMO_STREAM.splitlines(), source="<demo>")
    print(engine.report())


if __name__ == "__main__":
    main(sys.argv[1:])
