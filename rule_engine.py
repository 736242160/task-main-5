#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_engine.py — 规则版本化执行引擎（纯标准库，单文件）

输入流（行式文本，可多文件顺序喂入，状态跨流延续）：
    RULE    <规则编号> <表达式> [参数=默认值 ...]
    VERSION <规则编号> <版本号> <生效日期YYYY-MM-DD> [参数=值 ...]
    TXN     <交易号> <交易日期YYYY-MM-DD> <规则编号> [输入=值 ...]
    SETTLE  <交易号>
    # 注释；空行忽略

语义约定：
  * 切换点归属：生效日期当天起即归【新版本】（区间 [生效日, 下一版本生效日) 左闭右开）。
  * 交易按交易日期命中版本；未结算交易在新版本到达时按最新版本重算；已结算交易结果冻结。
  * 表达式为安全子集（ast 白名单）：数字、参数名、交易输入名、+ - * / // % **、括号、
    min/max/round/abs。

用法：
    python3 rule_engine.py 流文件1 [流文件2 ...]     # 多流顺序处理，状态延续
    cat 流文件 | python3 rule_engine.py              # 无参数时读标准输入
"""

import argparse
import ast
import shlex
import sys
from bisect import bisect_right
from datetime import date


# ---------------------------------------------------------------- 安全表达式求值

_FUNCS = {"min": min, "max": max, "round": round, "abs": abs}
_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}


def safe_eval(expr, names):
    """在白名单语法内求值；非法语法/未知名字抛 ValueError/KeyError。"""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("只允许数字字面量: %r" % node.value)
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            raise KeyError("未知变量: %s" % node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = ev(node.operand)
            return val if isinstance(node.op, ast.UAdd) else -val
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("表达式含不允许的语法: %s" % ast.dump(node))
    return ev(ast.parse(expr, mode="eval"))


# ---------------------------------------------------------------- 数据模型

class RuleDef:
    def __init__(self, rule_id, expr, defaults):
        self.rule_id = rule_id
        self.expr = expr
        self.defaults = dict(defaults)


class RuleVersion:
    def __init__(self, rule_id, version, eff_date, params):
        self.rule_id = rule_id
        self.version = version
        self.eff_date = eff_date
        self.params = dict(params)


class Txn:
    def __init__(self, txn_id, txn_date, rule_id, inputs):
        self.txn_id = txn_id
        self.txn_date = txn_date
        self.rule_id = rule_id
        self.inputs = dict(inputs)
        self.settled = False
        self.version_used = None   # 命中的版本号
        self.result = None         # 最近一次计算结果
        self.ok = False            # 是否已成功计算


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self):
        self.rules = {}                 # rule_id -> RuleDef
        self.versions = {}              # rule_id -> [RuleVersion] 按生效日期升序
        self._seen_versions = set()     # (rule_id, version) 用于重复生效拦截
        self.txns = {}                  # txn_id -> Txn
        self.errors = []                # (类别, 定位, 说明)
        self.recalcs = []               # (txn_id, 旧版本, 旧结果, 新版本, 新结果)

    # ---- 错误记录 ----
    def _err(self, kind, where, msg):
        self.errors.append((kind, where, msg))

    # ---- 版本查找：生效日当天归新版本（左闭右开）----
    def _match_version(self, rule_id, on_date):
        vers = self.versions.get(rule_id, [])
        dates = [v.eff_date for v in vers]
        idx = bisect_right(dates, on_date) - 1
        return vers[idx] if idx >= 0 else None

    # ---- 指令处理 ----
    def define_rule(self, rule_id, expr, defaults):
        if rule_id in self.rules:
            self._err("RULE_DUP", "规则 %s" % rule_id, "规则重复定义，已忽略")
            return
        self.rules[rule_id] = RuleDef(rule_id, expr, defaults)
        self.versions.setdefault(rule_id, [])

    def add_version(self, rule_id, version, eff_date, params):
        if rule_id not in self.rules:
            self._err("VERSION_UNKNOWN_RULE", "规则 %s 版本 %s" % (rule_id, version),
                      "版本指向不存在的规则，已忽略")
            return
        if (rule_id, version) in self._seen_versions:
            self._err("VERSION_DUP", "规则 %s 版本 %s" % (rule_id, version),
                      "同版本重复生效，已拦截")
            return
        vers = self.versions[rule_id]
        for old in vers:
            if old.eff_date == eff_date:
                self._err("VERSION_OVERLAP", "规则 %s 版本 %s" % (rule_id, version),
                          "与版本 %s 生效日期重叠(%s)，已忽略"
                          % (old.version, eff_date.isoformat()))
                return
            if (version > old.version and eff_date < old.eff_date) or \
               (version < old.version and eff_date > old.eff_date):
                self._err("VERSION_OUT_OF_ORDER", "规则 %s 版本 %s" % (rule_id, version),
                          "生效日期倒序：版本 %s 生效于 %s，早于更低版本 %s 的 %s（仍按日期纳入时间线）"
                          % (version, eff_date.isoformat(), old.version, old.eff_date.isoformat()))
        self._seen_versions.add((rule_id, version))
        vers.append(RuleVersion(rule_id, version, eff_date, params))
        vers.sort(key=lambda v: (v.eff_date, v.version))
        self._recalc_unsettled(rule_id)

    def _recalc_unsettled(self, rule_id):
        """规则变更后：未结算交易按最新版本重算；已结算交易保持旧结果。"""
        vers = self.versions[rule_id]
        if not vers:
            return
        latest = vers[-1]
        for txn in self.txns.values():
            if txn.rule_id != rule_id or txn.settled:
                continue
            old_ver, old_res, old_ok = txn.version_used, txn.result, txn.ok
            if self._execute(txn, latest, quiet=True):
                if (not old_ok) or old_ver != txn.version_used or old_res != txn.result:
                    self.recalcs.append((txn.txn_id,
                                         old_ver if old_ok else "-",
                                         old_res if old_ok else "-",
                                         txn.version_used, txn.result))

    def _execute(self, txn, version, quiet=False):
        rule = self.rules.get(txn.rule_id)
        if rule is None:
            return False
        names = dict(rule.defaults)
        names.update(version.params)
        names.update(txn.inputs)
        try:
            txn.result = safe_eval(rule.expr, names)
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            if not quiet:
                self._err("TXN_EVAL", "交易 %s" % txn.txn_id,
                          "规则 %s 版本 %s 计算失败: %s" % (txn.rule_id, version.version, exc))
            return False
        txn.version_used = version.version
        txn.ok = True
        return True

    def add_txn(self, txn_id, txn_date, rule_id, inputs):
        if txn_id in self.txns:
            self._err("TXN_DUP", "交易 %s" % txn_id, "交易号重复，已忽略")
            return
        txn = Txn(txn_id, txn_date, rule_id, inputs)
        self.txns[txn_id] = txn
        if rule_id not in self.rules:
            self._err("TXN_UNKNOWN_RULE", "交易 %s" % txn_id,
                      "引用不存在的规则 %s" % rule_id)
            return
        version = self._match_version(rule_id, txn_date)
        if version is None:
            first = self.versions[rule_id][0]
            self._err("TXN_VERSION_GAP", "交易 %s" % txn_id,
                      "规则 %s 在 %s 无生效版本（最早版本 %s 自 %s 生效，存在空档）"
                      % (rule_id, txn_date.isoformat(), first.version,
                         first.eff_date.isoformat()))
            return
        self._execute(txn, version)

    def settle(self, txn_id):
        txn = self.txns.get(txn_id)
        if txn is None:
            self._err("SETTLE_UNKNOWN_TXN", "交易 %s" % txn_id, "结算不存在的交易")
            return
        if txn.settled:
            self._err("SETTLE_DUP", "交易 %s" % txn_id, "重复结算，已忽略")
            return
        txn.settled = True

    # ---- 流解析（可多次调用，状态跨流延续）----
    def process_stream(self, lines, source="<stream>"):
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                parts = shlex.split(line, comments=True)
            except ValueError as exc:
                self._err("PARSE", "%s:%d" % (source, lineno), "行解析失败: %s" % exc)
                continue
            where = "%s:%d" % (source, lineno)
            try:
                self._dispatch(parts, where)
            except (ValueError, IndexError) as exc:
                self._err("PARSE", where, "指令格式错误: %s" % exc)

    def _dispatch(self, parts, where):
        cmd = parts[0].upper()
        if cmd == "RULE":
            self.define_rule(parts[1], parts[2], _parse_kv(parts[3:]))
        elif cmd == "VERSION":
            self.add_version(parts[1], int(parts[2]), _parse_date(parts[3]),
                             _parse_kv(parts[4:]))
        elif cmd == "TXN":
            self.add_txn(parts[1], _parse_date(parts[2]), parts[3],
                         _parse_kv(parts[4:]))
        elif cmd == "SETTLE":
            self.settle(parts[1])
        else:
            self._err("PARSE", where, "未知指令: %s" % parts[0])

    # ---- 输出 ----
    def report(self, out=sys.stdout):
        w = out.write
        w("== 规则执行结果 ==\n")
        w("%-8s %-12s %-8s %-6s %12s %-6s\n" % ("交易号", "日期", "规则", "版本", "结果", "状态"))
        for tid in sorted(self.txns):
            t = self.txns[tid]
            w("%-8s %-12s %-8s %-6s %12s %-6s\n" % (
                t.txn_id, t.txn_date.isoformat(), t.rule_id,
                t.version_used if t.ok else "-",
                t.result if t.ok else "-",
                "已结算" if t.settled else "未结算"))
        w("\n== 重算记录（规则变更后未结算交易按新版本重算）==\n")
        if not self.recalcs:
            w("（无）\n")
        for tid, ov, orr, nv, nr in self.recalcs:
            w("交易 %-8s 版本 %s -> %s，结果 %s -> %s\n" % (tid, ov, nv, orr, nr))
        w("\n== 错误报告 ==\n")
        if not self.errors:
            w("（无）\n")
        for kind, where, msg in self.errors:
            w("[%s] %s: %s\n" % (kind, where, msg))


def _parse_date(s):
    return date.fromisoformat(s)


def _parse_kv(pairs):
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        if not k or not v:
            raise ValueError("参数应为 k=v 形式: %r" % p)
        out[k] = _parse_num(v)
    return out


def _parse_num(s):
    try:
        return int(s)
    except ValueError:
        return float(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="规则版本化执行引擎（纯标准库单文件）")
    ap.add_argument("streams", nargs="*", help="输入流文件，可多个（状态跨流延续）；缺省读标准输入")
    args = ap.parse_args(argv)
    engine = Engine()
    if args.streams:
        for path in args.streams:
            with open(path, encoding="utf-8") as fh:
                engine.process_stream(fh, source=path)
    else:
        engine.process_stream(sys.stdin, source="<stdin>")
    engine.report()
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main())
