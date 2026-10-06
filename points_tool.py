#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会员积分管理工具（纯 Python 标准库，单文件）。

功能
  - 积分获得 / 消耗 / 冲正，消耗超余额自动拦截并报告
  - 按阈值表自动升降级；跨档（跳级）时逐档级联权益变更
  - 冲正撤销对应积分效果，导致等级回降时级联重算
  - 积分按有效期（天数，可配置）自动过期扣减并报告
  - 引用不存在会员、重复冲正、分值非法等集中错误报告
  - 多流（streams）顺序处理，状态跨流延续
  - 输出积分余额与等级清单、变动日志、错误报告

输入 JSON 结构
  {
    "validity_days": 365,                       # 积分有效期（天）
    "levels":  [{"name": "白银", "threshold": 0}, ...],   # 阈值严格递增
    "benefits": {"黄金": ["95折", "生日礼"], ...},        # 每级权益（自定）
    "members": [{"name": "张三", "level": "白银"}, ...],
    "streams": [                                 # 流列表，状态跨流延续
      [{"id": "e1", "date": "2026-01-01", "member": "张三",
        "type": "获得|消耗|冲正", "points": 100, "ref": "e0"}, ...],
      [...]
    ]
  }
  说明：获得/消耗需 points（正整数）；冲正需 ref（原事件 id）。
       等级以当前余额对照阈值表为准，余额变动即重算。

用法
  python3 points_tool.py input.json   # 处理输入文件
  python3 points_tool.py --demo       # 运行内置等级升降样例
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

EARN, CONSUME, REVERSE = "获得", "消耗", "冲正"
VALID_TYPES = (EARN, CONSUME, REVERSE)


@dataclass
class Lot:
    """一笔未过期积分（按来源事件与有效期跟踪，先进先出消耗）。"""
    points: int
    expire_on: date
    source_id: str


@dataclass
class Member:
    name: str
    level: str
    balance: int = 0
    lots: list = field(default_factory=list)


class PointsEngine:
    def __init__(self, config):
        self.validity_days = int(config.get("validity_days", 365))
        self.levels = list(config.get("levels") or [])
        if not self.levels:
            raise ValueError("等级阈值表 levels 不能为空")
        thresholds = [lv["threshold"] for lv in self.levels]
        if len(set(thresholds)) != len(thresholds) or thresholds != sorted(thresholds):
            raise ValueError("等级阈值必须严格递增")
        self.benefits = dict(config.get("benefits") or {})
        self.level_index = {lv["name"]: i for i, lv in enumerate(self.levels)}
        self.members = {}
        self.events = {}          # 已成功处理的事件 id -> 事件快照
        self.reversed_ids = set()  # 已被冲正的原事件 id
        self.errors = []
        self.logs = []
        for mdef in config.get("members") or []:
            name = mdef.get("name")
            level = mdef.get("level", self.levels[0]["name"])
            if not name:
                raise ValueError("会员定义缺少 name")
            if name in self.members:
                raise ValueError(f"会员 {name} 重复定义")
            if level not in self.level_index:
                raise ValueError(f"会员 {name} 的初始等级 {level} 不在阈值表中")
            self.members[name] = Member(name=name, level=level)

    # ---------------- 等级 ----------------
    def level_for(self, balance):
        chosen = self.levels[0]["name"]
        for lv in self.levels:
            if balance >= lv["threshold"]:
                chosen = lv["name"]
        return chosen

    def apply_level_change(self, m, where, reason):
        """余额变动后按阈值表重算等级；跨档时逐档级联权益变更。"""
        target = self.level_for(m.balance)
        if target == m.level:
            return
        old_idx = self.level_index[m.level]
        new_idx = self.level_index[target]
        step = 1 if new_idx > old_idx else -1
        span = abs(new_idx - old_idx)
        self.logs.append(
            f"{where} {m.name} {'升级' if step > 0 else '降级'}："
            f"{m.level} -> {target}（跨{span}档，原因：{reason}，余额 {m.balance}）"
        )
        # 级联：升级获得途经各档（含目标档）权益；降级失去高于目标档的各档权益
        rng = range(old_idx + 1, new_idx + 1) if step > 0 else range(old_idx, new_idx, -1)
        for idx in rng:
            lv_name = self.levels[idx]["name"]
            perks = self.benefits.get(lv_name, [])
            verb = "获得权益" if step > 0 else "失去权益"
            self.logs.append(
                f"{where}   └ 级联[{lv_name}] {verb}："
                f"{'、'.join(perks) if perks else '（无配置权益）'}"
            )
        m.level = target

    # ---------------- 积分 ----------------
    def deduct(self, m, amount, prefer_source=None):
        """按有效期先进先出扣减；冲正获得时优先扣原事件那一笔。调用前须保证余额足够。"""
        m.lots.sort(key=lambda lot: (lot.source_id != prefer_source, lot.expire_on))
        remaining = amount
        kept = []
        for lot in m.lots:
            if remaining and lot.points:
                take = min(lot.points, remaining)
                lot.points -= take
                remaining -= take
            if lot.points:
                kept.append(lot)
        m.lots = kept
        m.balance -= amount

    def expire_before(self, current, where):
        """时间推进时，所有会员过期积分自动扣减并报告，等级随降级联重算。"""
        for m in self.members.values():
            kept = []
            for lot in m.lots:
                if lot.expire_on < current:
                    m.balance -= lot.points
                    self.logs.append(
                        f"{where} {m.name} 积分过期扣减 {lot.points}"
                        f"（来源 {lot.source_id}，有效期至 {lot.expire_on.isoformat()}，"
                        f"余额 {m.balance}）"
                    )
                else:
                    kept.append(lot)
            m.lots = kept
            self.apply_level_change(m, where, "积分过期")

    # ---------------- 事件处理 ----------------
    def process(self, si, ei, raw):
        ev_id = str(raw.get("id") or f"s{si}#{ei}")
        where = f"[流{si}][{ev_id}]"
        try:
            ev_date = date.fromisoformat(str(raw["date"]))
        except (KeyError, ValueError, TypeError):
            self.errors.append(f"{where} 日期缺失或格式错误（应为 YYYY-MM-DD）")
            return
        self.expire_before(ev_date, where)

        m = self.members.get(raw.get("member"))
        if m is None:
            self.errors.append(f"{where} 引用不存在的会员：{raw.get('member')!r}")
            return
        etype = raw.get("type")
        if etype not in VALID_TYPES:
            self.errors.append(f"{where} 未知积分类型：{etype!r}")
            return

        if etype in (EARN, CONSUME):
            points = raw.get("points")
            if not isinstance(points, int) or isinstance(points, bool) or points <= 0:
                self.errors.append(f"{where} 分值必须为正整数，实际：{points!r}")
                return
            if ev_id in self.events:
                self.errors.append(f"{where} 事件 id 重复：{ev_id}")
                return
            if etype == EARN:
                lot = Lot(points, ev_date + timedelta(days=self.validity_days), ev_id)
                m.lots.append(lot)
                m.balance += points
                self.logs.append(
                    f"{where} {m.name} 获得 {points} 分"
                    f"（有效期至 {lot.expire_on.isoformat()}，余额 {m.balance}）"
                )
            else:
                if points > m.balance:  # 消耗超余额：拦截
                    self.errors.append(
                        f"{where} {m.name} 消耗 {points} 超余额 {m.balance}，已拦截"
                    )
                    return
                self.deduct(m, points)
                self.logs.append(f"{where} {m.name} 消耗 {points} 分（余额 {m.balance}）")
            self.events[ev_id] = {"type": etype, "points": points,
                                  "member": m.name, "date": ev_date}
            self.apply_level_change(m, where, etype)
            return

        # 冲正
        ref = raw.get("ref")
        orig = self.events.get(ref)
        if orig is None:
            self.errors.append(f"{where} 冲正失败：原事件 {ref!r} 不存在或已被拦截")
            return
        if ref in self.reversed_ids:
            self.errors.append(f"{where} 冲正失败：原事件 {ref!r} 已被冲正")
            return
        if orig["member"] != m.name:
            self.errors.append(
                f"{where} 冲正失败：原事件 {ref!r} 属于会员 {orig['member']!r}"
            )
            return
        if orig["type"] == EARN:  # 撤销获得：扣回积分，可能触发降级级联
            if orig["points"] > m.balance:
                self.errors.append(
                    f"{where} 冲正失败：撤销获得 {orig['points']} 分将致余额为负"
                    f"（当前 {m.balance}），已拦截"
                )
                return
            self.deduct(m, orig["points"], prefer_source=ref)
            self.logs.append(
                f"{where} {m.name} 冲正获得，扣回 {orig['points']} 分"
                f"（原事件 {ref}，余额 {m.balance}）"
            )
        else:  # 撤销消耗：返还积分，自冲正日起重新计有效期
            lot = Lot(orig["points"], ev_date + timedelta(days=self.validity_days), ev_id)
            m.lots.append(lot)
            m.balance += orig["points"]
            self.logs.append(
                f"{where} {m.name} 冲正消耗，返还 {orig['points']} 分"
                f"（原事件 {ref}，余额 {m.balance}）"
            )
        self.reversed_ids.add(ref)
        self.apply_level_change(m, where, f"冲正{orig['type']}")

    def run(self, streams):
        for si, stream in enumerate(streams, 1):
            if isinstance(stream, dict):  # 容忍单事件流
                stream = [stream]
            for ei, raw in enumerate(stream, 1):
                if not isinstance(raw, dict):
                    self.errors.append(f"[流{si}][#{ei}] 事件格式错误，已跳过")
                    continue
                self.process(si, ei, raw)

    # ---------------- 输出 ----------------
    def report(self):
        lines = ["===== 积分余额与等级清单 ====="]
        for name in sorted(self.members):
            m = self.members[name]
            perks = "、".join(self.benefits.get(m.level, [])) or "（无）"
            lines.append(f"{name}：余额 {m.balance} 分，等级 {m.level}，当前权益：{perks}")
            for lot in sorted(m.lots, key=lambda l: l.expire_on):
                lines.append(
                    f"    未过期积分 {lot.points}（来源 {lot.source_id}，"
                    f"有效期至 {lot.expire_on.isoformat()}）"
                )
        lines += ["", "===== 变动与过期日志 ====="]
        lines += self.logs or ["（无）"]
        lines += ["", "===== 错误报告 ====="]
        lines += self.errors or ["（无错误）"]
        return "\n".join(lines)


# ---------------- 内置样例：等级升降 / 跨档级联 / 冲正 / 过期 ----------------
DEMO = {
    "validity_days": 365,
    "levels": [
        {"name": "白银", "threshold": 0},
        {"name": "黄金", "threshold": 100},
        {"name": "铂金", "threshold": 300},
        {"name": "钻石", "threshold": 600},
    ],
    "benefits": {
        "白银": ["积分1倍"],
        "黄金": ["积分1.2倍", "生日礼"],
        "铂金": ["积分1.5倍", "免费停车", "专属客服"],
        "钻石": ["积分2倍", "机场贵宾厅", "年度礼盒"],
    },
    "members": [
        {"name": "张三", "level": "白银"},
        {"name": "李四", "level": "白银"},
    ],
    "streams": [
        [  # 流1：2026-01
            {"id": "e1", "date": "2026-01-01", "member": "张三", "type": "获得", "points": 350},
            {"id": "e2", "date": "2026-01-05", "member": "张三", "type": "获得", "points": 300},
            {"id": "e3", "date": "2026-01-10", "member": "张三", "type": "消耗", "points": 50},
            {"id": "e4", "date": "2026-01-12", "member": "张三", "type": "消耗", "points": 9999},
            {"id": "e5", "date": "2026-01-15", "member": "王五", "type": "获得", "points": 10},
            {"id": "e6", "date": "2026-01-20", "member": "李四", "type": "获得", "points": 120},
        ],
        [  # 流2：2026-02（状态自流1延续）
            {"id": "e7", "date": "2026-02-25", "member": "张三", "type": "冲正", "ref": "e1"},
            {"id": "e8", "date": "2026-02-26", "member": "张三", "type": "冲正", "ref": "e3"},
            {"id": "e9", "date": "2026-02-27", "member": "李四", "type": "消耗", "points": 200},
        ],
        [  # 流3：2027-02（李四 e6 的 120 分已于 2027-01-20 过期）
            {"id": "e10", "date": "2027-02-01", "member": "李四", "type": "获得", "points": 5},
        ],
    ],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="会员积分管理工具（纯标准库）")
    parser.add_argument("input", nargs="?", help="输入 JSON 文件路径")
    parser.add_argument("--demo", action="store_true", help="运行内置等级升降样例")
    args = parser.parse_args(argv)

    if args.demo:
        config = DEMO
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            config = json.load(f)
    else:
        parser.error("请提供输入 JSON 文件，或使用 --demo 运行内置样例")

    streams = config.pop("streams", [])
    if streams and isinstance(streams[0], dict):  # 容忍扁平事件列表
        streams = [streams]
    try:
        engine = PointsEngine(config)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    engine.run(streams)
    print(engine.report())
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
