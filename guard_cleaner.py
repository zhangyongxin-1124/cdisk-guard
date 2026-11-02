# -*- coding: utf-8 -*-
"""
guard_cleaner.py — 日志 / 崩溃转储 / 临时文件 自动清理
=====================================================
解决「什么都没干，C 盘也在一点点变少」：
软件日志、崩溃转储、更新残留、Temp 临时文件会一直长大，
这类东西**不该搬走，直接删掉最合理**。

设计原则（面向小白 + 绝不误删）：
  1. 只碰三类东西：日志文件、崩溃转储(.dmp)、Temp 临时文件
  2. 只删「N 天没动过」的，或「大得离谱且当前没在写」的
  3. 正在被写入的文件（10 分钟内动过）一律跳过
  4. 系统目录 / Program Files / 本软件自身目录 永不触碰
  5. 默认开启「清理前先问你」，可以关掉变全自动
"""

import os
import threading
import re
import json
import time
from pathlib import Path
from datetime import datetime

MB = 1024 * 1024
GB = 1024 ** 3

# 本软件数据目录（与主程序同目录；frozen 时由主程序注入）
import guard_fastscan          # v6.13 并发快扫引擎
from guard_appdir import APP_DIR
STATE_PATH = APP_DIR / "cleaner_state.json"

# ============================================================
# 默认配置
# ============================================================

DEFAULT_CFG = {
    "启用": False,            # 自动清理总开关（默认关，用户自己开）
    "日志保留天数": 3,         # 超过这么多天没动过的日志/临时文件 → 可清
    "超大日志MB": 50,         # 单个日志大到这个数、且当前没在写 → 可清
    "自动周期天": 7,          # 每隔多少天自动跑一次
    "清理前询问": True,       # 自动清理前弹窗让你确认（推荐开）
    "最小清理量MB": 20,       # 可清量不足这个数就不打扰你
    "清崩溃转储": True,       # 是否清 .dmp 崩溃转储
    "清临时文件": True,       # 是否清 Temp 里的旧文件
    "扫描根目录": [],         # 留空 = 用默认（Temp + AppData + Windows\Temp）
}

# ============================================================
# 默认扫描根目录
# ============================================================

def default_roots():
    """默认扫描位置：临时目录 + AppData（软件日志基本都在这）。"""
    out = []
    env = {
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
    }
    for k in ("TEMP", "LOCALAPPDATA", "APPDATA"):
        v = env.get(k, "")
        if v and os.path.isdir(v):
            out.append(v)
    wt = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
    if os.path.isdir(wt):
        out.append(wt)
    # 去重，并去掉「已被更上级目录包含」的根（否则会重复扫描同一批文件）
    uniq = []
    for p in sorted(out, key=len):
        pl = p.lower().rstrip("\\/")
        if any(pl == u.lower().rstrip("\\/") for u in uniq):
            continue
        if any(pl.startswith(u.lower().rstrip("\\/") + "\\") for u in uniq):
            continue                       # 新根在某个已有根之下 → 丢弃
        uniq = [u for u in uniq
                if not u.lower().rstrip("\\/").startswith(pl + "\\")]  # 反之替换
        uniq.append(p)
    return uniq


# ============================================================
# 永不触碰：系统目录 / 程序目录 / 本软件自身
# ============================================================

_EXCLUDE_KEYWORDS = (
    "\\windows\\", "\\system32\\", "\\syswow64\\", "\\winsxs\\",
    "\\program files\\", "\\programdata\\",
    "\\$recycle.bin\\", "\\system volume information\\",
    "\\.workbuddy\\", "\\.codebuddy\\", "\\.workbuddy-key-fallback\\",
    "\\guard_log\\", "\\node_modules\\", "\\.git\\", "\\.svn\\",
    "\\appdata\\local\\microsoft\\windows\\",
    "\\windowsapp\\", "\\packages\\",
)

# 这些扩展名/名称模式才算「日志」
_LOG_EXT = {".log", ".old", ".etl", ".out", ".err", ".trace"}
_CRASH_EXT = {".dmp", ".mdmp", ".hdmp", ".minidump"}
# 只在「像日志目录」里才认的扩展名
_SOFT_EXT = {".txt", ".csv", ".json", ".xml", ".gz", ".log.zip"}
_LOG_DIR_SEG = {"log", "logs", "logging", "diagnostics", "diagnostic",
                "traces", "trace", "crash", "crashes", "dumps", "crashpad",
                "reports", "telemetry", "sdk_storage"}

_ROTATED_RE = re.compile(r"\.log([.\-_]\d+|\.\d{4}[-_]?\d{2}[-_]?\d{2})$", re.I)


def _is_excluded(path_l: str) -> bool:
    for k in _EXCLUDE_KEYWORDS:
        if k in path_l:
            return True
    try:
        pl = Path(path_l)
        ap = str(APP_DIR).lower().rstrip("\\/")
        if ap and path_l.startswith(ap + "\\"):
            return True      # 不清理本软件自己的数据
    except Exception:
        pass
    return False


def is_temp_root(p: str) -> bool:
    """判断一个扫描根是否真的是「临时目录」。

    不能只看路径里有没有叫 temp 的段——很多软件把自己的缓存目录也命名为
    Temp，那样会把用户的重要文件误判成临时文件。
    """
    if not p:
        return False
    pl = str(p).lower().rstrip("\\/")
    base = os.path.basename(pl)
    if base in ("temp", "tmp"):
        return True
    for k in ("TEMP", "TMP"):
        v = os.environ.get(k, "").lower().rstrip("\\/")
        if v and (pl == v or pl.startswith(v + "\\")):
            return True
    return False


def classify(path: str, temp_prefixes=()) -> str:
    """判断一个文件属于哪一类可清目标；返回 '' 表示不算。

    返回：'日志' / '崩溃转储' / '临时文件' / ''
    temp_prefixes：真正的临时目录前缀（小写，不带尾斜杠）
    """
    pl = path.lower()
    name = os.path.basename(pl)
    ext = os.path.splitext(name)[1]

    if ext in _CRASH_EXT:
        return "崩溃转储"
    if ext in _LOG_EXT or _ROTATED_RE.search(name):
        return "日志"

    segs = set(s for s in re.split(r"[\\/]+", pl) if s)
    in_log_dir = bool(segs & _LOG_DIR_SEG)
    if in_log_dir and (ext in _SOFT_EXT or ext == ""):
        return "日志"

    # 只在「真正的临时目录」之下的文件才算临时文件
    for pre in temp_prefixes:
        if pl.startswith(pre + "\\") or pl == pre:
            return "临时文件"
    return ""


# ============================================================
# 扫描
# ============================================================

def _walk(roots, budget_s=25.0, max_files=300000):
    """迭代式遍历，带时间预算与文件数上限，避免卡死。"""
    deadline = time.time() + budget_s
    seen = 0
    stack = [(r, 0) for r in roots]
    while stack:
        cur, depth = stack.pop()
        if time.time() > deadline or seen > max_files:
            return
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if time.time() > deadline or seen > max_files:
                        return
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < 8:
                                stack.append((e.path, depth + 1))
                        else:
                            seen += 1
                            yield e
                    except OSError:
                        continue
        except OSError:
            continue


def scan(cfg=None, roots=None, budget_s=25.0, max_files=300000,
         max_items=2000, progress_cb=None):
    """扫描可清理目标。

    返回 dict：
      items   —— 每一项 {path,size,mtime,age_days,cat,reason}
      total   —— 可释放字节数
      scanned —— 实际扫过的文件数
      elapsed —— 耗时秒
      skipped_active —— 因为「正在写入」而跳过的数量
      truncated —— 是否因为上限被截断
    """
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    roots = roots or (cfg.get("扫描根目录") or default_roots())
    roots = [r for r in roots if r and os.path.isdir(r)]

    keep_days = max(0, int(cfg.get("日志保留天数", 3)))
    big_mb = max(1, int(cfg.get("超大日志MB", 50)))
    do_crash = bool(cfg.get("清崩溃转储", True))
    do_temp = bool(cfg.get("清临时文件", True))

    now = time.time()
    temp_prefixes = tuple(r.lower().rstrip("\\/") for r in roots if is_temp_root(r))
    items = []
    have = set()          # 路径去重，杜绝同一文件出现两次
    total = 0
    total_safe = 0        # 不含「正在被写入」的部分
    scanned = 0
    skipped_active = 0
    truncated = False
    t0 = time.time()

    # ---- v6.13：改走并发快扫引擎（原单线程 _walk 每文件 2 次 time.time，
    # 30 万文件时把 GIL 吃满、界面跟着卡）。预算/上限检查提到「每层」粒度，
    # 触发上限立即全局取消，不再逐条空转。 ----
    cancel = threading.Event()
    deadline = t0 + budget_s

    def descend(parent, level, ctx, entry):
        return None if level >= 8 else ctx     # 与 _walk 的 depth<8 一致

    def visit(entry, parent, level, ctx):
        nonlocal scanned, skipped_active, total, total_safe, truncated
        if cancel.is_set():
            return
        if scanned >= max_files or len(items) >= max_items:
            truncated = truncated or len(items) >= max_items
            cancel.set()
            return
        if time.time() > deadline:
            cancel.set()
            return
        scanned += 1
        size = entry.size
        if size <= 0 or entry.is_dir:
            return
        p = entry.path
        pl = p.lower()
        if pl in have:
            return
        have.add(pl)
        if _is_excluded(pl):
            return
        cat = classify(p, temp_prefixes)
        if not cat:
            return
        if cat == "崩溃转储" and not do_crash:
            return
        if cat == "临时文件" and not do_temp:
            return

        mtime = entry.mtime
        age_days = (now - mtime) / 86400.0
        idle_min = (now - mtime) / 60.0
        reason = ""
        active = False          # 正在被软件写入

        if age_days >= keep_days:
            reason = f"{cat} · {int(age_days)} 天没动过"
        elif size >= big_mb * MB:
            active = idle_min < 10
            reason = (f"{cat} · 超大（{size / MB:.0f} MB）" +
                      ("，正在被写入" if active else ""))
            if active:
                skipped_active += 1     # 列出来给你看，但不会自动删
        else:
            return

        items.append({
            "path": p,
            "size": size,
            "mtime": mtime,
            "age_days": age_days,
            "cat": cat,
            "reason": reason,
            "active": active,
        })
        total += size
        if not active:
            total_safe += size
        if len(items) >= max_items:
            truncated = True
            cancel.set()

    guard_fastscan.parallel_scan(
        [(r, 0, 0) for r in roots],       # ctx 用 0：None 表示“不下钻”
        descend, visit, cancel_evt=cancel,
        progress_cb=(lambda n, cur: progress_cb(scanned, cur))
                    if progress_cb else None,
        progress_every=400)

    items.sort(key=lambda x: -x["size"])
    return {
        "items": items,
        "total": total,
        "total_safe": total_safe,
        "scanned": scanned,
        "elapsed": time.time() - t0,
        "skipped_active": skipped_active,
        "truncated": truncated,
        "roots": roots,
    }


def aggregate(items, max_rows=300):
    """按目录分组，方便小白一眼看懂（与「今日新增」一致的展示方式）。"""
    groups = {}
    for it in items:
        d = os.path.dirname(it["path"])
        g = groups.get(d)
        if g is None:
            g = groups[d] = {"dir": d, "size": 0, "safe_size": 0, "active": False,
                             "count": 0, "last_ts": 0, "cats": {}, "items": []}
        g["size"] += it["size"]
        if not it.get("active"):
            g["safe_size"] = g.get("safe_size", 0) + it["size"]
        g["active"] = g.get("active", False) or bool(it.get("active"))
        g["count"] += 1
        g["cats"][it["cat"]] = g["cats"].get(it["cat"], 0) + 1
        g["last_ts"] = max(g["last_ts"], it["mtime"])
        g["items"].append(it)
    rows = sorted(groups.values(), key=lambda x: -x["size"])
    for g in rows:
        g["items"].sort(key=lambda x: -x["size"])
        g["cat_text"] = "、".join(f"{k}×{v}" for k, v in
                                 sorted(g["cats"].items(), key=lambda kv: -kv[1]))
    return rows[:max_rows]


# ============================================================
# 清理
# ============================================================

def clean(items):
    """删除给定文件（永久删除，不再占空间）。

    返回 dict：ok[(path,size)] / fail[(path,err)] / freed
    """
    ok, fail, freed = [], [], 0
    for it in items:
        p = it["path"] if isinstance(it, dict) else str(it)
        try:
            size = os.path.getsize(p)
        except OSError:
            try:
                size = it["size"]
            except Exception:
                size = 0
        try:
            os.remove(p)
            ok.append((p, size))
            freed += size
            continue
        except OSError as e1:
            # 只读属性导致失败 → 去掉只读再试一次
            try:
                os.chmod(p, 0o600)
                os.remove(p)
                ok.append((p, size))
                freed += size
                continue
            except OSError as e2:
                fail.append((p, str(e2) or str(e1)))
        except Exception as e:
            fail.append((p, str(e)))
    return {"ok": ok, "fail": fail, "freed": freed}


# ============================================================
# 状态：上次清理时间 + 历史
# ============================================================

def load_state():
    d = {"上次清理": 0, "历史": []}
    try:
        p = Path(STATE_PATH)
        if p.exists():
            u = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(u, dict):
                d.update(u)
    except Exception:
        pass
    return d


def save_state(st):
    try:
        st["历史"] = (st.get("历史") or [])[-200:]
        Path(STATE_PATH).write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except Exception:
        pass


def mark_checked():
    """登记「已经跑过一轮检查」（无论最终有没有清），避免反复扫描。"""
    st = load_state()
    st["上次清理"] = time.time()
    save_state(st)
    return st


def mark_cleaned(freed, count):
    st = load_state()
    st["上次清理"] = time.time()
    st.setdefault("历史", []).append({
        "ts": time.time(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "freed": int(freed),
        "count": int(count),
    })
    save_state(st)
    return st


def due(cfg):
    """是否到了自动清理的时间。"""
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    if not cfg.get("启用"):
        return False, 0
    period = max(1, int(cfg.get("自动周期天", 7)))
    last = float(load_state().get("上次清理") or 0)
    diff = time.time() - last
    return diff >= period * 86400, diff


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"
