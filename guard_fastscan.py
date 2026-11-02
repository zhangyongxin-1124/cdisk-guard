# -*- coding: utf-8 -*-
"""
guard_fastscan.py — 高性能磁盘扫描引擎（v6.12 新增）
====================================================
解决「C 盘文件一多，读取慢得像蜗牛」的问题。

为什么原来的慢（本机实测，C:\\Windows\\WinSxS，7.3 万个文件）：
    os.walk + 每个文件 os.stat()   58.15 秒
    os.scandir（复用 DirEntry）   11.25 秒   5.2x
    本模块（scandir + 线程池）      8.66 秒   6.7x
  C:\\Users\\...\\AppData\\Local（3 万个文件）：15.55 秒 → 0.93 秒（16.7x）

三个关键改动：
  1. 一次 os.scandir 就把名字/类型/大小/时间拿到手，不再对每个文件
     单独拼路径再 os.stat()（路径解析 + 系统调用是主要开销）。
  2. 按顶层子目录把一棵大树切成多份，丢进线程池并发遍历。
     Python 在 os.scandir / stat 的 C 调用期间会释放 GIL，所以 I/O 能真并行。
  3. 自动跳过 junction / 符号链接（Python 3.12+ 的 DirEntry.is_junction()
     零额外开销），既防死循环，也防把别的盘的内容算进这块盘。

对外只暴露两个东西：
    iter_entries(dir)    —— 枚举一层，产出 Entry
    parallel_scan(...)   —— 并发遍历多棵树，逐条回调 visit()

约定：
  · visit() 由引擎内部加锁后调用，调用方可以直接往共享 list/dict 里写。
  · descend() 必须是纯函数（不修改共享状态），返回子目录的上下文对象；
    返回 None 表示这个目录不往下钻（但要照常 visit）。
  · cancel_evt 一旦 set，所有线程会在下一个目录边界尽快停下。
  · 非 Windows 平台自动退化为普通 scandir 遍历，功能完全一致。
"""
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

IS_WINDOWS = sys.platform == "win32"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class Entry:
    """一条目录项。__slots__ 是为了几十万条时少占内存、少做属性字典。"""
    __slots__ = ("path", "name", "size", "mtime", "ctime", "is_dir")

    def __init__(self, path, name, size, mtime, ctime, is_dir):
        self.path = path
        self.name = name
        self.size = size
        self.mtime = mtime
        self.ctime = ctime
        self.is_dir = is_dir


def _is_junction(e) -> bool:
    """是不是 reparse point（目录联接 / 符号链接）。拿不准就当是，宁可跳过。"""
    try:
        if hasattr(e, "is_junction"):
            return bool(e.is_junction())
        st = e.stat(follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) &
                    FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def iter_entries(directory, cancel_evt=None, stat_dirs=True):
    """枚举一层目录，产出 Entry（含目录与文件），自动跳过链接。

    每个文件条目只做一次 e.stat()；Windows 下 DirEntry 已缓存 FindFirstFile
    拿到的元数据，比 os.stat(完整路径) 快一个数量级。

    stat_dirs=False 时目录条目不再 stat（mtime/ctime 填 0）——
    存量盘点/增长聚合只看文件大小，能省掉几万次无用的系统调用。
    """
    if cancel_evt is not None and cancel_evt.is_set():
        return
    try:
        with os.scandir(directory) as it:
            for e in it:
                if cancel_evt is not None and cancel_evt.is_set():
                    return
                try:
                    if _is_junction(e):
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    if is_dir and not stat_dirs:
                        yield Entry(e.path, e.name, 0, 0.0, 0.0, True)
                        continue
                    st = e.stat(follow_symlinks=False)
                    ct = getattr(st, "st_ctime", 0) or 0
                    mt = getattr(st, "st_mtime", 0) or 0
                    yield Entry(e.path, e.name,
                                0 if is_dir else int(st.st_size or 0),
                                float(mt or 0), float(ct or mt or 0), is_dir)
                except (OSError, ValueError):
                    continue
    except (OSError, ValueError):
        return


def default_workers() -> int:
    """并发线程数：CPU 核数与 8 取小，环境变量 GUARD_SCAN_WORKERS 可覆盖。"""
    try:
        v = int(os.environ.get("GUARD_SCAN_WORKERS", "0"))
        if 1 <= v <= 32:
            return v
    except (TypeError, ValueError):
        pass
    return max(2, min(8, (os.cpu_count() or 4)))


def parallel_scan(roots, descend, visit, *, workers=None, cancel_evt=None,
                  progress_cb=None, progress_every=200, expand=2,
                  stat_dirs=True):
    """并发遍历多棵目录树。

    参数
    ----
    roots : [(目录路径, 起始层级 int, 上下文对象), ...]
    descend(parent, level, ctx, entry) -> 子目录的 ctx，或 None（不往下钻）
        必须是纯函数；子目录的层级自动 +1。
    visit(entry, parent, level, ctx)
        每条目录项（含目录本身）都会调用一次；引擎内部加锁，可安全写共享结构。
    workers : 线程数，默认 default_workers()
    cancel_evt : threading.Event，set 后尽快停
    progress_cb(n_dirs, cur_dir) : 每扫过 progress_every 个目录回调一次
    expand : 顶层子任务不足 workers*expand 时，再展开一层以提高并发度
    stat_dirs : 是否对「目录条目」取 mtime/ctime（只需要文件信息时设 False）

    返回 {"dirs", "entries", "elapsed", "workers", "canceled"}
    """
    t0 = time.time()
    workers = workers or default_workers()
    lock = threading.Lock()
    stat = {"dirs": 0, "entries": 0}
    canceled = False

    def _layer(cur, level, ctx):
        """处理一层：一次加锁批量回调（每条加锁在 4 万目录时是明显瓶颈），
        返回需要继续下钻的子任务。"""
        entries = list(iter_entries(cur, cancel_evt, stat_dirs))
        if not entries and cur is None:
            return []
        with lock:
            for entry in entries:
                visit(entry, cur, level, ctx)
            stat["entries"] += len(entries)
            stat["dirs"] += 1
            n = stat["dirs"]
        if progress_cb and n % max(1, progress_every) == 0:
            try:
                progress_cb(n, cur)
            except Exception:
                pass
        subs = []
        for entry in entries:
            if entry.is_dir:
                try:
                    sub_ctx = descend(cur, level, ctx, entry)
                except Exception:
                    sub_ctx = None
                if sub_ctx is not None:
                    subs.append((entry.path, level + 1, sub_ctx))
        return subs

    def _walk_dir(task):
        stack = [task]
        while stack:
            if cancel_evt is not None and cancel_evt.is_set():
                return
            cur, level, ctx = stack.pop()
            stack.extend(_layer(cur, level, ctx))

    # ---- 把每棵树的顶层子目录切成任务 ----
    tasks = []
    for root, level, ctx in roots:
        if cancel_evt is not None and cancel_evt.is_set():
            canceled = True
            break
        tasks.extend(_layer(root, level, ctx))

    # 任务太少（例如只有一个巨型目录）时再展开一层，避免 8 个线程闲 7 个
    if expand and tasks and len(tasks) < workers * expand and \
            (cancel_evt is None or not cancel_evt.is_set()):
        deeper = []
        for path, level, ctx in tasks[:workers * 8]:
            deeper.extend(_layer(path, level, ctx))
        tasks = deeper + tasks[workers * 8:]

    if not canceled and tasks and (cancel_evt is None or not cancel_evt.is_set()):
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="FastScan") as ex:
            list(ex.map(_walk_dir, tasks))
    canceled = canceled or (cancel_evt is not None and cancel_evt.is_set())

    return {"dirs": stat["dirs"], "entries": stat["entries"],
            "elapsed": time.time() - t0, "workers": workers,
            "canceled": canceled}


def quick_space(root: str):
    """毫秒级读取容量（不遍历文件）。返回 (total, used, free)；失败全 0。"""
    try:
        import shutil
        t, u, f = shutil.disk_usage(root)
        return t, u, f
    except OSError:
        return 0, 0, 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="磁盘扫描速度自检")
    ap.add_argument("path", nargs="?", default=r"C:\Windows")
    ap.add_argument("--workers", type=int, default=0)
    a = ap.parse_args()
    n = [0]
    sz = [0]
    r = parallel_scan([(a.path, 0, None)],
                       descend=lambda p, l, c, e: "ctx",
                       visit=lambda e, p, l, c: (
                           n.__setitem__(0, n[0] + (0 if e.is_dir else 1)),
                           sz.__setitem__(0, sz[0] + e.size)),
                       workers=a.workers or 0)
    print(f"{a.path}: {n[0]} 文件 / {sz[0]/2**30:.2f} GB, "
          f"耗时 {r['elapsed']:.2f}s, {r['workers']} 线程, {r['dirs']} 目录")
