# -*- coding: utf-8 -*-
"""
guard_migrate.py — 软件迁移向导（v6.14 新增）
============================================
把「整个软件目录搬到别的盘」这件事做完整：不只是复制文件，还要把
搬家后会失效的四类引用一起处理好 ——

  1. 快捷方式（.lnk）：开始菜单 / 桌面里的目标路径、起始位置、图标路径
  2. 注册表：InstallLocation、Uninstall、Run 自启动等字符串值里的旧路径
  3. Windows 服务：ImagePath 指向旧目录 → **只提示、不自动改**（风险最高）
  4. 原位置：默认留 junction 透明链接，软件按老路径读写完全无感

风险分级（服务型软件先给等级，再让用户决定）：
  · 高：有服务 / 驱动(.sys) / 系统保护目录 / 正在运行 → 建议不要搬，
        或改用「卸载后重装到目标盘」；确要搬需勾选「我已知悉风险」
  · 中：有自启动项 / 注册表安装信息 / 卸载项引用
  · 低：只有快捷方式，或没有任何外部引用

安全原则（与 guard_wizard 一脉相承）：
  · 先复制、逐项校验，成功后再动源目录；任一步失败立即回滚，绝不留半吊子
  · 所有被改动的快捷方式 / 注册表值先备份，随时可一键还原
  · dry_run 只预览不改任何东西；服务永远只提示，不由本程序改写
  · 全程不联网、不删用户数据（旧副本仅在复制校验通过后才清理）

纯逻辑模块，不 import PySide6；外部系统操作集中在可注入的
scan_shortcuts / scan_registry / apply_* 函数里，方便测试用替身覆盖。
"""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import threading
import datetime
import subprocess
from pathlib import Path

from guard_appdir import APP_DIR
import guard_paths as gpaths
import guard_wizard as gwizard
import guard_fastscan

MIGRATIONS_FILE = APP_DIR / "software_migrations.json"
BACKUP_DIR = APP_DIR / "migration_backups"

RISK_LOW = "低"
RISK_MED = "中"
RISK_HIGH = "高"
RISK_COLORS = {RISK_LOW: "#2f7d32", RISK_MED: "#b26a00", RISK_HIGH: "#c62828"}

# 系统保护目录：这些位置不建议整体搬走
_PROTECTED = (
    "\\windows\\", "\\program files\\common files\\", "\\windowsapps\\",
    "\\system32\\", "\\syswow64\\", "\\winsxs\\",
    "\\programdata\\microsoft\\", "\\programdata\\package cache\\",
)

# 快捷方式要搜的位置（用 PowerShell 的 Environment.GetFolderPath，语言无关）
_PS_SCAN_SHORTCUTS = r'''
$ErrorActionPreference = "SilentlyContinue"
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath("Desktop"),
  [Environment]::GetFolderPath("CommonDesktopDirectory"),
  [Environment]::GetFolderPath("StartMenu"),
  [Environment]::GetFolderPath("CommonStartMenu"),
  [Environment]::GetFolderPath("Programs"),
  [Environment]::GetFolderPath("CommonPrograms")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$res = @()
foreach ($d in $dirs) {
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $l = $sh.CreateShortcut($_.FullName)
      $res += [pscustomobject]@{
        path    = $_.FullName
        target  = [string]$l.TargetPath
        workdir = [string]$l.WorkingDirectory
        args    = [string]$l.Arguments
        icon    = [string]$l.IconLocation
      }
    } catch {}
  }
}
$res | ConvertTo-Json -Depth 3 -Compress | Set-Content -LiteralPath "__OUTFILE__" -Encoding UTF8
'''

_PS_APPLY_SHORTCUTS = r'''
$ErrorActionPreference = "SilentlyContinue"
$items = Get-Content -LiteralPath "__INFILE__" -Raw -Encoding UTF8 | ConvertFrom-Json
$sh = New-Object -ComObject WScript.Shell
$ok = @(); $fail = @()
foreach ($i in $items) {
  try {
    $l = $sh.CreateShortcut($i.path)
    if ($i.new_target)  { $l.TargetPath = $i.new_target }
    if ($i.new_workdir) { $l.WorkingDirectory = $i.new_workdir }
    if ($null -ne $i.new_args) { $l.Arguments = $i.new_args }
    if ($i.new_icon)    { $l.IconLocation = $i.new_icon }
    $l.Save()
    $ok += $i.path
  } catch { $fail += $i.path }
}
[pscustomobject]@{ ok = $ok; fail = $fail } |
  ConvertTo-Json -Depth 3 -Compress | Set-Content -LiteralPath "__OUTFILE__" -Encoding UTF8
'''


def human_size(n) -> str:
    return gwizard.human_size(n)


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm(p) -> str:
    return str(p or "").strip().lower().replace("/", "\\").rstrip("\\/")


# ============================================================
# 一、枚举可迁移软件
# ============================================================

def default_install_roots() -> list:
    """常见软件安装根目录（只读枚举，不改动）。"""
    out = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        v = os.environ.get(env, "")
        if not v:
            continue
        base = Path(v) if env != "LOCALAPPDATA" else Path(v) / "Programs"
        if base.is_dir():
            out.append(str(base))
    # 用户自己可能装在别处，不猜；由界面里「浏览…」手动加
    return out


def _dir_stats(path, max_seconds=6.0) -> tuple:
    """用并发快扫算目录体积/文件数（带时间预算，卡住就返回已统计部分）。"""
    total = [0]
    cnt = [0]
    t0 = time.time()
    cancel = threading.Event()

    def descend(parent, level, ctx, entry):
        return ctx

    def visit(entry, parent, level, ctx):
        if entry.is_dir:
            return
        total[0] += entry.size
        cnt[0] += 1
        if time.time() - t0 > max_seconds:
            cancel.set()

    try:
        guard_fastscan.parallel_scan(
            [(str(path), 0, "x")], descend, visit,
            cancel_evt=cancel, stat_dirs=False, progress_every=100000)
    except Exception:
        pass
    return total[0], cnt[0]


def list_apps(min_mb: float = 20.0, max_items: int = 120) -> list:
    """枚举本机可迁移的软件目录。

    数据来源全是 Windows 公开只读位置：注册表卸载表 + 常见安装根目录。
    返回 [{name, path, size, files, publisher, source}]，按体积倒序。
    """
    rows, seen = [], set()

    def add(path, name, publisher, source):
        try:
            p = Path(path)
            if not p.is_dir():
                return
            key = _norm(str(p))
            if key in seen:
                return
            seen.add(key)
            size, cnt = _dir_stats(p)
            if size < min_mb * 2 ** 20:
                return
            rows.append({"name": name or p.name, "path": str(p), "size": size,
                         "files": cnt, "publisher": publisher or "",
                         "source": source})
        except Exception:
            return

    # 1) 注册表「已安装软件」的 InstallLocation
    try:
        import guard_rules
        for a in guard_rules.list_installed_apps():
            if a.get("source") == "下载的安装包":
                continue
            if a.get("path"):
                add(a["path"], a["name"], a.get("publisher", ""), "已安装")
    except Exception:
        pass

    # 2) 常见安装根目录下的一级子目录（绿色/便携软件注册表里没有）
    for root in default_install_roots():
        try:
            for d in Path(root).iterdir():
                if not d.is_dir() or d.name.startswith((".", "$")):
                    continue
                if gpaths.is_junction(d):
                    continue          # 已经搬过的（带链接）不重复列
                add(str(d), d.name, "", "安装目录")
        except (OSError, PermissionError):
            continue
        if len(rows) >= max_items:
            break

    rows.sort(key=lambda x: -x["size"])
    return rows[:max_items]


# ============================================================
# 二、引用扫描：快捷方式 / 注册表 / 服务
# ============================================================

def _run_ps(script: str, out_file: Path, in_file: Path = None,
            timeout: int = 90) -> bool:
    """把 PowerShell 脚本写到临时文件执行。

    in_file 是「输入 JSON」路径（更新快捷方式时用），out_file 是「输出 JSON」
    路径；两者必须不同，否则脚本会先覆盖掉自己的输入。
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ps1 = BACKUP_DIR / ("_ps_%d.ps1" % int(time.time() * 1000))
        text = script.replace("__OUTFILE__", str(out_file))
        if in_file is not None:
            text = text.replace("__INFILE__", str(in_file))
        # 必须带 BOM：PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 ANSI 解析，
        # 数据目录路径里的中文就会变乱码，导致脚本写错位置
        ps1.write_text(text, encoding="utf-8-sig")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(ps1)],
            capture_output=True, timeout=timeout)
        try:
            ps1.unlink()
        except OSError:
            pass
        return out_file.exists()
    except Exception:
        return False


def _read_ps_json(path: Path):
    try:
        txt = path.read_text(encoding="utf-8-sig").strip()
        if not txt:
            return None
        data = json.loads(txt)
        return data
    except Exception:
        return None


def scan_shortcuts() -> list:
    """枚举桌面 / 开始菜单里的 .lnk（只读）。COM 不可用时返回空列表。"""
    out = BACKUP_DIR / "_shortcuts_scan.json"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
    except OSError:
        pass
    if not _run_ps(_PS_SCAN_SHORTCUTS, out, timeout=120):
        return []
    data = _read_ps_json(out)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    return [x for x in data if isinstance(x, dict) and x.get("path")]


def _replace_path(text: str, old: str, new: str) -> str:
    """把 text 里出现的 old 路径前缀替换成 new，保留其余字符原样（大小写不敏感）。"""
    if not text or not old:
        return ""
    lo, old_l = text.lower(), old.lower().rstrip("\\/")
    idx = lo.find(old_l)
    if idx < 0:
        return ""
    return text[:idx] + str(new).rstrip("\\/") + text[idx + len(old_l):]


def _hits_old(value: str, old: str) -> bool:
    return bool(value) and old.lower().rstrip("\\/") in str(value).lower()


def _registry_roots():
    try:
        import winreg
    except ImportError:
        return []
    return [
        (winreg.HKEY_CURRENT_USER, "HKCU", r"Software", 4),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE", 3),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM",
         r"SYSTEM\CurrentControlSet\Services", 2),
        (winreg.HKEY_CURRENT_USER, "HKCU",
         r"Software\Microsoft\Windows\CurrentVersion\Run", 1),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM",
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", 1),
    ]


def scan_registry(old_root: str, max_values: int = 20000,
                  max_depth: int = 4) -> list:
    """扫描注册表里指向 old_root 的字符串值（只读，权限不足自动跳过）。

    返回 [{hive, key, name, old, kind, service}]。
    kind: 'value'（可替换） / 'service'（只提示，不自动改）
    """
    try:
        import winreg
    except ImportError:
        return []
    old = _norm(old_root)
    if not old:
        return []
    out = []
    budget = [0]

    def walk(hive, tag, sub, depth, is_service):
        if depth > max_depth or budget[0] >= max_values:
            return
        try:
            k = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ)
        except OSError:
            return                      # 权限不足 / 键不存在：安静跳过
        try:
            n_val = winreg.QueryInfoKey(k)[1]
            for i in range(n_val):
                if budget[0] >= max_values:
                    break
                try:
                    name, val, typ = winreg.EnumValue(k, i)
                except OSError:
                    continue
                budget[0] += 1
                if typ not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    continue
                if not _hits_old(str(val), old):
                    continue
                out.append({
                    "hive": tag, "key": sub, "name": name, "old": str(val),
                    "type": typ,
                    "kind": "service" if is_service else "value",
                    "service": sub.rsplit("\\", 1)[-1] if is_service else "",
                })
            n_sub = winreg.QueryInfoKey(k)[0]
            for i in range(n_sub):
                if budget[0] >= max_values:
                    break
                try:
                    child = winreg.EnumKey(k, i)
                except OSError:
                    continue
                walk(hive, tag, sub + "\\" + child, depth + 1, is_service)
        except OSError:
            pass
        finally:
            try:
                winreg.CloseKey(k)
            except OSError:
                pass

    for hive, tag, sub, depth in _registry_roots():
        is_service = "Services" in sub
        walk(hive, tag, sub, 0, is_service)
    return out


def scan_services(old_root: str) -> list:
    """只看服务 ImagePath（不扫描其它注册表值），用于风险分级。"""
    try:
        import winreg
    except ImportError:
        return []
    old = _norm(old_root)
    out = []
    base = r"SYSTEM\CurrentControlSet\Services"
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        n = winreg.QueryInfoKey(k)[0]
        for i in range(n):
            try:
                svc = winreg.EnumKey(k, i)
                with winreg.OpenKey(k, svc, 0, winreg.KEY_READ) as sk:
                    try:
                        img = str(winreg.QueryValueEx(sk, "ImagePath")[0])
                    except OSError:
                        continue
                    if _hits_old(img, old):
                        out.append({"name": svc, "image": img})
            except OSError:
                continue
        winreg.CloseKey(k)
    except OSError:
        pass
    return out


# ============================================================
# 三、风险分级
# ============================================================

def assess_risk(src: Path, shortcuts=None, registry=None, services=None) -> dict:
    """给出迁移风险等级与理由。三个扫描结果可注入，方便测试。"""
    src = Path(src)
    reasons = []
    level = RISK_LOW
    shortcuts = shortcuts if shortcuts is not None else []
    registry = registry if registry is not None else []
    services = services if services is not None else []

    pl = _norm(str(src))
    if any(k in pl + "\\" for k in _PROTECTED):
        reasons.append("位于系统保护目录，搬走可能破坏系统组件")
        level = RISK_HIGH
    if services:
        names = "、".join(s["name"] for s in services[:4])
        reasons.append(f"有 {len(services)} 个 Windows 服务指向这里（{names}）；"
                       "服务不会自动改，建议卸载后重装到目标盘")
        level = RISK_HIGH
    # 驱动文件：高风险（内核驱动签名 / 路径绑定）；只查前两层，避免大目录卡住
    try:
        for cur, dirs, files in os.walk(str(src)):
            if any(f.lower().endswith(".sys") for f in files):
                reasons.append("目录里含驱动文件（.sys），搬家可能导致驱动无法加载")
                level = RISK_HIGH
                break
            if os.path.relpath(cur, str(src)).count(os.sep) >= 2:
                dirs[:] = []
    except (OSError, PermissionError):
        pass
    reg_values = [r for r in registry if r.get("kind") == "value"]
    if reg_values:
        auto = [r for r in reg_values if "CurrentVersion\\Run" in r.get("key", "")]
        if auto:
            reasons.append(f"有 {len(auto)} 个开机自启动项引用旧路径")
            if level != RISK_HIGH:
                level = RISK_MED
        reasons.append(f"注册表里有 {len(reg_values)} 处路径引用（可自动替换）")
        if level == RISK_LOW:
            level = RISK_MED
    hit_sc = [s for s in shortcuts if _shortcut_hits(s, str(src))]
    if hit_sc:
        reasons.append(f"有 {len(hit_sc)} 个快捷方式指向旧目录（可自动更新）")
    if not reasons:
        reasons.append("没有发现外部引用，搬走后软件一般可直接照常使用")
    return {
        "level": level,
        "color": RISK_COLORS[level],
        "reasons": reasons,
        "services": services,
        "shortcut_count": len(hit_sc),
        "registry_count": len(reg_values),
    }


def _shortcut_hits(sc: dict, old_root: str) -> bool:
    old = _norm(old_root)
    for f in ("target", "workdir", "icon", "args"):
        if _hits_old(str(sc.get(f, "")), old):
            return True
    return False


# ============================================================
# 四、生成计划（纯只读）
# ============================================================

def build_plan(src, dst_root, keep_link=True, *, shortcuts=None,
               registry=None, services=None, stats=None) -> dict:
    """生成一份完整的迁移计划，**不改动任何东西**。

    dst_root 是目标盘根目录（如 `D:\\`），实际目录为
    `<dst_root>\\C盘守卫迁移\\<软件目录名>`。
    """
    src = Path(src)
    old = str(src)
    # "D:\" 去掉尾斜杠后只剩 "D:"，Path 会当成相对路径 → 必须补回根斜杠
    root = str(dst_root).strip().rstrip("\\/")
    if re.fullmatch(r"[A-Za-z]:", root):
        root += "\\"
    dst = Path(root) / "C盘守卫迁移" / gwizard.safe_name(src.name)
    if stats is None:
        stats = _dir_stats(src)
    size, cnt = stats

    sc = shortcuts if shortcuts is not None else scan_shortcuts()
    reg = registry if registry is not None else scan_registry(old)
    svc = services if services is not None else scan_services(old)
    risk = assess_risk(src, sc, reg, svc)

    sc_rows = []
    for s in sc:
        if not _shortcut_hits(s, old):
            continue
        row = {"path": s.get("path", ""), "old": {}, "new": {}}
        for f in ("target", "workdir", "args", "icon"):
            v = str(s.get(f, "") or "")
            nv = _replace_path(v, old, str(dst))
            if nv:
                row["old"][f] = v
                row["new"][f] = nv
        sc_rows.append(row)

    reg_rows = []
    for r in reg:
        if r.get("kind") != "value":
            continue
        nv = _replace_path(str(r.get("old", "")), old, str(dst))
        if nv:
            reg_rows.append({**r, "new": nv})

    steps = [
        f"① 复制 {cnt} 个文件（{human_size(size)}）到 {dst}",
        "② 逐项校验文件数量一致后才动源目录",
        f"③ 更新 {len(sc_rows)} 个快捷方式的目标/起始位置",
        f"④ 替换 {len(reg_rows)} 处注册表路径（先备份，可还原）",
    ]
    if svc:
        steps.append(f"⑤ ⚠️ {len(svc)} 个服务需你手动处理（程序不会自动改服务）")
    steps.append("⑥ 原位置" + ("留 junction 透明链接，软件按老路径照常读写"
                              if keep_link else "清空，不再保留"))

    return {
        "src": old, "dst": str(dst), "name": src.name,
        "size": size, "files": cnt, "keep_link": bool(keep_link),
        "risk": risk, "shortcuts": sc_rows, "registry": reg_rows,
        "services": svc, "steps": steps,
        "need_confirm_high_risk": risk["level"] == RISK_HIGH,
    }


def plan_summary(plan: dict) -> str:
    r = plan["risk"]
    return (f"【{plan['name']}】{human_size(plan['size'])} / {plan['files']} 个文件\n"
            f"风险等级：{r['level']}\n"
            + "\n".join("  · " + x for x in r["reasons"])
            + "\n\n将执行：\n" + "\n".join("  " + s for s in plan["steps"]))


# ============================================================
# 五、执行 / 回滚
# ============================================================

def _apply_shortcuts(rows, backup: list) -> tuple:
    """批量更新 .lnk；返回 (ok 数, 失败数)。失败不影响整体迁移。"""
    if not rows:
        return 0, 0
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        inp = BACKUP_DIR / "_shortcuts_apply.json"
        out = BACKUP_DIR / "_shortcuts_result.json"
        payload = []
        for r in rows:
            item = {"path": r["path"]}
            for f in ("target", "workdir", "args", "icon"):
                if f in r.get("new", {}):
                    item["new_" + f] = r["new"][f]
            payload.append(item)
            backup.append({"kind": "shortcut", "path": r["path"],
                           "old": r.get("old", {})})
        inp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if out.exists():
            out.unlink()
        if not _run_ps(_PS_APPLY_SHORTCUTS, out, in_file=inp):
            return 0, len(rows)
        data = _read_ps_json(out) or {}
        ok = len(data.get("ok") or []) if isinstance(data, dict) else 0
        return ok, len(rows) - ok
    except Exception:
        return 0, len(rows)


def _apply_registry(rows, backup: list) -> tuple:
    """替换注册表字符串值（先备份原值）；权限不足的键安静跳过。"""
    if not rows:
        return 0, 0
    try:
        import winreg
    except ImportError:
        return 0, len(rows)
    hive_map = {"HKCU": winreg.HKEY_CURRENT_USER,
                "HKLM": winreg.HKEY_LOCAL_MACHINE}
    ok = fail = 0
    for r in rows:
        hive = hive_map.get(r.get("hive"))
        if hive is None:
            fail += 1
            continue
        try:
            with winreg.OpenKey(hive, r["key"], 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, r["name"], 0, int(r.get("type", winreg.REG_SZ)),
                                  r["new"])
            backup.append({"kind": "registry", "hive": r["hive"],
                           "key": r["key"], "name": r["name"],
                           "old": r["old"], "type": r.get("type", 1)})
            ok += 1
        except OSError:
            fail += 1
    return ok, fail


def apply_plan(plan: dict, log=print, dry_run=False,
               update_shortcuts=True, update_registry=True,
               cancel_evt=None) -> dict:
    """执行迁移计划。dry_run=True 时只预览、绝不改动。

    返回 {ok, stage, detail, freed, record}；record 会被写进迁移记录文件，
    供 rollback() 使用。
    """
    src = Path(plan["src"])
    dst = Path(plan["dst"])
    res = {"ok": False, "stage": "", "detail": "", "freed": 0, "record": None}
    if dry_run:
        res.update({"ok": True, "stage": "模拟运行",
                    "detail": plan_summary(plan)})
        return res
    if not src.is_dir():
        res["stage"], res["detail"] = "源目录不存在", str(src)
        return res
    if gpaths.is_junction(src):
        res["stage"], res["detail"] = "跳过", "源目录已经是链接（之前搬过）"
        return res
    if dst.exists():
        res["stage"], res["detail"] = "跳过", f"目标目录已存在：{dst}"
        return res
    if cancel_evt is not None and cancel_evt.is_set():
        res["stage"], res["detail"] = "已取消", ""
        return res

    backup = []
    old_dir = None
    junction_made = False
    # 目标盘剩余空间检查（不足则不开始，避免复制到一半）
    try:
        free = shutil.disk_usage(str(dst.anchor)).free
        if free < plan["size"] * 1.05:
            res["stage"] = "目标盘空间不足"
            res["detail"] = (f"需要 {human_size(plan['size'] * 1.05)}，"
                             f"{dst.anchor} 只剩 {human_size(free)}")
            return res
    except OSError:
        pass

    log(f"    ① 复制 {plan['files']} 个文件（{human_size(plan['size'])}）…")
    ok, rc = gwizard.quiet_copy(src, dst)
    if not ok:
        res["stage"], res["detail"] = "复制失败", f"robocopy 返回码 {rc}"
        return res
    dsize, dcnt, _ = gpaths.dir_size(dst, max_items=plan["files"] * 2 + 100)
    if plan["files"] and dcnt < plan["files"] * 0.99:
        res["stage"], res["detail"] = "校验失败", \
            f"源 {plan['files']} 个文件，目标只有 {dcnt} 个（数据未改动）"
        return res

    if update_shortcuts:
        n_ok, n_fail = _apply_shortcuts(plan.get("shortcuts", []), backup)
        log(f"    ③ 快捷方式：更新 {n_ok} 个" + (f"，失败 {n_fail} 个" if n_fail else ""))
    if update_registry:
        n_ok, n_fail = _apply_registry(plan.get("registry", []), backup)
        log(f"    ④ 注册表：替换 {n_ok} 处" + (f"，{n_fail} 处权限不足已跳过" if n_fail else ""))

    log("    ⑤ 源目录改名腾位置…")
    old_dir = Path(str(src) + "_守卫待清理")
    try:
        os.rename(str(src), str(old_dir))
    except OSError as e:
        # 引用已经改过了 → 立刻还原，绝不留半吊子
        _restore_references(backup)
        res["stage"], res["detail"] = "重命名失败", \
            f"{type(e).__name__}（可能有文件被占用）；已还原引用，你的数据未改动"
        return res

    if plan.get("keep_link", True):
        log("    ⑥ 在原位置建 junction 透明链接…")
        junction_made = gwizard.make_junction(src, dst)
        if not junction_made:
            try:
                os.rename(str(old_dir), str(src))
            except OSError:
                pass
            _restore_references(backup)
            res["stage"], res["detail"] = "建链接失败", \
                "已还原原目录与引用，数据未动"
            return res

    try:
        shutil.rmtree(str(old_dir), ignore_errors=True)
    except Exception:
        pass

    record = {
        "id": "m%d" % int(time.time() * 1000),
        "时间": _ts(), "名称": plan["name"],
        "原位置": str(src), "新位置": str(dst),
        "大小": plan["size"], "文件数": plan["files"],
        "风险等级": plan["risk"]["level"],
        "留链接": bool(plan.get("keep_link", True) and junction_made),
        "服务": [s["name"] for s in plan.get("services", [])],
        "快捷方式备份": [b for b in backup if b["kind"] == "shortcut"],
        "注册表备份": [b for b in backup if b["kind"] == "registry"],
        "旧副本残留": str(old_dir) if old_dir.exists() else "",
    }
    _save_record(record)
    res.update({"ok": True, "stage": "完成",
                "detail": ("原位置留有透明链接，软件照常按老路径读写"
                           if record["留链接"] else "原位置已清空，不再保留"),
                "freed": plan["size"], "record": record})
    return res


def _restore_references(backup: list) -> int:
    """把改过的快捷方式 / 注册表值还原（失败尽力而为，不抛异常）。"""
    done = 0
    try:
        import winreg
        hive_map = {"HKCU": winreg.HKEY_CURRENT_USER,
                    "HKLM": winreg.HKEY_LOCAL_MACHINE}
        for b in backup:
            if b.get("kind") != "registry":
                continue
            hive = hive_map.get(b.get("hive"))
            if hive is None:
                continue
            try:
                with winreg.OpenKey(hive, b["key"], 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, b["name"], 0,
                                      int(b.get("type", winreg.REG_SZ)), b["old"])
                done += 1
            except OSError:
                pass
    except Exception:
        pass
    sc = [b for b in backup if b.get("kind") == "shortcut"]
    if sc:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            inp = BACKUP_DIR / "_shortcuts_restore.json"
            out = BACKUP_DIR / "_shortcuts_restore_result.json"
            payload = []
            for b in sc:
                item = {"path": b["path"]}
                for f in ("target", "workdir", "args", "icon"):
                    if f in b.get("old", {}):
                        item["new_" + f] = b["old"][f]
                payload.append(item)
            inp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            if out.exists():
                out.unlink()
            _run_ps(_PS_APPLY_SHORTCUTS, out, in_file=inp)
            done += len(sc)
        except Exception:
            pass
    return done


def load_records() -> list:
    try:
        return json.loads(MIGRATIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_record(rec: dict):
    rows = load_records()
    rows.append(rec)
    MIGRATIONS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                               encoding="utf-8")


def rollback(rec_id: str, log=print) -> dict:
    """撤销一次软件迁移：还原引用 → 摘链接 → 把数据搬回原位置。"""
    rows = load_records()
    rec = next((r for r in rows if r.get("id") == rec_id), None)
    if not rec:
        return {"ok": False, "detail": "找不到这条迁移记录"}
    src = Path(rec["原位置"])
    dst = Path(rec["新位置"])
    backup = (list(rec.get("快捷方式备份") or []) +
              list(rec.get("注册表备份") or []))
    _restore_references(backup)
    if not dst.is_dir():
        return {"ok": False, "detail": f"新位置的数据不在了：{dst}"}
    if src.exists() and not gpaths.is_junction(src):
        return {"ok": False, "detail": "原位置已是真实目录，无需撤回"}
    if src.exists():
        try:
            src.rmdir()                # 摘掉 junction（不动目标数据）
        except OSError as e:
            return {"ok": False, "detail": f"链接摘不掉：{e}"}
    log("    把数据搬回原位置…")
    ok, rc = gwizard.quiet_copy(dst, src)
    if not ok:
        gwizard.make_junction(src, dst)     # 立刻把链接装回去，不留断链
        return {"ok": False, "detail": f"复制失败（robocopy {rc}），已还原链接"}
    _, ocnt, _ = gpaths.dir_size(src, max_items=rec["文件数"] * 2 + 100)
    if rec["文件数"] and ocnt < rec["文件数"] * 0.99:
        return {"ok": False, "detail": "回搬校验不一致，保留新数据不动，请手动确认"}
    try:
        shutil.rmtree(str(dst), ignore_errors=True)
    except Exception:
        pass
    MIGRATIONS_FILE.write_text(
        json.dumps([r for r in rows if r.get("id") != rec_id],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "detail": f"已把「{rec['名称']}」搬回 {src}"}
