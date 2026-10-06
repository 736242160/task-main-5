#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resource_auction.py — 资源竞价出清工具(纯 Python 标准库, 单文件)

用法:
    python3 resource_auction.py 输入文件     # 处理事件流文件
    python3 resource_auction.py             # 从标准输入读取事件流
    python3 resource_auction.py --demo      # 运行内置出清/级联重算示例(可直接验证)

事件流格式(每行一条指令, # 之后为注释, 空行忽略; 指令不区分大小写):
    RESOURCE 资源编号 数量 底价         定义资源
    BID     竞价号 资源编号 数量 出价    提交竞价
    ADJUST  竞价号 新出价              调整出价(仅对未成交余量生效)
    WITHDRAW 竞价号                    撤出竞价(取消未成交余量)
    CLEAR                             触发一轮出清

出清规则(自定, 全局一致):
  1. 优先级: 出价高者优先; 同价按提交时间(事件序号)先者优先。
     调整出价不改变原提交时间, 即时间优先级不变。
  2. 冲突裁决: 同一资源累计需求超过剩余供给时, 按优先级从高到低依次成交,
     边际竞价部分成交, 优先级更低的竞价本轮不成交; 未成交余量留在账本中,
     继续参与后续各轮出清。
  3. 统一出清价: 同一轮同一资源的所有成交使用同一价格 —
       累计需求 >= 剩余供给: 出清价 = 边际(最后一笔成交)竞价的出价;
       累计需求 <  剩余供给: 出清价 = 资源底价。
  4. 部分成交: 边际竞价按剩余供给部分成交, 余量继续竞价。
  5. 底价拦截: 出价低于底价的竞价在提交时被拦截并记入错误报告;
     调整到低于底价同样被拒绝(保留原出价)。
  6. 无效引用: 引用不存在资源的竞价被拦截并报告;
     调整/撤出不存在或已终结(全部成交/已撤出)的竞价被报告。
  7. 级联重算: 成交即结算(竞价余量与资源剩余供给同步扣减), 每次 CLEAR
     基于当前账本状态对全部资源重新出清; 输入结束时若仍有可成交的
     竞价与剩余供给, 自动追加一轮最终出清。
"""

import sys
from dataclasses import dataclass


def fmt(price):
    return f"{price:.2f}"


@dataclass
class Resource:
    rid: str
    total: int
    floor: float
    remaining: int


@dataclass
class Bid:
    bid_id: str
    rid: str
    qty: int          # 未成交余量
    price: float
    seq: int          # 提交时间(事件序号), 同价时的时间优先级依据
    active: bool = True


@dataclass
class Fill:
    bid_id: str
    rid: str
    qty: int
    price: float
    round_no: int


class Engine:
    def __init__(self):
        self.resources = {}       # rid -> Resource
        self.resource_order = []  # 资源定义顺序
        self.bids = {}            # bid_id -> Bid
        self.fills = []           # 全部成交记录
        self.errors = []          # 错误报告
        self.out = []             # 出清过程输出
        self.seq = 0              # 事件序号(时间)
        self.round_no = 0

    # ---------- 工具 ----------

    def err(self, line_no, msg):
        self.errors.append(f"第{line_no}行: {msg}")

    @staticmethod
    def parse_qty(text):
        qty = int(text)  # 可能抛 ValueError
        if qty <= 0:
            raise ValueError
        return qty

    @staticmethod
    def parse_price(text):
        price = float(text)  # 可能抛 ValueError
        if price <= 0:
            raise ValueError
        return price

    # ---------- 指令处理 ----------

    def cmd_resource(self, args, line_no):
        if len(args) != 3:
            self.err(line_no, "RESOURCE 需要 3 个参数: 资源编号 数量 底价")
            return
        rid, qty_s, floor_s = args
        if rid in self.resources:
            self.err(line_no, f"资源 {rid} 重复定义")
            return
        try:
            qty = self.parse_qty(qty_s)
        except ValueError:
            self.err(line_no, f"资源 {rid} 数量 '{qty_s}' 不是正整数")
            return
        try:
            floor = float(floor_s)
            if floor < 0:
                raise ValueError
        except ValueError:
            self.err(line_no, f"资源 {rid} 底价 '{floor_s}' 不是非负数值")
            return
        self.resources[rid] = Resource(rid, qty, floor, qty)
        self.resource_order.append(rid)

    def cmd_bid(self, args, line_no):
        if len(args) != 4:
            self.err(line_no, "BID 需要 4 个参数: 竞价号 资源编号 数量 出价")
            return
        bid_id, rid, qty_s, price_s = args
        if bid_id in self.bids:
            self.err(line_no, f"竞价号 {bid_id} 已存在")
            return
        if rid not in self.resources:
            self.err(line_no, f"竞价 {bid_id} 引用的资源 {rid} 不存在, 已拦截")
            return
        try:
            qty = self.parse_qty(qty_s)
        except ValueError:
            self.err(line_no, f"竞价 {bid_id} 数量 '{qty_s}' 不是正整数, 已拦截")
            return
        try:
            price = self.parse_price(price_s)
        except ValueError:
            self.err(line_no, f"竞价 {bid_id} 出价 '{price_s}' 不是正数值, 已拦截")
            return
        floor = self.resources[rid].floor
        if price < floor:
            self.err(line_no,
                     f"竞价 {bid_id} 出价 {fmt(price)} 低于资源 {rid} 底价 {fmt(floor)}, 已拦截")
            return
        self.seq += 1
        self.bids[bid_id] = Bid(bid_id, rid, qty, price, self.seq)

    def cmd_adjust(self, args, line_no):
        if len(args) != 2:
            self.err(line_no, "ADJUST 需要 2 个参数: 竞价号 新出价")
            return
        bid_id, price_s = args
        bid = self.bids.get(bid_id)
        if bid is None:
            self.err(line_no, f"竞价 {bid_id} 不存在, 无法调整")
            return
        if not bid.active or bid.qty <= 0:
            self.err(line_no, f"竞价 {bid_id} 已终结(全部成交或已撤出), 无法调整")
            return
        try:
            price = self.parse_price(price_s)
        except ValueError:
            self.err(line_no, f"新出价 '{price_s}' 不是正数值, 调整被拒绝")
            return
        floor = self.resources[bid.rid].floor
        if price < floor:
            self.err(line_no,
                     f"竞价 {bid_id} 新出价 {fmt(price)} 低于资源 {bid.rid} 底价 "
                     f"{fmt(floor)}, 调整被拒绝, 保留原出价 {fmt(bid.price)}")
            return
        old = bid.price
        bid.price = price  # 时间优先级(seq)不变
        self.out.append(f"  [事件] 第{line_no}行: 竞价 {bid_id} 出价 "
                        f"{fmt(old)} -> {fmt(price)} (余量{bid.qty}, 时间优先级不变)")

    def cmd_withdraw(self, args, line_no):
        if len(args) != 1:
            self.err(line_no, "WITHDRAW 需要 1 个参数: 竞价号")
            return
        bid_id = args[0]
        bid = self.bids.get(bid_id)
        if bid is None:
            self.err(line_no, f"竞价 {bid_id} 不存在, 无法撤出")
            return
        if not bid.active or bid.qty <= 0:
            self.err(line_no, f"竞价 {bid_id} 已终结(全部成交或已撤出), 无可撤出余量")
            return
        bid.active = False
        self.out.append(f"  [事件] 第{line_no}行: 竞价 {bid_id} 撤出, "
                        f"余量{bid.qty}取消, 后续出清不再参与")

    def cmd_clear(self, line_no):
        self.round_no += 1
        self.out.append(f"--- 第{self.round_no}轮出清 (第{line_no}行触发) ---" if line_no
                        else f"--- 第{self.round_no}轮出清 (输入结束自动触发) ---")
        any_activity = False
        for rid in self.resource_order:
            res = self.resources[rid]
            book = [b for b in self.bids.values()
                    if b.active and b.qty > 0 and b.rid == rid]
            if not book:
                continue
            any_activity = True
            # 优先级: 出价高者优先; 同价按提交时间先者优先
            book.sort(key=lambda b: (-b.price, b.seq))
            supply = res.remaining
            if supply <= 0:
                rest = ", ".join(f"{b.bid_id}余{b.qty}" for b in book)
                self.out.append(f"  资源 {rid}: 剩余供给 0, 无法成交 ({rest} 继续等待)")
                continue
            demand = sum(b.qty for b in book)
            # 冲突裁决: 按优先级依次成交, 边际竞价部分成交
            fills = []
            left = supply
            for b in book:
                if left <= 0:
                    break
                take = min(b.qty, left)
                fills.append((b, take))
                left -= take
            # 统一出清价
            if demand >= supply:
                price = fills[-1][0].price
                rule = f"需求{demand} >= 供给{supply}, 取边际竞价 {fills[-1][0].bid_id} 的出价"
            else:
                price = res.floor
                rule = f"需求{demand} < 供给{supply}, 取资源底价"
            self.out.append(f"  资源 {rid} (剩余供给{supply}, 需求{demand}): "
                            f"统一出清价 {fmt(price)}  [{rule}]")
            for b, take in fills:
                before = b.qty
                b.qty -= take
                res.remaining -= take
                self.fills.append(Fill(b.bid_id, rid, take, price, self.round_no))
                if b.qty == 0:
                    b.active = False
                    note = "全部成交"
                else:
                    note = f"部分成交 {take}/{before}, 余{b.qty}继续竞价"
                self.out.append(f"    成交 {b.bid_id}: {take} @{fmt(price)} ({note})")
        if not any_activity:
            self.out.append("  (无待出清竞价, 本轮无成交)")

    # ---------- 驱动 ----------

    def process_line(self, line, line_no):
        line = line.split("#", 1)[0].strip()
        if not line:
            return
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]
        handler = {
            "RESOURCE": self.cmd_resource,
            "BID": self.cmd_bid,
            "ADJUST": self.cmd_adjust,
            "WITHDRAW": self.cmd_withdraw,
        }.get(cmd)
        if cmd == "CLEAR":
            if args:
                self.err(line_no, "CLEAR 不接受参数")
            else:
                self.cmd_clear(line_no)
        elif handler is None:
            self.err(line_no, f"未知指令 '{parts[0]}'")
        else:
            handler(args, line_no)

    def run(self, text):
        for line_no, line in enumerate(text.splitlines(), 1):
            self.process_line(line, line_no)
        # 输入结束: 若仍有可成交的竞价与剩余供给, 自动追加最终出清
        clearable = any(
            res.remaining > 0 and any(
                b.active and b.qty > 0 and b.rid == rid for b in self.bids.values())
            for rid, res in self.resources.items())
        if clearable:
            self.cmd_clear(0)

    # ---------- 输出 ----------

    def render(self):
        lines = list(self.out)
        lines.append("===== 出清清单(全部成交汇总) =====")
        if not self.fills:
            lines.append("  (无成交)")
        for rid in self.resource_order:
            fills = [f for f in self.fills if f.rid == rid]
            if not fills:
                continue
            res = self.resources[rid]
            sold = res.total - res.remaining
            lines.append(f"资源 {rid}: 总量{res.total}, 累计成交{sold}, 剩余供给{res.remaining}")
            for f in fills:
                lines.append(f"    {f.bid_id} 成交{f.qty} @{fmt(f.price)} (第{f.round_no}轮)")
        lines.append("----- 未成交余量(继续竞价中) -----")
        rest = [b for b in self.bids.values() if b.active and b.qty > 0]
        if not rest:
            lines.append("  (无)")
        for b in sorted(rest, key=lambda b: b.seq):
            lines.append(f"  {b.bid_id} 资源{b.rid} 余量{b.qty} 出价{fmt(b.price)}")
        lines.append(f"===== 错误报告(共{len(self.errors)}条) =====")
        if not self.errors:
            lines.append("  (无错误)")
        else:
            lines.extend("  " + e for e in self.errors)
        return "\n".join(lines)


DEMO = """\
# ===== 资源定义: RESOURCE 编号 数量 底价 =====
RESOURCE R1 100 5.0
RESOURCE R2 60 2.0
RESOURCE R3 100 1.0
RESOURCE R4 30 1.0

# ===== R1: 同价时间优先 + 超量冲突裁决 + 部分成交 =====
BID B1 R1 60 7.0
BID B2 R1 50 7.0
BID B3 R1 30 8.0
BID B4 R1 20 4.0
# ^ B4 出价低于底价5.0, 应被拦截
BID B5 RX 10 6.0
# ^ B5 引用不存在的资源RX, 应被报告
CLEAR
# 预期: 优先序 B3(8.0) > B1(7.0,先提交) > B2(7.0,后提交)
#   B3成交30, B1成交60, B2部分成交10(余40继续); 需求140>=供给100, 统一价=边际B2出价7.0

# ===== R2: 需求不足, 按底价出清 =====
BID B6 R2 50 3.0
CLEAR
# 预期: 需求50<供给60, B6全部成交, 统一价=底价2.0

# ===== R3: 调整出价后级联重算 =====
BID B7 R3 60 4.0
CLEAR
# 预期: 需求60<供给100, B7全部成交, 统一价=底价1.0, R3剩余供给40
BID B8 R3 50 3.0
BID B9 R3 30 2.0
ADJUST B9 3.5
# ^ B9 出价反超 B8(3.0), 下一轮优先序变为 B9 > B8
CLEAR
# 预期: B9成交30, B8部分成交10(余40继续); 需求80>=供给40, 统一价=边际B8出价3.0

# ===== R4: 撤出竞价后重新出清 =====
BID B10 R4 20 5.0
BID B11 R4 20 4.0
WITHDRAW B10
# ^ B10 撤出, 不再参与; 若不撤出, 需求40>供给30, B11只能成交10
CLEAR
# 预期: 仅B11参与, 需求20<供给30, 全部成交, 统一价=底价1.0

# ===== 各类错误报告 =====
ADJUST B4 9.0
# ^ B4 已被拦截, 不存在
ADJUST B8 0.5
# ^ 低于R3底价1.0, 拒绝并保留3.0
WITHDRAW B7
# ^ B7 已全部成交, 无可撤余量
WITHDRAW B8
# ^ 撤出B8余量40
"""


def main(argv):
    args = argv[1:]
    if "--demo" in args:
        text = DEMO
        print("===== 输入事件流 =====")
        for i, line in enumerate(text.splitlines(), 1):
            print(f"{i:>3} | {line}")
        print()
    elif args:
        with open(args[0], "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    engine = Engine()
    engine.run(text)
    print("===== 运行输出 =====")
    print(engine.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
