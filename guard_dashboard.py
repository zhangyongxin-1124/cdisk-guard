# -*- coding: utf-8 -*-
"""
guard_dashboard.py — 空间仪表盘与周报
=====================================
回答四个问题：
  1. C 盘现在的健康度怎么样？
  2. 空间是怎么一点点没的（增长来源 Top10）——靠两次快照对比，不猜。
  3. 现在能动的有哪些？（大文件 / 可迁移缓存 / 可卸载软件 / 启动项）
  4. 隔离区里还有没有等你处置的东西。

原则：**只报告，不擅自改任何东西。** 卸载软件、关启动项这类操作，报告里给的是
Windows 自己的设置入口（ms-settings 链接），不是我们自己动手。

用法：
  C盘守卫.exe --dashboard     打印到屏幕并生成 HTML 报告
  C盘守卫.exe --report        生成周报（适合放到任务计划里每周跑一次）
"""

import guard_fastscan          # v6.12 高性能扫描引擎
import os
import sys
import json
import time
import ctypes
import datetime
import subprocess
from pathlib import Path

import guard_paths as gp
import guard_quarantine as gq

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


from guard_appdir import APP_DIR
LOG_DIR = APP_DIR / "guard_log"
SNAPSHOT_FILE = APP_DIR / "space_snapshots.json"


def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def disk_state():
    total = ctypes.c_ulonglong(0)
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW("C:\\", ctypes.byref(free),
                                               ctypes.byref(total), None)
    t, f = total.value or 1, free.value
    used = max(0, t - f)
    pct = used / t * 100
    gb = f / 2 ** 30
    if pct >= 92 or gb < 5:
        level, icon = "危险", "🔴"
    elif pct >= 85 or gb < 10:
        level, icon = "紧张", "🟠"
    elif pct >= 70 or gb < 20:
        level, icon = "偏紧", "🟡"
    else:
        level, icon = "充足", "🟢"
    return {"total": t, "free": f, "used": used, "pct": pct,
            "level": level, "icon": icon}


# ============================================================
# 快照（用于算"增长来源"）
# ============================================================

def _aggregate(root: Path, depth: int, agg: dict, budget=None):
    """把每个文件的大小归到 depth 层以内的祖先头上（v6.12 并发快扫）。"""
    root_str = str(root)

    def descend(parent, level, ctx, entry):
        if level + 1 > depth:
            return None
        try:
            parts = Path(entry.path).relative_to(root).parts
        except ValueError:
            parts = ()
        prefix = "/".join(parts[:depth]) if parts else "."
        return {"prefix": prefix, "depth": depth}

    def visit(entry, parent, level, ctx):
        if entry.is_dir:
            return
        prefix = ctx["prefix"]
        key = f"{root_str}\\{prefix}" if prefix != "." else root_str
        agg[key] = agg.get(key, 0) + entry.size

    guard_fastscan.parallel_scan(
        [(root_str, 0, {"prefix": ".", "depth": depth})],
        descend, visit, stat_dirs=False)


def _is_link(p) -> bool:
    try:
        return bool(os.lstat(p).st_file_attributes & gp.REPARSE)
    except OSError:
        return False


def take_snapshot() -> dict:
    home = Path(os.environ.get("USERPROFILE", ""))
    agg = {}
    if home.exists():
        _aggregate(home, 2, agg)
    pf = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
    if pf.exists():
        _aggregate(pf, 1, agg)
    pf86 = Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"))
    if pf86.exists():
        _aggregate(pf86, 1, agg)
    return {"time": time.time(), "entries": agg}


def load_snapshots() -> dict:
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"history": []}


def save_snapshot(snap: dict):
    data = load_snapshots()
    data["history"].append(snap)
    data["history"] = data["history"][-12:]
    try:
        SNAPSHOT_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def growth_top(cur_snap: dict, history: list, within_days: float = 30.0,
               top: int = 10):
    """跟历史快照比，找出涨得最多的地方。返回 (rows, 对比天数)；
    rows = [(路径, 增长字节, 当前大小)]。没有可比的历史时返回 ([], None)。"""
    now = cur_snap["time"]
    base = None
    for h in sorted(history, key=lambda x: -x["time"]):
        age = (now - h["time"]) / 86400
        if 0.5 <= age <= within_days:
            base = h
            break
    if base is None:
        return [], None
    rows = []
    for k, v in cur_snap["entries"].items():
        old = base["entries"].get(k)
        if old is None:
            continue
        delta = v - old
        if delta > 1 * 2 ** 20:
            rows.append((k, delta, v))
    rows.sort(key=lambda x: -x[1])
    return rows[:top], (now - base["time"]) / 86400


# ============================================================
# 可卸载软件
# ============================================================

UNINSTALL_ROOTS = [
    (winreg.HKEY_LOCAL_MACHINE if winreg else None,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE if winreg else None,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER if winreg else None,
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
]

SKIP_NAME_PAT = ("kb", "update for", "security update", "microsoft visual c++",
                 "hotfix", "visual studio code Insiders".lower(), "redistributable",
                 "windows 更新", ".net")
KNOWN_UPDATE_PAT = ("update", "patch", "driver")


def scan_uninstall(measure_top: int = 20) -> list:
    if winreg is None:
        return []
    rows = []
    seen = set()
    for hive, root in UNINSTALL_ROOTS:
        if hive is None:
            continue
        try:
            h = winreg.OpenKey(hive, root)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(h, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(h, sub) as k:
                    def val(n, default=""):
                        try:
                            return winreg.QueryValueEx(k, n)[0]
                        except OSError:
                            return default
                    if val("SystemComponent") == 1 or val("ParentKeyName"):
                        continue
                    name = val("DisplayName")
                    if not name or name in seen:
                        continue
                    low = name.lower()
                    if any(p in low for p in ("update for windows", "kb2", "kb4", "kb5")):
                        continue
                    size_kb = val("EstimatedSize", 0)
                    loc = val("InstallLocation", "")
                    rows.append({
                        "name": name, "publisher": val("Publisher", ""),
                        "version": val("DisplayVersion", ""),
                        "size": int(size_kb) * 1024 if isinstance(size_kb, int) else 0,
                        "location": loc,
                        "installed": val("InstallDate", ""),
                        "uninstall": val("UninstallString", ""),
                    })
                    seen.add(name)
            except OSError:
                continue
        winreg.CloseKey(h)

    # 注册表里的 EstimatedSize 经常是空的，补测 InstallLocation
    todo = [r for r in rows if not r["size"] and r["location"]
            and os.path.isdir(r["location"])]
    todo.sort(key=lambda r: r["name"])
    for r in todo[:measure_top]:
        try:
            s, _, _ = gp.dir_size(Path(r["location"]), max_items=40000)
            r["size"] = s
            r["size_measured"] = True
        except Exception:
            pass

    rows = [r for r in rows if r["size"] >= 20 * 2 ** 20]
    for r in rows:
        loc = r.get("location", "")
        r["on_c"] = (not loc) or os.path.splitdrive(loc)[0].lower() == "c:"
    rows.sort(key=lambda x: (not x["on_c"], -x["size"]))
    return rows


# ============================================================
# 启动项
# ============================================================

RUN_KEYS = [
    (winreg.HKEY_CURRENT_USER if winreg else None,
     r"Software\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_LOCAL_MACHINE if winreg else None,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER if winreg else None,
     r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE if winreg else None,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
]

# 常见的"顺手就有但你不一定需要开机就跑"的东西（只是标记，不会替你关）
BUNDLE_PAT = ("360", "kingsoft", "wps", "sogou", "baidu", "2345", "2345hao",
              "ludashi", "驱动", "驱动人生", "驱动精灵", "qq游戏", "tgp",
              "完美世界", "steam", "epic", "adobe", "adobeipcbroker",
              "adobe gc invoker", "creativecloud", "creative cloud",
              "cepioct", "ccxprocess", "acrotray", "adobeupdater",
              "searchindexer", "igfx", "sunlogin", "向日葵", "todesk",
              "teamviewer", "anydesk", "bingwallpaper", "pptv", "迅雷",
              "thunder", "qqmusic", "cloudmusic", "腾讯视频", "腾讯管家")


def _lnk_target(p: Path) -> str:
    try:
        ps = ('$s=(New-Object -ComObject WScript.Shell).CreateShortcut("'
              + str(p) + '"); Write-Output $s.TargetPath')
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=15)
        return r.stdout.decode("utf-8", "replace").strip().splitlines()[0] if r.stdout else ""
    except Exception:
        return ""


def scan_startup() -> list:
    rows = []
    if winreg is not None:
        for hive, root in RUN_KEYS:
            if hive is None:
                continue
            scope = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
            try:
                h = winreg.OpenKey(hive, root)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(h, i)
                except OSError:
                    break
                i += 1
                rows.append({"name": name, "command": str(val), "scope": scope,
                             "where": root.split("\\")[-1]})
            winreg.CloseKey(h)
    startup_dirs = [
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"),
    ]
    for sd in startup_dirs:
        if not sd.exists():
            continue
        for f in sd.glob("*.lnk"):
            rows.append({"name": f.name[:-4], "command": _lnk_target(f) or str(f),
                         "scope": "启动文件夹", "where": str(sd)})
    for r in rows:
        low = (r["name"] + " " + r["command"]).lower()
        r["suspicious"] = any(p in low for p in BUNDLE_PAT)
    rows.sort(key=lambda x: (not x["suspicious"], x["name"].lower()))
    return rows


# ============================================================
# 汇总
# ============================================================

def build_report(scan_big: bool = True, uninstall: bool = True,
                 startup: bool = True) -> dict:
    home = Path(os.environ.get("USERPROFILE", ""))
    rep = {"time": datetime.datetime.now(), "disk": disk_state()}
    rep["app_targets"] = [
        {"label": t["label"], "path": str(p), "size": size, "files": cnt,
         "category": t["category"], "action": t["action"], "note": t.get("note", "")}
        for t, p, size, cnt in gp.scan_app_targets(home, min_mb=50)
    ]
    rep["system_targets"] = [
        {"label": t["label"], "path": str(p), "size": size, "note": t.get("note", "")}
        for t, p, size, cnt in gp.scan_system_targets(Path("C:\\"), min_mb=200)
    ]
    if scan_big:
        rep["big_files"] = [(s, p) for s, p in
                            gp.scan_big_files(home, min_mb=200.0, top=25)]
    else:
        rep["big_files"] = []
    if uninstall:
        rep["uninstall"] = scan_uninstall()
    else:
        rep["uninstall"] = []
    if startup:
        rep["startup"] = scan_startup()
    else:
        rep["startup"] = []
    q = gq.Quarantine()
    rep["quarantine"] = {"root": str(q.root), "rows": q.list(),
                         "size": q.total_size(),
                         "expiring": q.expired_soon()}
    return rep


# ============================================================
# 控制台输出
# ============================================================

def console_report(rep: dict, growth=None, growth_days: float = 0):
    st = rep["disk"]
    line = "═" * 68
    print("\n" + line)
    print("  C 盘仪表盘   " + rep["time"].strftime("%Y-%m-%d %H:%M"))
    print(line)
    print(f"  {st['icon']} {st['level']}   剩余 {human_size(st['free'])}"
          f" / 已用 {human_size(st['used'])}（{st['pct']:.1f}%）")

    if growth_days:
        print(f"\n【谁在吃掉你的 C 盘】对比 {growth_days:.0f} 天前的快照")
        if not growth:
            print("  这段时间没有出现明显的增长点（单个位置涨幅都不到 1 MB）。")
        for path, delta, cur in growth:
            print(f"   +{human_size(delta):>8}  {path}")
            print(f"     现在共 {human_size(cur)}")
    else:
        print("\n【谁在吃掉你的 C 盘】第一次扫描，先记录一份基线，"
              "下次就有得比了（隔几天再跑一次更准）")

    if rep["big_files"]:
        print("\n【大文件 Top%d】" % len(rep["big_files"]))
        for s, p in rep["big_files"][:12]:
            print(f"   {human_size(s):>9}  {p}")

    if rep["app_targets"]:
        print("\n【可以搬走 / 可以清理】")
        for a in rep["app_targets"]:
            tag = "可搬家" if a["action"] == "migrate" else "可清理"
            print(f"   [{tag}] {human_size(a['size']):>9}  {a['label']}")
            print(f"             {a['path']}")

    if rep["system_targets"]:
        print("\n【系统级大户】（需要管理员权限才能动，这里只提示）")
        for a in rep["system_targets"]:
            print(f"   {human_size(a['size']):>9}  {a['label']}   {a['note']}")

    if rep["uninstall"]:
        print("\n【占地方的软件 Top%d】" % min(10, len(rep["uninstall"])))
        for u in rep["uninstall"][:10]:
            print(f"   {human_size(u['size']):>9}  {u['name'][:44]}")
            if u.get("location"):
                print(f"             {u['location']}")

    if rep["startup"]:
        susp = [s for s in rep["startup"] if s["suspicious"]]
        print(f"\n【开机启动项】共 {len(rep['startup'])} 个"
              f"，其中 {len(susp)} 个看着像「顺装组件」")
        for s in susp[:8]:
            print(f"   · {s['name']}  ({s['scope']})")
            print(f"     {s['command'][:80]}")

    q = rep["quarantine"]
    if q["rows"]:
        print(f"\n【隔离区】{len(q['rows'])} 项 / {human_size(q['size'])}"
              f"（位置：{q['root']}）")
        if q["expiring"]:
            print(f"   ⚠️ {len(q['expiring'])} 项快到期了，"
                  f"跑 C盘守卫.exe --quarantine 决定要还是不要")
    print(line)


# ============================================================
# HTML 报告
# ============================================================

CSS = """
body{font-family:"Microsoft YaHei","PingFang SC",-apple-system,Segoe UI,sans-serif;
 background:#f5f6f8;color:#1f2328;margin:0;padding:28px 0;line-height:1.65}
.box{max-width:960px;margin:0 auto;background:#fff;border-radius:14px;
 padding:30px 38px;box-shadow:0 2px 14px rgba(0,0,0,.07)}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:30px 0 12px;padding-left:10px;border-left:4px solid #2b6cb0}
.sub{color:#6b7280;font-size:13px;margin-bottom:22px}
.card{display:flex;gap:18px;flex-wrap:wrap;margin:18px 0}
.stat{flex:1;min-width:150px;background:#f8fafc;border:1px solid #e5e7eb;
 border-radius:10px;padding:14px 16px}
.stat b{display:block;font-size:20px;margin-bottom:2px}
.stat span{font-size:12px;color:#6b7280}
.bar{height:10px;background:#e5e7eb;border-radius:6px;overflow:hidden;margin:6px 0 4px}
.bar i{display:block;height:100%;border-radius:6px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef0f3;vertical-align:top}
th{color:#6b7280;font-weight:600;background:#fafbfc}
td.r{text-align:right;white-space:nowrap}
.up{color:#c0392b;font-weight:600}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;
 background:#eef2ff;color:#4338ca;margin-left:6px}
.tag.warn{background:#fef3c7;color:#92400e}
.tag.ok{background:#dcfce7;color:#166534}
.muted{color:#9ca3af;font-size:12px}
path,.p{font-family:Consolas,monospace;font-size:12px;color:#6b7280}
ol{padding-left:22px}li{margin:4px 0}
.warnbox{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;
 padding:14px 16px;margin:16px 0;font-size:13px}
footer{margin-top:26px;color:#9ca3af;font-size:12px;text-align:center}
"""


def render_html(rep: dict, out: Path, growth=None, growth_days: float = 0,
                weekly: bool = False):
    st = rep["disk"]
    color = {"充足": "#16a34a", "偏紧": "#ca8a04", "紧张": "#ea580c",
             "危险": "#dc2626"}[st["level"]]
    h = []
    a = h.append
    a(f'<h1>{"C 盘周报" if weekly else "C 盘体检报告"}</h1>')
    a(f'<div class="sub">{rep["time"].strftime("%Y-%m-%d %H:%M")} · '
      f'由 C 盘守卫生成</div>')
    a('<div class="card">')
    a(f'<div class="stat"><b style="color:{color}">{st["icon"]} {st["level"]}</b>'
      f'<span>C 盘健康度</span></div>')
    a(f'<div class="stat"><b>{human_size(st["free"])}</b><span>可用空间</span></div>')
    a(f'<div class="stat"><b>{human_size(st["used"])}</b>'
      f'<span>已用 {st["pct"]:.1f}%</span></div>')
    a(f'<div class="stat"><b>{human_size(st["total"])}</b><span>C 盘总容量</span></div>')
    a('</div>')
    a(f'<div class="bar"><i style="width:{st["pct"]:.1f}%;background:{color}"></i></div>')
    a(f'<div class="muted">已用 {human_size(st["used"])} / {human_size(st["total"])}</div>')

    if growth:
        a(f'<h2>谁在吃掉你的 C 盘（对比 {growth_days:.0f} 天前）</h2>')
        a('<table><tr><th>增长</th><th>位置</th><th class="r">现在共</th></tr>')
        for path, delta, cur in growth:
            a(f'<tr><td class="up">+{human_size(delta)}</td>'
              f'<td><span class="p">{path}</span></td>'
              f'<td class="r">{human_size(cur)}</td></tr>')
        a('</table>')

    if rep["app_targets"]:
        a('<h2>可以搬走 / 可以清理</h2>')
        a('<table><tr><th class="r">占用</th><th>项目</th><th>建议</th></tr>')
        for t in rep["app_targets"]:
            mig = t["action"] == "migrate"
            a(f'<tr><td class="r">{human_size(t["size"])}</td>'
              f'<td>{t["label"]}'
              f'<span class="tag {"ok" if mig else "warn"}">'
              f'{"搬家（可撤回）" if mig else "清理（不可撤回）"}</span>'
              f'<div class="p">{t["path"]}</div>'
              + (f'<div class="muted">{t["note"]}</div>' if t.get("note") else "")
              + '</td><td></td></tr>')
        a('</table>')
        a('<div class="warnbox">搬家走的是「搬到别的盘 + 原位留目录联接」，'
          '软件完全无感，随时可以 <code>--undo</code> 撤回；'
          '清理是永久删除，所以默认不替你做。</div>')

    if rep["big_files"]:
        a('<h2>大文件</h2><table><tr><th class="r">大小</th><th>路径</th></tr>')
        for s, p in rep["big_files"]:
            a(f'<tr><td class="r">{human_size(s)}</td><td><span class="p">{p}</span></td></tr>')
        a('</table>')

    if rep["uninstall"]:
        a('<h2>占地方的软件</h2>')
        a('<table><tr><th class="r">占用</th><th>软件</th><th>安装位置</th></tr>')
        for u in rep["uninstall"][:18]:
            a(f'<tr><td class="r">{human_size(u["size"])}</td>'
              f'<td>{u["name"][:60]}'
              f'<div class="muted">{u.get("publisher","")[:40]}'
              f'{" · 安装日期 " + str(u.get("installed","")) if u.get("installed") else ""}</div></td>'
              f'<td><span class="p">{u.get("location","")}</span></td></tr>')
        a('</table>')
        a('<div class="muted">卸载请走 Windows 自己的「设置 → 应用 → 已安装的应用」，'
          '守卫不会替你卸载。</div>')

    if rep["startup"]:
        susp = [s for s in rep["startup"] if s["suspicious"]]
        a('<h2>开机启动项</h2>')
        a(f'<div class="muted">共 {len(rep["startup"])} 个开机自启，'
          f'其中 {len(susp)} 个看着像「顺装组件」。'
          f'要改就用系统自己的入口：<b>设置 → 应用 → 启动</b>，守卫不替你关。</div>')
        a('<table><tr><th>名称</th><th>位置</th><th>命令</th></tr>')
        for s in rep["startup"]:
            tag = '<span class="tag warn">顺装组件</span>' if s["suspicious"] else ""
            a(f'<tr><td>{s["name"][:40]}{tag}</td><td>{s["scope"]}</td>'
              f'<td><span class="p">{s["command"][:100]}</span></td></tr>')
        a('</table>')

    q = rep["quarantine"]
    if q["rows"]:
        a('<h2>隔离区</h2>')
        a(f'<div class="muted">{len(q["rows"])} 项 / {human_size(q["size"])}'
          f'　·　这里是你删过的东西，清空回收站也不会丢；'
          f'跑 <code>C盘守卫.exe --quarantine</code> 决定要还是不要。</div>')
        a('<table><tr><th class="r">大小</th><th>原位置</th><th class="r">删除时间</th></tr>')
        for e in q["rows"][:20]:
            when = datetime.datetime.fromtimestamp(e.get("deleted", 0)).strftime("%m-%d %H:%M")
            a(f'<tr><td class="r">{human_size(e["size"])}</td>'
              f'<td><span class="p">{e["orig"]}</span></td>'
              f'<td class="r">{when}</td></tr>')
        a('</table>')

    a('<footer>本报告只做显示，没有改动任何设置或文件。<br>'
      '真正要让 C 盘瘦下来，选上面的项目跑「C盘守卫.exe --wizard」即可。</footer>')
    html = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>C 盘体检报告</title><style>' + CSS + '</style></head>'
            '<body><div class="box">' + "".join(h) + '</div></body></html>')
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# ============================================================
# 入口
# ============================================================

def dashboard(open_html: bool = False) -> Path:
    print("\n正在扫描 C 盘，第一次可能要一分钟…")
    rep = build_report()
    snap = take_snapshot()
    data = load_snapshots()
    rows, days = growth_top(snap, data["history"])
    console_report(rep, rows, days)
    save_snapshot(snap)
    print(f"\n  （已记录这次快照，累计 {len(data['history'])+1} 次，"
          f"多跑几次对比才准）")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out = LOG_DIR / f"C盘体检报告_{stamp}.html"
    render_html(rep, out, rows, days)
    print(f"\n  HTML 报告：{out}")
    if open_html:
        try:
            os.startfile(str(out))
        except Exception:
            pass
    return out


def weekly_report() -> Path:
    snap = take_snapshot()
    data = load_snapshots()
    rows, days = growth_top(snap, data["history"], within_days=30.0)
    rep = build_report(scan_big=True)
    save_snapshot(snap)
    yw = datetime.datetime.now().isocalendar()
    out = LOG_DIR / f"C盘周报_{yw[0]}-W{yw[1]:02d}.html"
    render_html(rep, out, rows, days, weekly=True)
    return out


if __name__ == "__main__":
    dashboard(open_html=True)
