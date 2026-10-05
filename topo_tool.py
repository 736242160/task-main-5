#!/usr/bin/env python3
"""topo_tool.py — 网络拓扑与路由管理工具（纯 Python 标准库，单文件）

输入 DSL（行式命令，# 后为注释，空行忽略；事件按顺序处理，状态跨流延续）：
  node    <节点> ...                      定义节点
  link    <链路> <节点A> <节点B>           定义链路（初始为停用）
  enable  <链路>                          启用链路（触发环路检测）
  disable <链路>                          停用链路（触发级联重路由）
  route   <路由> <源> <目的> <节点> ...    定义路由（源 目的 路径）

自定规则：
  环路检测：并查集。启用链路两端在已启用链路图中已连通即判定成环，
           报告并定位该成环链路；链路仍启用，作为冗余备份参与重路由。
  级联重路由：BFS 最少跳数，邻接按字典序展开保证结果确定；
           无可用路径则报告并定位源目的对，路由置为中断。
用法：python3 topo_tool.py [输入文件]   （缺省读标准输入）
"""

import sys
from collections import defaultdict, deque


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def connected(self, a, b):
        return self.find(a) == self.find(b)

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


class Link:
    def __init__(self, lid, a, b):
        self.id = lid
        self.a = a
        self.b = b
        self.enabled = False


class Route:
    def __init__(self, rid, src, dst, path):
        self.id = rid
        self.src = src
        self.dst = dst
        self.path = path
        self.active = True


class Topology:
    def __init__(self):
        self.nodes = set()
        self.links = {}        # lid -> Link
        self.pair_link = {}    # frozenset({a,b}) -> lid
        self.routes = {}       # rid -> Route
        self.uf = UnionFind()
        self.logs = []         # (行号, 消息)
        self.errors = []       # (行号, 消息)

    def log(self, lineno, msg):
        self.logs.append((lineno, msg))

    def error(self, lineno, msg):
        self.errors.append((lineno, msg))

    # ---- 节点与链路定义 ----
    def add_node(self, lineno, name):
        if name in self.nodes:
            self.error(lineno, "节点 %s 重复定义，已忽略" % name)
            return
        self.nodes.add(name)
        self.uf.find(name)

    def add_link(self, lineno, lid, a, b):
        if lid in self.links:
            self.error(lineno, "链路 %s 重复定义，已忽略" % lid)
            return
        for n in (a, b):
            if n not in self.nodes:
                self.error(lineno, "链路 %s 引用了不存在的节点 %s，已拒绝" % (lid, n))
                return
        if a == b:
            self.error(lineno, "链路 %s 两端相同（自环），已拒绝" % lid)
            return
        key = frozenset((a, b))
        if key in self.pair_link:
            self.error(lineno, "节点对 %s-%s 已存在链路 %s，链路 %s 重复定义，已忽略"
                               % (a, b, self.pair_link[key], lid))
            return
        self.links[lid] = Link(lid, a, b)
        self.pair_link[key] = lid

    # ---- 链路启用（含环路检测） ----
    def enable_link(self, lineno, lid):
        link = self.links.get(lid)
        if link is None:
            self.error(lineno, "引用了不存在的链路 %s" % lid)
            return
        if link.enabled:
            self.error(lineno, "重复启用已启用链路 %s，已拦截" % lid)
            return
        if self.uf.connected(link.a, link.b):
            self.error(lineno, "环路检测：启用链路 %s(%s-%s) 后形成环路，成环链路定位为 %s；"
                               "按规则链路仍启用，作为冗余备份" % (lid, link.a, link.b, lid))
        link.enabled = True
        self.uf.union(link.a, link.b)
        self.log(lineno, "链路 %s(%s-%s) 已启用" % (lid, link.a, link.b))

    # ---- 链路停用（含级联重路由） ----
    def disable_link(self, lineno, lid):
        link = self.links.get(lid)
        if link is None:
            self.error(lineno, "引用了不存在的链路 %s" % lid)
            return
        if not link.enabled:
            self.error(lineno, "链路 %s 未处于启用状态，停用请求已拦截" % lid)
            return
        link.enabled = False
        self.log(lineno, "链路 %s(%s-%s) 已停用" % (lid, link.a, link.b))
        self._rebuild_union_find()
        self._cascade_reroute(lineno, lid)

    def _rebuild_union_find(self):
        self.uf = UnionFind()
        for n in self.nodes:
            self.uf.find(n)
        for link in self.links.values():
            if link.enabled:
                self.uf.union(link.a, link.b)

    def _cascade_reroute(self, lineno, lid):
        for route in self.routes.values():
            if not route.active or lid not in self._path_links(route.path):
                continue
            new_path = self.find_path(route.src, route.dst)
            if new_path is not None:
                self.log(lineno, "路由 %s 受链路 %s 停用影响，级联重路由：%s -> %s"
                                 % (route.id, lid,
                                    "-".join(route.path), "-".join(new_path)))
                route.path = new_path
            else:
                route.active = False
                self.error(lineno, "链路 %s 停用后，源目的对 %s->%s 无可用路径，路由 %s 中断"
                                   % (lid, route.src, route.dst, route.id))

    # ---- 路由定义（含一致性校验） ----
    def add_route(self, lineno, rid, src, dst, path):
        if rid in self.routes:
            self.error(lineno, "路由 %s 重复定义，已忽略" % rid)
            return
        for n in (src, dst):
            if n not in self.nodes:
                self.error(lineno, "路由 %s 引用了不存在的节点 %s，已拒绝" % (rid, n))
                return
        if not path:
            self.error(lineno, "路由 %s 路径为空，已拒绝" % rid)
            return
        for n in path:
            if n not in self.nodes:
                self.error(lineno, "路由 %s 路径引用了不存在的节点 %s，已拒绝" % (rid, n))
                return
        if path[0] != src or path[-1] != dst:
            self.error(lineno, "路由 %s 路径端点与源目的不一致（%s->%s 与 %s），已拒绝"
                               % (rid, src, dst, "-".join(path)))
            return
        seen = set()
        for n in path:
            if n in seen:
                self.error(lineno, "路由 %s 路径成环：节点 %s 在路径 %s 中重复出现，已拒绝"
                                   % (rid, n, "-".join(path)))
                return
            seen.add(n)
        for i in range(len(path) - 1):
            lid = self.pair_link.get(frozenset((path[i], path[i + 1])))
            if lid is None:
                self.error(lineno, "路由 %s 引用了不存在的链路（%s-%s 之间无链路），已拒绝"
                                   % (rid, path[i], path[i + 1]))
                return
            if not self.links[lid].enabled:
                self.error(lineno, "路由 %s 状态不一致：路径使用停用链路 %s（%s-%s），已拒绝"
                                   % (rid, lid, path[i], path[i + 1]))
                return
        self.routes[rid] = Route(rid, src, dst, list(path))
        self.log(lineno, "路由 %s 已建立：%s->%s 路径 %s"
                         % (rid, src, dst, "-".join(path)))

    # ---- 工具 ----
    def _path_links(self, path):
        lids = []
        for i in range(len(path) - 1):
            lid = self.pair_link.get(frozenset((path[i], path[i + 1])))
            if lid is not None:
                lids.append(lid)
        return lids

    def find_path(self, src, dst):
        """BFS 最少跳数路径；邻接按字典序展开，结果确定。无路径返回 None。"""
        if src == dst:
            return [src]
        adj = defaultdict(list)
        for link in self.links.values():
            if link.enabled:
                adj[link.a].append(link.b)
                adj[link.b].append(link.a)
        for neighbors in adj.values():
            neighbors.sort()
        prev = {src: None}
        queue = deque([src])
        while queue:
            u = queue.popleft()
            for v in adj[u]:
                if v in prev:
                    continue
                prev[v] = u
                if v == dst:
                    path = [dst]
                    while path[-1] != src:
                        path.append(prev[path[-1]])
                    path.reverse()
                    return path
                queue.append(v)
        return None


def process(topo, lines):
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "node":
            if not args:
                topo.error(lineno, "node 命令缺少节点名")
            for name in args:
                topo.add_node(lineno, name)
        elif cmd == "link":
            if len(args) != 3:
                topo.error(lineno, "link 命令格式错误（应为：link <链路> <节点A> <节点B>）：%s" % line)
            else:
                topo.add_link(lineno, args[0], args[1], args[2])
        elif cmd == "enable":
            if len(args) != 1:
                topo.error(lineno, "enable 命令格式错误（应为：enable <链路>）：%s" % line)
            else:
                topo.enable_link(lineno, args[0])
        elif cmd == "disable":
            if len(args) != 1:
                topo.error(lineno, "disable 命令格式错误（应为：disable <链路>）：%s" % line)
            else:
                topo.disable_link(lineno, args[0])
        elif cmd == "route":
            if len(args) < 4:
                topo.error(lineno, "route 命令格式错误（应为：route <路由> <源> <目的> <节点...>）：%s" % line)
            else:
                topo.add_route(lineno, args[0], args[1], args[2], args[3:])
        else:
            topo.error(lineno, "未知命令：%s" % parts[0])


def render_report(topo):
    out = []
    sep = "=" * 64

    out.append(sep)
    out.append("【处理日志】")
    if topo.logs:
        for lineno, msg in topo.logs:
            out.append("  [行%03d] %s" % (lineno, msg))
    else:
        out.append("  （无）")

    out.append(sep)
    out.append("【拓扑状态】")
    out.append("  节点（%d）：%s" % (len(topo.nodes), " ".join(sorted(topo.nodes))))
    out.append("  链路（%d）：" % len(topo.links))
    for lid in sorted(topo.links):
        link = topo.links[lid]
        state = "启用" if link.enabled else "停用"
        out.append("    %-6s %-7s %s" % (lid, "%s-%s" % (link.a, link.b), state))

    out.append(sep)
    out.append("【路由清单】")
    if topo.routes:
        for rid in sorted(topo.routes):
            route = topo.routes[rid]
            status = "活动" if route.active else "中断"
            out.append("    %-6s %s->%s  路径 %-16s 状态 %s"
                       % (rid, route.src, route.dst, "-".join(route.path), status))
    else:
        out.append("  （无）")

    out.append(sep)
    out.append("【错误报告】（共 %d 条）" % len(topo.errors))
    if topo.errors:
        for i, (lineno, msg) in enumerate(topo.errors, 1):
            out.append("  %2d. [行%03d] %s" % (i, lineno, msg))
    else:
        out.append("  （无）")
    out.append(sep)
    return "\n".join(out)


def main(argv):
    if len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()
    topo = Topology()
    process(topo, lines)
    print(render_report(topo))
    return 1 if topo.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
