#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_engine.py — 规则版本化执行引擎（纯 Python 标准库，单文件）

输入为命令流（行文本，空白分隔，# 之后为注释）：

    RULE    <规则编号> <参数名>                            定义规则
    VERSION <规则编号> <版本号> <生效日期YYYY-MM-DD> <参数值:浮点>
    TX      <交易号> <交易日期YYYY-MM-DD> <规则编号> <参数输入:浮点>
    SETTLE  <交易号> <结算日期YYYY-MM-DD>

核心语义
--------
1. 版本命中：同一规则的版本按生效日期升序构成半开区间
   [生效日_i, 生效日_{i+1})，切换当天归属新版本（生效日含当天，含边界）。
2. 规则变更（VERSION 到达）时，该规则所有未结算(OPEN)交易立即按新版本
   参数重算；已结算(SETTLED)交易冻结，永久保留旧版本结果。
3. 规则计算：结果 = 参数输入 * 参数值（占位规则，可替换 calculate()）。
4. 状态保存在 Engine 实例中，可连续处理多个流（跨流状态延续）。
5. 所有异常进入错误清单，携带 来源:行号 与规则编号定位，不中断处理。

校验
----
- 同版本重复生效（同规则同版本号）         -> E_DUP_VERSION  拦截
- 同规则同生效日不同版本（日期重叠）       -> E_OVERLAP      报告并拦截
- 生效日期倒序（新版本号生效日早于旧版本） -> E_VERSION_ORDER 报告（仍登记，查找按日期）
- 交易日期早于首个版本生效日（空档）       -> E_NO_VERSION   报告
- 交易引用不存在的规则                     -> E_UNKNOWN_RULE 报告

用法
----
    python3 rule_engine.py 流文件1 [流文件2 ...]   # 多文件按序处理，状态延续
    python3 rule_engine.py < 流文件                # 标准输入
    python3 rule_engine.py --demo                  # 内置演示
退出码：有错误为 1，否则为 0。
"""

from __future__ import annotations

import bisect
import sys
from dataclasses import dataclass, field
from datetime import date


# ---------------------------------------------------------------- 数据结构

@dataclass
class RuleDef:
    rule_id: str
    param_name: str
    line: int


@dataclass
class RuleVersion:
    rule_id: str
    version: str
    effective: date
    param: float
    line: int


@dataclass
class CalcRecord:
    event: str      # initial | recalc | freeze
    version: str
    param: float
    result: float
    note: str
    line: int


@dataclass
class Transaction:
    tx_id: str
    tx_date: date
    rule_id: str
    input_value: float
    line: int
    status: str = "OPEN"            # OPEN -> SETTLED
    history: list = field(default_factory=list)  # list[CalcRecord]

    @property
    def current(self):
        return self.history[-1] if self.history else None


@dataclass
class Error:
    source: str
    line: int
    code: str
    message: str


def _ver_key(version: str):
    """版本号排序键：纯数字按数值，否则按字符串。"""
    try:
        return (0, int(version), "")
    except ValueError:
        return (1, 0, version)


def _num(x: float) -> str:
    return "%g" % x


# ---------------------------------------------------------------- 引擎

class Engine:
    """规则引擎：保存全部跨流状态。"""

    def __init__(self):
        self.rules = {}       # rule_id -> RuleDef
        self.versions = {}    # rule_id -> list[RuleVersion]，按生效日升序
        self.txs = {}         # tx_id -> Transaction
        self.errors = []      # list[Error]

    # ---- 规则计算（占位实现：输入 * 参数；可按需替换） ----
    @staticmethod
    def calculate(input_value: float, param: float) -> float:
        return input_value * param

    # ---- 错误登记 ----
    def _error(self, source, line, code, message):
        self.errors.append(Error(source, line, code, message))

    # ---- 版本查找：半开区间 [eff_i, eff_{i+1})，切换当天归新版本 ----
    def _find_version(self, rule_id: str, tx_date: date):
        vs = self.versions.get(rule_id, [])
        idx = bisect.bisect_right([v.effective for v in vs], tx_date) - 1
        return vs[idx] if idx >= 0 else None

    # ---- 命令：RULE ----
    def _cmd_rule(self, source, line_no, rule_id, param_name):
        if rule_id in self.rules:
            self._error(source, line_no, "E_DUP_RULE",
                        "规则 %s 重复定义（首次定义于第 %d 行），已忽略"
                        % (rule_id, self.rules[rule_id].line))
            return
        self.rules[rule_id] = RuleDef(rule_id, param_name, line_no)
        self.versions.setdefault(rule_id, [])

    # ---- 命令：VERSION ----
    def _cmd_version(self, source, line_no, rule_id, version, effective, param):
        if rule_id not in self.rules:
            self._error(source, line_no, "E_UNKNOWN_RULE",
                        "版本流引用了不存在的规则 %s（版本 %s），已忽略"
                        % (rule_id, version))
            return
        vs = self.versions[rule_id]

        # 同版本重复生效 -> 拦截
        for ev in vs:
            if ev.version == version:
                self._error(source, line_no, "E_DUP_VERSION",
                            "规则 %s 版本 %s 重复生效（首次登记于第 %d 行），已拦截"
                            % (rule_id, version, ev.line))
                return

        # 同规则同生效日不同版本 -> 日期重叠，拦截
        for ev in vs:
            if ev.effective == effective:
                self._error(source, line_no, "E_OVERLAP",
                            "规则 %s 版本日期重叠：版本 %s 与版本 %s 的生效日同为 %s"
                            "（后者登记于第 %d 行），归属不明，已拦截"
                            % (rule_id, version, ev.version, effective, ev.line))
                return

        # 生效日期倒序：版本号更大但生效日更早（或反之）-> 报告，仍登记
        for ev in vs:
            if (_ver_key(ev.version) < _ver_key(version)) != (ev.effective <= effective):
                self._error(source, line_no, "E_VERSION_ORDER",
                            "规则 %s 生效日期倒序：版本 %s（生效日 %s）与版本 %s"
                            "（生效日 %s，第 %d 行）的版本号顺序与生效日顺序不一致"
                            % (rule_id, version, effective, ev.version,
                               ev.effective, ev.line))

        # 登记并保持按生效日升序
        nv = RuleVersion(rule_id, version, effective, param, line_no)
        bisect.insort(vs, nv, key=lambda v: v.effective)

        # 规则变更：该规则所有未结算交易按新版本重算；已结算交易冻结不动
        for tx in self.txs.values():
            if tx.rule_id == rule_id and tx.status == "OPEN":
                result = self.calculate(tx.input_value, param)
                tx.history.append(CalcRecord(
                    "recalc", version, param, result,
                    "规则变更为 %s（生效日 %s），未结算交易按新版本重算"
                    % (version, effective), line_no))

    # ---- 命令：TX ----
    def _cmd_tx(self, source, line_no, tx_id, tx_date, rule_id, input_value):
        if tx_id in self.txs:
            self._error(source, line_no, "E_DUP_TX",
                        "交易 %s 重复（首次出现于第 %d 行），已忽略"
                        % (tx_id, self.txs[tx_id].line))
            return
        if rule_id not in self.rules:
            self._error(source, line_no, "E_UNKNOWN_RULE",
                        "交易 %s 引用了不存在的规则 %s，已忽略" % (tx_id, rule_id))
            return
        ver = self._find_version(rule_id, tx_date)
        if ver is None:
            first = self.versions[rule_id][0] if self.versions[rule_id] else None
            hint = ("首个版本 %s 生效日为 %s" % (first.version, first.effective)
                    if first else "该规则尚无任何版本")
            self._error(source, line_no, "E_NO_VERSION",
                        "规则 %s 版本空档：交易 %s 日期 %s 之前无生效版本（%s）"
                        % (rule_id, tx_id, tx_date, hint))
            return
        tx = Transaction(tx_id, tx_date, rule_id, input_value, line_no)
        result = self.calculate(input_value, ver.param)
        tx.history.append(CalcRecord(
            "initial", ver.version, ver.param, result,
            "按交易日期命中版本 %s（生效日 %s）" % (ver.version, ver.effective),
            line_no))
        self.txs[tx_id] = tx

    # ---- 命令：SETTLE ----
    def _cmd_settle(self, source, line_no, tx_id, settle_date):
        tx = self.txs.get(tx_id)
        if tx is None:
            self._error(source, line_no, "E_UNKNOWN_TX",
                        "结算了不存在的交易 %s，已忽略" % tx_id)
            return
        if tx.status == "SETTLED":
            self._error(source, line_no, "E_DUP_SETTLE",
                        "交易 %s 已结算，重复结算已忽略" % tx_id)
            return
        if settle_date < tx.tx_date:
            self._error(source, line_no, "E_SETTLE_DATE",
                        "交易 %s 结算日 %s 早于交易日 %s，请核查"
                        % (tx_id, settle_date, tx.tx_date))
        tx.status = "SETTLED"
        cur = tx.current
        tx.history.append(CalcRecord(
            "freeze", cur.version, cur.param, cur.result,
            "于 %s 结算，结果冻结，后续规则变更不再影响" % settle_date, line_no))

    # ---- 流解析 ----
    def process_line(self, raw, source, line_no):
        text = raw.split("#", 1)[0].strip()
        if not text:
            return
        parts = text.split()
        cmd, args = parts[0].upper(), parts[1:]
        try:
            if cmd == "RULE" and len(args) == 2:
                self._cmd_rule(source, line_no, args[0], args[1])
            elif cmd == "VERSION" and len(args) == 4:
                self._cmd_version(source, line_no, args[0], args[1],
                                  date.fromisoformat(args[2]), float(args[3]))
            elif cmd == "TX" and len(args) == 4:
                self._cmd_tx(source, line_no, args[0],
                             date.fromisoformat(args[1]), args[2], float(args[3]))
            elif cmd == "SETTLE" and len(args) == 2:
                self._cmd_settle(source, line_no, args[0],
                                 date.fromisoformat(args[1]))
            else:
                self._error(source, line_no, "E_PARSE",
                            "无法识别的命令或参数个数错误：%s" % text)
        except ValueError as exc:
            self._error(source, line_no, "E_PARSE",
                        "参数格式错误（日期须为 YYYY-MM-DD，数值须为数字）：%s [%s]"
                        % (text, exc))

    def process_stream(self, lines, source):
        """处理一个输入流；可多次调用，状态在流之间延续。"""
        for line_no, raw in enumerate(lines, 1):
            self.process_line(raw, source, line_no)

    # ---- 输出 ----
    def render(self) -> str:
        out = []
        out.append("=" * 60)
        out.append("规则执行结果（切换当天归属新版本；已结算交易冻结）")
        out.append("=" * 60)
        if not self.txs:
            out.append("（无交易）")
        for tx in self.txs.values():
            cur = tx.current
            out.append("交易 %-6s 规则=%-4s 交易日=%s 输入=%s 状态=%s 当前结果=%s"
                       % (tx.tx_id, tx.rule_id, tx.tx_date, _num(tx.input_value),
                          tx.status, _num(cur.result)))
            for rec in tx.history:
                out.append("    [第%-3d行] %-7s 版本=%-4s 参数=%-6s 结果=%-8s %s"
                           % (rec.line, rec.event, rec.version, _num(rec.param),
                              _num(rec.result), rec.note))
        out.append("")
        out.append("=" * 60)
        out.append("错误报告（共 %d 条）" % len(self.errors))
        out.append("=" * 60)
        if not self.errors:
            out.append("（无错误）")
        for e in self.errors:
            out.append("[%s:%d] %-16s %s" % (e.source, e.line, e.code, e.message))
        return "\n".join(out)


# ---------------------------------------------------------------- 演示

DEMO = """\
# 规则定义与版本流
RULE R1 rate
VERSION R1 v1 2024-01-01 0.05
VERSION R1 v2 2024-04-01 0.08

# 交易流：T1 命中 v1；T2 在切换当天，归属新版本 v2
TX T1 2024-03-31 R1 1000
TX T2 2024-04-01 R1 1000
TX T3 2024-02-15 R1 200

# T1 结算后冻结；T2/T3 未结算，将被 v3 重算
SETTLE T1 2024-05-10
VERSION R1 v3 2024-07-01 0.10

# 各类错误演示
TX T4 2023-12-01 R1 5          # 空档：早于首个版本生效日
TX T5 2024-05-01 RX 1          # 引用不存在的规则
VERSION R1 v3 2024-08-01 0.09  # 同版本重复生效 -> 拦截
VERSION R1 v9 2024-07-01 0.50  # 与 v3 生效日重叠 -> 拦截
VERSION R1 v4 2024-03-01 0.07  # 生效日倒序（v4 早于 v3）-> 报告
"""


def main(argv) -> int:
    engine = Engine()
    args = argv[1:]
    if args == ["--demo"]:
        engine.process_stream(DEMO.splitlines(), "<demo>")
    elif args:
        for path in args:  # 多流按序处理，状态延续
            with open(path, encoding="utf-8") as fh:
                engine.process_stream(fh, path)
    else:
        engine.process_stream(sys.stdin, "<stdin>")
    print(engine.render())
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
