#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topo_tool.py — 拓扑/路由仿真与校验工具（纯 Python 标准库，单文件）

输入 DSL（每行一条指令，# 之后为注释，空行忽略）：
  node  <节点名>                                   定义节点
  link  <链路ID> <节点A> <节点B> <enable|disable>   定义链路或变更其状态（跨流状态延续）
  route <源> <目的> <路径节点...>                   定义路由（路径可空格分隔，或 A-B-C / A>B>C 单串）

自定规则：
  * 链路三态：启用(转发) / 阻塞(成环被隔离，不转发) / 停用。
  * 环路检测：启用链路前，若两端点在“已启用链路”构成的图中已连通，则启用将成环
    —— 报告并定位成环链路与完整环路，将该链路置为阻塞（仿生成树）。
  * 级联重路由：停用链路后，先尝试解除不再成环的阻塞链路（拓扑自愈），
    再对受影响路由按“最少跳数优先，跳数相同取节点名字典序最小”重算；
    无可用路径则报告源目的对并将路由标记为中断。
  * 状态延续：链路重新启用后，自动尝试修复处于中断状态的路由（同一选路规则）。
"""
import sys
import heapq
from collections import defaultdict

UP, BLOCKED, DOWN = "up", "blocked", "down"
STATE_CN = {UP: "启用", BLOCKED: "阻塞", DOWN: "停用"}


class Topo:
    def __init__(self):
        self.nodes = set()
        self.links = {}          # lid -> {"a","b","state"}
        self.pair2link = {}      # (端点小, 端点大) -> lid
        self.routes = []         # {"id","src","dst","path","links","status"}
        self.errors = []
        self.events = []
        self._seq = 0

    # ---------- 记录 ----------
    def err(self, lineno, msg):
        self.errors.append("[line %d] %s" % (lineno, msg))

    def ev(self, lineno, msg):
        self.events.append("[line %d] %s" % (lineno, msg))

    # ---------- 图算法 ----------
    def enabled_adj(self):
        adj = defaultdict(list)
        for lid, lk in self.links.items():
            if lk["state"] == UP:
                adj[lk["a"]].append((lk["b"], lid))
                adj[lk["b"]].append((lk["a"], lid))
        for nbrs in adj.values():
            nbrs.sort()
        return adj

    def shortest_path(self, src, dst):
        """自定选路规则：最少跳数优先，并列时取节点名字典序最小的路径。"""
        adj = self.enabled_adj()
        best = {src: (0, (src,))}
        pq = [(0, (src,), src)]
        while pq:
            hops, path, u = heapq.heappop(pq)
            if best.get(u) != (hops, path):
                continue
            if u == dst:
                return list(path)
            for v, _lid in adj.get(u, ()):
                key = (hops + 1, path + (v,))
                if v not in best or key < best[v]:
                    best[v] = key
                    heapq.heappush(pq, (key[0], key[1], v))
        return None

    def path_links(self, path):
        return [self.pair2link.get(tuple(sorted((u, v))))
                for u, v in zip(path, path[1:])]

    def heal_blocked(self, lineno):
        """拓扑自愈：解除不再成环的阻塞链路（加边不会断开图，单趟即可）。"""
        for lid, lk in self.links.items():
            if lk["state"] == BLOCKED and \
                    self.shortest_path(lk["a"], lk["b"]) is None:
                lk["state"] = UP
                self.ev(lineno, "阻塞链路 %s(%s-%s) 环路已消失，自动解除阻塞"
                        % (lid, lk["a"], lk["b"]))

    # ---------- 指令：node ----------
    def cmd_node(self, lineno, name):
        if name in self.nodes:
            self.err(lineno, "重复定义节点 %s，已忽略" % name)
            return
        self.nodes.add(name)
        self.ev(lineno, "定义节点 %s" % name)

    # ---------- 指令：link ----------
    def cmd_link(self, lineno, lid, a, b, want_enable):
        for n in (a, b):
            if n not in self.nodes:
                self.err(lineno, "链路 %s 引用不存在节点 %s，已忽略" % (lid, n))
                return
        if a == b:
            self.err(lineno, "链路 %s 两端点相同(%s)，已忽略" % (lid, a))
            return
        key = tuple(sorted((a, b)))
        created = False
        if lid not in self.links:
            if key in self.pair2link:
                self.err(lineno, "链路 %s 与已有链路 %s 端点对(%s-%s)冲突，已忽略"
                         % (lid, self.pair2link[key], a, b))
                return
            self.links[lid] = {"a": a, "b": b, "state": DOWN}
            self.pair2link[key] = lid
            created = True
            self.ev(lineno, "定义链路 %s(%s-%s)，初始停用" % (lid, a, b))
        else:
            lk = self.links[lid]
            if {lk["a"], lk["b"]} != {a, b}:
                self.err(lineno, "链路 %s 端点与定义(%s-%s)不一致，已忽略"
                         % (lid, lk["a"], lk["b"]))
                return
        if want_enable:
            self._enable(lineno, lid)
        elif not created:
            self._disable(lineno, lid)

    def _enable(self, lineno, lid):
        lk = self.links[lid]
        if lk["state"] == UP:
            self.err(lineno, "重复启用已启用链路 %s(%s-%s)，已拦截"
                     % (lid, lk["a"], lk["b"]))
            return
        a, b = lk["a"], lk["b"]
        alt = self.shortest_path(a, b)          # 环路检测：两端点已连通则成环
        if alt is not None:
            cyc_links = self.path_links(alt)
            cyc = "-".join(alt + [alt[0]])
            lk["state"] = BLOCKED
            self.err(lineno, "成环：启用链路 %s(%s-%s) 将与已有链路 [%s] 构成环路 %s，"
                             "已定位成环链路 %s 并置为阻塞"
                     % (lid, a, b, ",".join(cyc_links), cyc, lid))
            return
        lk["state"] = UP
        self.ev(lineno, "启用链路 %s(%s-%s)" % (lid, a, b))
        for r in self.routes:                    # 状态延续：修复中断路由
            if r["status"] == "中断":
                p = self.shortest_path(r["src"], r["dst"])
                if p:
                    r["path"], r["links"], r["status"] = p, self.path_links(p), "正常"
                    self.ev(lineno, "路由 %s(%s->%s) 恢复：%s（链路 %s）"
                            % (r["id"], r["src"], r["dst"],
                               "-".join(p), ",".join(r["links"])))

    def _disable(self, lineno, lid):
        lk = self.links[lid]
        if lk["state"] == DOWN:
            self.err(lineno, "重复停用已停用链路 %s(%s-%s)，已拦截"
                     % (lid, lk["a"], lk["b"]))
            return
        lk["state"] = DOWN
        self.ev(lineno, "停用链路 %s(%s-%s)" % (lid, lk["a"], lk["b"]))
        self.heal_blocked(lineno)                # 先自愈：解除不再成环的阻塞链路
        for r in self.routes:                    # 再级联重路由
            if r["status"] == "中断" or lid not in r["links"]:
                continue
            p = self.shortest_path(r["src"], r["dst"])
            if p:
                r["path"], r["links"] = p, self.path_links(p)
                self.ev(lineno, "路由 %s(%s->%s) 级联重路由：%s（链路 %s）"
                        % (r["id"], r["src"], r["dst"],
                           "-".join(p), ",".join(r["links"])))
            else:
                r["status"], r["path"], r["links"] = "中断", [], []
                self.err(lineno, "重路由失败：源目的对 %s->%s 无可用路径，路由 %s 中断"
                         % (r["src"], r["dst"], r["id"]))

    # ---------- 指令：route ----------
    def cmd_route(self, lineno, src, dst, path):
        for n in (src, dst):
            if n not in self.nodes:
                self.err(lineno, "路由引用不存在节点 %s，已忽略" % n)
                return
        if not path:
            self.err(lineno, "路由 %s->%s 缺少路径，已忽略" % (src, dst))
            return
        if path[0] != src or path[-1] != dst:
            self.err(lineno, "路由路径 %s 与源目的 %s->%s 不一致，已忽略"
                     % ("-".join(path), src, dst))
            return
        if len(set(path)) != len(path):
            seen, dup = set(), None
            for n in path:
                if n in seen:
                    dup = n
                    break
                seen.add(n)
            self.err(lineno, "路径成环：路径 %s 内含重复节点 %s，已忽略"
                     % ("-".join(path), dup))
            return
        for n in path:
            if n not in self.nodes:
                self.err(lineno, "路径引用不存在节点 %s，已忽略" % n)
                return
        lids = []
        for u, v in zip(path, path[1:]):
            lid = self.pair2link.get(tuple(sorted((u, v))))
            if lid is None:
                self.err(lineno, "路径引用不存在链路 %s-%s，已忽略" % (u, v))
                return
            st = self.links[lid]["state"]
            if st != UP:
                self.err(lineno, "状态不一致：路径 %s 使用%s链路 %s(%s-%s)，已忽略"
                         % ("-".join(path), STATE_CN[st], lid, u, v))
                return
            lids.append(lid)
        self._seq += 1
        rid = "R%d" % self._seq
        self.routes.append({"id": rid, "src": src, "dst": dst,
                            "path": path, "links": lids, "status": "正常"})
        self.ev(lineno, "安装路由 %s %s->%s：%s（链路 %s）"
                % (rid, src, dst, "-".join(path), ",".join(lids)))


def parse_path_tokens(tokens):
    if len(tokens) == 1 and ("-" in tokens[0] or ">" in tokens[0]):
        return [x for x in tokens[0].replace(">", "-").split("-") if x]
    return tokens


def run(lines):
    topo = Topo()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "node" and len(args) == 1:
            topo.cmd_node(lineno, args[0])
        elif cmd == "link" and len(args) == 4:
            st = args[3].lower()
            if st not in ("enable", "disable"):
                topo.err(lineno, "链路 %s 状态非法(%s)，应为 enable/disable"
                         % (args[0], args[3]))
                continue
            topo.cmd_link(lineno, args[0], args[1], args[2], st == "enable")
        elif cmd == "route" and len(args) >= 3:
            topo.cmd_route(lineno, args[0], args[1], parse_path_tokens(args[2:]))
        else:
            topo.err(lineno, "无法解析的指令: %s" % raw.strip())
    return topo


def report(topo):
    out = ["==== 事件日志 ===="]
    out += topo.events or ["(无)"]
    out += ["", "==== 拓扑状态 ===="]
    out.append("节点(%d): %s" % (len(topo.nodes), " ".join(sorted(topo.nodes)) or "(无)"))
    out.append("链路(%d):" % len(topo.links))
    for lid in sorted(topo.links):
        lk = topo.links[lid]
        out.append("  %-4s %s-%s  %s" % (lid, lk["a"], lk["b"], STATE_CN[lk["state"]]))
    out += ["", "==== 路由清单 ===="]
    if not topo.routes:
        out.append("  (无)")
    for r in topo.routes:
        p = "-".join(r["path"]) if r["path"] else "(无路径)"
        out.append("  %-4s %s->%s  [%s]  %s"
                   % (r["id"], r["src"], r["dst"], r["status"], p))
    out += ["", "==== 错误报告 ===="]
    out += topo.errors or ["(无错误)"]
    return "\n".join(out)


def main(argv):
    lines = open(argv[1], encoding="utf-8").readlines() if len(argv) > 1 \
        else sys.stdin.readlines()
    print(report(run(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
