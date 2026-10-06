#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clearing.py — 资源竞价出清工具（单文件，纯 Python 标准库）

用法:
    python3 clearing.py             # 从 stdin 读取指令流
    python3 clearing.py input.txt   # 从文件读取指令流
    python3 clearing.py --demo      # 运行内置“出清重算”示例(可直接验证)
    python3 clearing.py --selftest  # 运行内置自检(断言出清规则正确性)

指令流(每行一条, # 之后为注释, 三类流可任意交错, 状态跨流延续):
    RESOURCE <资源号> <数量> <底价>        # 资源定义流
    BID      <竞价号> <资源号> <数量> <出价>  # 竞价流
    ADJUST   <竞价号> <新出价>             # 出价调整流
    WITHDRAW <竞价号>                      # 撤出竞价
    CLEAR                                  # 基于当前状态全量重算并输出出清清单
    REPORT                                 # 输出累计错误报告

出清规则(自定, 在此固定):
  1. 优先级: 出价高者优先; 同价按提交时间(行序)先到者优先; 调价不改变时间优先权。
  2. 冲突裁决: 同一资源上竞价累计超量时, 按优先级从高到低依次成交,
     资源耗尽处边界竞价部分成交, 未成交部分继续留在竞价池参与后续出清。
  3. 统一出清价: 等于边际竞价(最后一笔有成交的竞价)的出价; 无成交则无出清价。
  4. 出价低于底价的竞价在提交时被拦截(记错误, 不生效);
     调价后低于底价同样被拦截(记错误, 保留原价)。
  5. 引用不存在资源的竞价被拦截; 引用不存在/已失效竞价的调价或撤出记错误。
  6. 每次 CLEAR 都基于全部历史指令的当前状态从零重算,
     因此调价/撤出会级联反映到最新出清结果。
"""
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


# ---------------------------------------------------------------- 数据模型

@dataclass
class Resource:
    rid: str
    qty: Decimal
    floor: Decimal


@dataclass
class Bid:
    bid_id: str
    rid: str
    qty: Decimal
    price: Decimal
    seq: int            # 提交时间序号(行序), 同价时小者优先
    active: bool = True     # False 表示提交时即被拦截(如低于底价)
    withdrawn: bool = False


def fmt(d):
    """Decimal -> 简洁字符串: 50.00 -> '50', 55.5 -> '55.5'"""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


# ---------------------------------------------------------------- 出清引擎

class Engine:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.resources = {}   # rid -> Resource (保持定义顺序)
        self.bids = {}        # bid_id -> Bid
        self.errors = []      # [(line_no, code, msg)]
        self.seq = 0
        self.clear_count = 0

    # ---- 错误报告 ----
    def error(self, line_no, code, msg):
        self.errors.append((line_no, code, msg))
        print(f"ERROR 行{line_no} [{code}] {msg}", file=self.out)

    # ---- 指令处理 ----
    def cmd_resource(self, line_no, rid, qty, floor):
        if rid in self.resources:
            return self.error(line_no, "E_DUP_RESOURCE", f"资源 {rid} 重复定义, 忽略本次定义")
        if qty <= 0:
            return self.error(line_no, "E_BAD_QTY", f"资源 {rid} 数量必须为正数")
        self.resources[rid] = Resource(rid, qty, floor)

    def cmd_bid(self, line_no, bid_id, rid, qty, price):
        if bid_id in self.bids:
            return self.error(line_no, "E_DUP_BID", f"竞价号 {bid_id} 重复, 忽略本次竞价")
        if qty <= 0:
            return self.error(line_no, "E_BAD_QTY", f"竞价 {bid_id} 数量必须为正数, 已拦截")
        self.seq += 1
        bid = Bid(bid_id, rid, qty, price, self.seq)
        self.bids[bid_id] = bid
        res = self.resources.get(rid)
        if res is None:
            bid.active = False
            return self.error(line_no, "E_UNKNOWN_RESOURCE",
                              f"竞价 {bid_id} 引用了不存在的资源 {rid}, 已拦截")
        if price < res.floor:
            bid.active = False
            return self.error(line_no, "E_BELOW_FLOOR",
                              f"竞价 {bid_id} 出价 {fmt(price)} 低于资源 {rid} 底价 "
                              f"{fmt(res.floor)}, 已拦截")

    def cmd_adjust(self, line_no, bid_id, new_price):
        bid = self.bids.get(bid_id)
        if bid is None:
            return self.error(line_no, "E_UNKNOWN_BID", f"调价引用了不存在的竞价 {bid_id}")
        if not bid.active:
            return self.error(line_no, "E_INACTIVE_BID",
                              f"竞价 {bid_id} 提交时已被拦截, 不可调价")
        if bid.withdrawn:
            return self.error(line_no, "E_WITHDRAWN_BID",
                              f"竞价 {bid_id} 已撤出, 不可调价")
        floor = self.resources[bid.rid].floor
        if new_price < floor:
            return self.error(line_no, "E_BELOW_FLOOR",
                              f"竞价 {bid_id} 调价至 {fmt(new_price)} 低于底价 "
                              f"{fmt(floor)}, 已拦截, 保留原价 {fmt(bid.price)}")
        bid.price = new_price   # 保留原 seq: 调价不改变时间优先权

    def cmd_withdraw(self, line_no, bid_id):
        bid = self.bids.get(bid_id)
        if bid is None:
            return self.error(line_no, "E_UNKNOWN_BID", f"撤出引用了不存在的竞价 {bid_id}")
        if not bid.active:
            return self.error(line_no, "E_INACTIVE_BID",
                              f"竞价 {bid_id} 提交时已被拦截, 无需撤出")
        if bid.withdrawn:
            return self.error(line_no, "E_DUP_WITHDRAW", f"竞价 {bid_id} 已撤出, 重复操作")
        bid.withdrawn = True

    # ---- 出清核心: 每次从零全量重算, 天然支持级联 ----
    def clear_resource(self, res):
        """返回 (fills, clearing_price, unsold)。
        fills: [(bid, 成交量)], 按优先级排序; 最后一笔可能部分成交。"""
        pool = [b for b in self.bids.values()
                if b.rid == res.rid and b.active and not b.withdrawn]
        pool.sort(key=lambda b: (-b.price, b.seq))   # 价高优先, 同价时间优先
        remaining = res.qty
        fills = []
        for b in pool:
            if remaining <= 0:
                break
            take = min(b.qty, remaining)
            fills.append((b, take))
            remaining -= take
        price = fills[-1][0].price if fills else None   # 统一价 = 边际竞价出价
        return fills, price, remaining

    def cmd_clear(self, line_no):
        self.clear_count += 1
        print(f"\n== CLEAR #{self.clear_count} (行{line_no}) ==", file=self.out)
        for res in self.resources.values():
            fills, price, unsold = self.clear_resource(res)
            print(f"[{res.rid}] 数量={fmt(res.qty)} 底价={fmt(res.floor)}", file=self.out)
            filled_ids = set()
            for i, (b, take) in enumerate(fills):
                filled_ids.add(b.bid_id)
                tag = "FILL" if take == b.qty else "PART"
                extra = "" if take == b.qty else f" 剩余={fmt(b.qty - take)}(继续参与竞价)"
                print(f"  {tag} {b.bid_id} 成交={fmt(take)} 出价={fmt(b.price)}{extra}",
                      file=self.out)
            for b in sorted(self.bids.values(), key=lambda x: x.seq):
                if (b.rid == res.rid and b.active and not b.withdrawn
                        and b.bid_id not in filled_ids):
                    print(f"  MISS {b.bid_id} 出价={fmt(b.price)} (优先级不足, 未成交)",
                          file=self.out)
            if price is None:
                print("  无成交, 无出清价", file=self.out)
            else:
                marginal = fills[-1][0].bid_id
                print(f"  出清价={fmt(price)} (边际竞价 {marginal})  "
                      f"已出清={fmt(res.qty - unsold)} 未售出={fmt(unsold)}",
                      file=self.out)

    def cmd_report(self):
        print(f"\n== 错误报告 (共 {len(self.errors)} 条) ==", file=self.out)
        for line_no, code, msg in self.errors:
            print(f"  行{line_no} [{code}] {msg}", file=self.out)
        if not self.errors:
            print("  无错误", file=self.out)

    # ---- 指令流解析 ----
    def process(self, lines):
        for line_no, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0].upper(), parts[1:]
            try:
                if cmd == "RESOURCE" and len(args) == 3:
                    self.cmd_resource(line_no, args[0],
                                      Decimal(args[1]), Decimal(args[2]))
                elif cmd == "BID" and len(args) == 4:
                    self.cmd_bid(line_no, args[0], args[1],
                                 Decimal(args[2]), Decimal(args[3]))
                elif cmd == "ADJUST" and len(args) == 2:
                    self.cmd_adjust(line_no, args[0], Decimal(args[1]))
                elif cmd == "WITHDRAW" and len(args) == 1:
                    self.cmd_withdraw(line_no, args[0])
                elif cmd == "CLEAR" and not args:
                    self.cmd_clear(line_no)
                elif cmd == "REPORT" and not args:
                    self.cmd_report()
                else:
                    self.error(line_no, "E_PARSE", f"无法解析的指令: {line}")
            except InvalidOperation:
                self.error(line_no, "E_PARSE", f"数值格式错误: {line}")
        self.cmd_report()   # 流结束时总是输出累计错误报告


# ---------------------------------------------------------------- 内置示例

DEMO = r"""
# ---- 资源定义流 ----
RESOURCE R1 100 50
RESOURCE R2 60 10

# ---- 竞价流 ----
BID B1 R1 40 60
BID B2 R1 40 60        # 与 B1 同价, 时间靠后 -> B1 优先
BID B3 R1 50 55
BID B4 R1 30 70
BID B5 R9 10 99        # 错误: 资源 R9 不存在
BID B6 R1 10 40        # 错误: 低于 R1 底价 50
BID B7 R2 80 12        # 超量 -> 部分成交 60, 剩余 20 继续竞价
CLEAR

# ---- 出价调整流(级联重算) ----
ADJUST B3 75           # B3 提价至 75 -> 跃居首位, 出清结果级联改变
ADJUST B5 100          # 错误: B5 已被拦截
ADJUST B4 8            # 错误: 低于底价, 保留原价 70
CLEAR

# ---- 撤出后重新出清 ----
WITHDRAW B4            # 撤出高价竞价 -> 重新出清, 出清价回落
WITHDRAW B4            # 错误: 重复撤出
CLEAR
"""

# ---------------------------------------------------------------- 自检

def selftest():
    import io, contextlib

    def run(text):
        eng = Engine(out=io.StringIO())
        with contextlib.redirect_stdout(eng.out):
            eng.process(text.strip().splitlines())
        return eng

    def fills_of(eng, rid):
        fills, price, _ = eng.clear_resource(eng.resources[rid])
        return {b.bid_id: take for b, take in fills}, price

    D = Decimal
    # 1) 价高优先 + 同价时间优先 + 部分成交 + 统一出清价=边际出价
    eng = run("""
        RESOURCE R 100 1
        BID A R 60 10
        BID B R 60 10
        BID C R 50 9
    """)
    f, p = fills_of(eng, "R")
    assert f == {"A": D(60), "B": D(40)} and p == D(10), (f, p)

    # 2) 部分成交剩余量继续竞价: 撤出高价竞价后剩余量补位成交
    eng = run("""
        RESOURCE R 100 1
        BID A R 60 10
        BID B R 60 9
        WITHDRAW A
    """)
    f, p = fills_of(eng, "R")
    assert f == {"B": D(60)} and p == D(9), (f, p)

    # 3) 调价级联重算: 低价竞价提价后跃居首位, 出清价随之改变
    eng = run("""
        RESOURCE R 100 1
        BID A R 50 5
        BID B R 50 6
        BID C R 50 7
        ADJUST A 8
    """)
    f, p = fills_of(eng, "R")
    assert f == {"A": D(50), "C": D(50)} and p == D(7), (f, p)

    # 4) 低于底价拦截 / 不存在资源报告 / 调价低于底价保留原价
    eng = run("""
        RESOURCE R 100 50
        BID A R 10 49
        BID B R 10 60
        BID C RX 10 99
        ADJUST B 10
    """)
    codes = [c for _, c, _ in eng.errors]
    assert codes == ["E_BELOW_FLOOR", "E_UNKNOWN_RESOURCE", "E_BELOW_FLOOR"], codes
    assert not eng.bids["A"].active and eng.bids["B"].price == D(60)
    f, p = fills_of(eng, "R")
    assert f == {"B": D(10)} and p == D(60), (f, p)

    # 5) 同价时间优先不受调价影响(调价保留原时间序号)
    eng = run("""
        RESOURCE R 10 1
        BID A R 10 5
        BID B R 10 6
        ADJUST B 5
    """)
    f, p = fills_of(eng, "R")
    assert f == {"A": D(10)} and p == D(5), (f, p)

    print("selftest: 全部 5 组断言通过")


# ---------------------------------------------------------------- 入口

def main(argv):
    args = argv[1:]
    if args and args[0] == "--selftest":
        selftest()
        return 0
    if args and args[0] == "--demo":
        print("---- 示例指令流 ----")
        print(DEMO.strip())
        print("\n---- 运行结果 ----")
        Engine().process(DEMO.strip().splitlines())
        return 0
    if len(args) == 1:
        with open(args[0], encoding="utf-8") as fh:
            Engine().process(fh.read().splitlines())
        return 0
    if not args:
        Engine().process(sys.stdin.read().splitlines())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
