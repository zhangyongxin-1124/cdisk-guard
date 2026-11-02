# -*- coding: utf-8 -*-
"""_test_fastscan.py — 高速扫描引擎回归测试

思路：造一棵临时目录树，用「改造前的旧算法」和「新的并发快扫」各跑一遍，
逐项比对结果（路径/大小/时间/分类桶），确保提速的同时行为零变化。
另测：junction 不跟随、取消、进度回调、空目录、不存在的根。
"""
import os
import sys
import time
import shutil
import tempfile
import threading
import datetime
import subprocess
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

import guard_fastscan
import guard_today
import guard_legacy
import guard_dashboard

PASS = 0
FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS ", name)
    else:
        FAIL += 1
        print("  FAIL ", name, extra)


def build_tree(root: Path):
    """造 4 层目录、若干文件，含 1 个老文件（2020 年）。"""
    (root / "a" / "a1" / "a1x").mkdir(parents=True)
    (root / "a" / "a2").mkdir(parents=True)
    (root / "b" / "b1").mkdir(parents=True)
    (root / "c").mkdir(parents=True)
    files = {
        "top.txt": 100, "a/a.dat": 200, "a/a1/a1.dat": 300,
        "a/a1/a1x/deep.bin": 400, "a/a2/a2.dat": 500,
        "b/b.dat": 600, "b/b1/b1.dat": 700, "c/c.dat": 800,
    }
    for rel, size in files.items():
        p = root / rel
        p.write_bytes(b"x" * size)
        os.utime(p, (time.time(), time.time()))
    old = root / "old.log"
    old.write_bytes(b"0" * 123)
    ts = datetime.datetime(2020, 1, 1).timestamp()
    os.utime(old, (ts, ts))
    return files


# ---------- 旧算法参照实现 ----------
def ref_today(root, depth, day0, skip_fn):
    items = []
    base_depth = str(root).rstrip("\\").count("\\")
    for cur, dirs, files in os.walk(root):
        lvl = str(cur).count("\\") - base_depth
        if lvl >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not skip_fn(cur, d)]
        for d in list(dirs):
            full = os.path.join(cur, d)
            try:
                st = os.stat(full, follow_symlinks=False)
                if st.st_mtime >= day0:
                    items.append((full, True, 0))
            except OSError:
                pass
        for fn in files:
            full = os.path.join(cur, fn)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime >= day0:
                items.append((full, False, st.st_size))
    return sorted(items)


def ref_legacy(root, depth, skip_fn):
    """返回 {key: (size, count)}，与原 guard_legacy.scan 的桶口径一致。"""
    buckets = {}
    stack = [(str(root), 0, str(root))]
    while stack:
        cur, lvl, key = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    isdir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if isdir:
                    if skip_fn(e.name):
                        continue
                    nk = e.path if (lvl + 1) <= depth else key
                    stack.append((e.path, lvl + 1, nk))
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                b = buckets.setdefault(key, [0, 0])
                b[0] += int(st.st_size or 0)
                b[1] += 1
    return {k: tuple(v) for k, v in buckets.items()}


def ref_aggregate(root, depth):
    agg = {}
    root_str = str(root)
    for cur, dirs, files in os.walk(root_str):
        parts = Path(cur).relative_to(root).parts
        if len(parts) > depth:
            dirs[:] = []
            continue
        prefix = "/".join(parts[:depth]) if parts else "."
        for fn in files:
            try:
                s = os.stat(os.path.join(cur, fn)).st_size
            except OSError:
                continue
            key = f"{root_str}\\{prefix}" if prefix != "." else root_str
            agg[key] = agg.get(key, 0) + s
    return agg


class _Today(guard_today.TodayScanner):
    def scan_roots(self):
        return [(self._root, self._depth)]


class _Legacy(guard_legacy.LegacyScanner):
    def roots(self):
        return [(self._root, self._depth, set())]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="guard_fast_"))
    try:
        # ============ 1. iter_entries ============
        print("[1] 单层枚举")
        root = tmp / "tree"
        root.mkdir()
        files = build_tree(root)
        es = list(guard_fastscan.iter_entries(str(root)))
        names = {e.name for e in es}
        ck("枚举到所有顶层条目", {"a", "b", "c", "top.txt", "old.log"} <= names,
           str(names))
        ck("目录标记正确", all(e.is_dir for e in es if e.name in ("a", "b", "c")))
        ck("文件大小正确",
           {e.name: e.size for e in es if not e.is_dir}.get("top.txt") == 100)
        ck("时间可读", all(e.mtime > 0 for e in es))
        ck("不存在的目录不抛异常",
           list(guard_fastscan.iter_entries(str(tmp / "nope"))) == [])

        # ============ 2. junction 不跟随 ============
        print("[2] 目录联接防循环")
        jsrc = tmp / "jsrc"
        jsrc.mkdir()
        (jsrc / "inside.bin").write_bytes(b"x" * 50)
        jdst = root / "link_alias"
        made = False
        if sys.platform == "win32":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(jdst), str(jsrc)],
                               capture_output=True)
            made = r.returncode == 0
        if made:
            es = list(guard_fastscan.iter_entries(str(root)))
            ck("junction 被跳过", "link_alias" not in {e.name for e in es})
            got = []
            guard_fastscan.parallel_scan(
                [(str(root), 0, "ctx")],
                descend=lambda p, l, c, e: c,
                visit=lambda e, p, l, c: got.append(e.path))
            ck("遍历不会钻进 junction",
               not any("inside.bin" in p for p in got), str(got[-3:]))
        else:
            ck("junction 跳过（本机无法建联接，跳过该用例）", True)

        # ============ 3. 今日新增：新旧结果一致 ============
        print("[3] 今日新增新旧算法一致性")
        root2 = tmp / "tree2"
        root2.mkdir()
        build_tree(root2)
        day0 = datetime.datetime.combine(datetime.date.today(),
                                         datetime.time.min).timestamp()
        for depth in (1, 2, 3, 6):
            old = ref_today(root2, depth, day0, lambda cur, d: False)
            sc = _Today(tmp, "C")
            sc._root, sc._depth = root2, depth
            res = sc.scan()
            new = sorted((i["path"], i["is_dir"], i["size"])
                         for i in res["items"])
            ck(f"depth={depth} 路径/类型/大小一致", old == new,
               f"old={len(old)} new={len(new)}")
        sc = _Today(tmp, "C")
        sc._root, sc._depth = root2, 6
        ck("2020 年的老文件不进今日清单",
           not any("old.log" in i["path"] for i in sc.scan()["items"]))

        # ============ 4. 存量盘点：新旧桶一致 ============
        print("[4] 存量盘点新旧算法一致性")
        for depth in (1, 2, 3):
            old = ref_legacy(root2, depth, lambda n: False)
            sc = _Legacy(tmp, "C")
            sc._root, sc._depth = root2, depth
            res = sc.scan(0)
            new = {r["path"]: (r["size"], r["count"]) for r in res["rows"]}
            ck(f"depth={depth} 桶与总量一致", old == new,
               f"old={old} new={new}")

        # ============ 5. 仪表盘聚合一致 ============
        print("[5] 增长来源聚合一致性")
        for depth in (1, 2):
            old = ref_aggregate(root2, depth)
            agg = {}
            guard_dashboard._aggregate(root2, depth, agg)
            ck(f"depth={depth} 聚合一致", old == agg, f"old={old} new={agg}")

        # ============ 6. 取消 / 进度 / 并发统计 ============
        print("[6] 取消、进度与统计")
        ev = threading.Event()
        ev.set()
        r = guard_fastscan.parallel_scan([(str(root), 0, "ctx")],
                                         descend=lambda p, l, c, e: c,
                                         visit=lambda e, p, l, c: None,
                                         cancel_evt=ev)
        ck("取消后不扫描条目", r["entries"] == 0, str(r))
        ck("取消标记正确", r["canceled"] is True)
        prog = []
        r2 = guard_fastscan.parallel_scan(
            [(str(root), 0, "ctx")],
            descend=lambda p, l, c, e: c,
            visit=lambda e, p, l, c: None,
            progress_cb=lambda n, cur: prog.append(n), progress_every=1)
        ck("进度回调被触发", len(prog) > 0, str(len(prog)))
        ck("统计有耗时与线程数", r2["elapsed"] >= 0 and r2["workers"] >= 2)
        ck("全部条目被访问", r2["entries"] > 10, str(r2["entries"]))

        # ============ 7. 性能不退化 ============
        print("[7] 引擎速度")
        t0 = time.time()
        cnt = [0]

        def visit(e, p, l, c):
            cnt[0] += 1

        rr = guard_fastscan.parallel_scan(
            [(str(root), 0, "ctx")],
            descend=lambda p, l, c, e: c, visit=visit)
        ck("小目录扫描 < 2 秒", rr["elapsed"] < 2.0, str(rr["elapsed"]))
        print(f"       {cnt[0]} 条 / {rr['elapsed']:.3f}s / {rr['workers']} 线程")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n合计：通过 {PASS} 项，失败 {FAIL} 项")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
