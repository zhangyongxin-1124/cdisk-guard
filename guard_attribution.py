# -*- coding: utf-8 -*-
"""
guard_attribution.py — 来源归因增强（v6.14 新增）
================================================
原来「今日新增」的来源应用只靠一张本地关键词表猜，容易认错、也没有任何
可核验的信息。本模块补上三条更可靠的证据，并允许你手动纠正：

  1. 数字签名   读取 .exe 的 Authenticode 签名（Status / 证书 Subject / 颁发者），
                 有有效签名时把「发布者」显示出来，而不是只显示文件夹名
  2. 进程关联   找出 exe 就在这个目录里、当前正在运行的进程，
                 「是谁在往这儿写」有据可查
  3. 可信发布者 签名状态为 Valid 即标记「✅ 已验证签名」，并把证书里的
                 公司名（CN / O）提取出来；不预置任何品牌名单、不联网
  4. 手动修正   你觉得认错了，直接改，结果按目录关键词记下来并持久化，
                 下次扫描同目录的所有文件都按你写的名字显示

性能取舍（重要）：数字签名与进程枚举都是**昂贵**操作，绝不对几万个文件
逐个调用。本模块只在你要看某一组的详情时按需计算，并带磁盘缓存；
轻量部分（手动修正）才接进每文件都会走的 guess_app。

纯逻辑模块，不 import PySide6；所有系统查询失败都安静降级，不影响扫描。
"""
from __future__ import annotations

import os
import re
import json
import time
import subprocess
import threading
from pathlib import Path

from guard_appdir import APP_DIR

OVERRIDE_FILE = APP_DIR / "attribution_overrides.json"
SIG_CACHE_FILE = APP_DIR / "signature_cache.json"
TMP_DIR = APP_DIR / "attribution_tmp"

_CN_RE = re.compile(r"(?:^|,)\s*(?:CN|O)\s*=\s*([^,]+)", re.I)

_PS_SIGNATURES = r'''
$ErrorActionPreference = "SilentlyContinue"
$paths = Get-Content -LiteralPath "__INFILE__" -Raw -Encoding UTF8 | ConvertFrom-Json
$res = @()
foreach ($p in $paths) {
  try {
    $s = Get-AuthenticodeSignature -LiteralPath $p -ErrorAction Stop
    $res += [pscustomobject]@{
      path    = $p
      status  = [string]$s.Status
      subject = [string]$s.SignerCertificate.Subject
      issuer  = [string]$s.SignerCertificate.Issuer
    }
  } catch {
    $res += [pscustomobject]@{ path = $p; status = "Error"; subject = ""; issuer = "" }
  }
}
$res | ConvertTo-Json -Depth 3 -Compress | Set-Content -LiteralPath "__OUTFILE__" -Encoding UTF8
'''

_PS_PROCESSES = r'''
$ErrorActionPreference = "SilentlyContinue"
Get-CimInstance Win32_Process |
  Select-Object Name, ProcessId, ExecutablePath |
  ConvertTo-Json -Depth 3 -Compress |
  Set-Content -LiteralPath "__OUTFILE__" -Encoding UTF8
'''


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _run_ps(script: str, out_file: Path, in_file: Path = None,
            timeout: int = 60) -> bool:
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        ps1 = TMP_DIR / ("_ps_%d.ps1" % int(time.time() * 1000))
        text = script.replace("__OUTFILE__", str(out_file))
        if in_file is not None:
            text = text.replace("__INFILE__", str(in_file))
        # 必须带 BOM：PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按 ANSI 解析，
        # 数据目录路径里的中文就会变乱码，导致脚本写错位置
        ps1.write_text(text, encoding="utf-8-sig")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                       capture_output=True, timeout=timeout)
        try:
            ps1.unlink()
        except OSError:
            pass
        return out_file.exists()
    except Exception:
        return False


def _read_json(path: Path):
    try:
        txt = path.read_text(encoding="utf-8-sig").strip()
        return json.loads(txt) if txt else None
    except Exception:
        return None


# ============================================================
# 一、手动修正归属（最轻量，接进每文件的 guess_app）
# ============================================================

class OverrideStore:
    """用户手工指定的「这个目录属于哪个软件」，按关键词匹配、本地持久化。"""

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else OVERRIDE_FILE
        self.rules = []
        self.load()

    def load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rules = data.get("rules", []) if isinstance(data, dict) else []
        except Exception:
            self.rules = []
        return self.rules

    def save(self) -> bool:
        try:
            self.path.write_text(
                json.dumps({"version": 1, "rules": self.rules},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def set(self, keyword: str, app: str) -> dict:
        """把包含 keyword 的路径都归到 app。同名关键词覆盖。"""
        keyword = str(keyword or "").strip().lower()
        app = str(app or "").strip()
        if not keyword or not app:
            return {}
        self.rules = [r for r in self.rules
                      if str(r.get("keyword", "")).lower() != keyword]
        rule = {"id": "o%d" % int(time.time() * 1000),
                "keyword": keyword, "app": app, "time": _now()}
        self.rules.append(rule)
        self.save()
        return rule

    def remove(self, rid: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.get("id") != rid]
        if len(self.rules) != before:
            return self.save()
        return False

    def resolve(self, path: str) -> str:
        """返回用户指定的来源名；没有则空串。后加的规则优先。"""
        if not self.rules:
            return ""
        pl = str(path or "").lower().replace("/", "\\")
        for r in reversed(self.rules):
            k = str(r.get("keyword", "")).lower()
            if k and k in pl:
                return str(r.get("app", ""))
        return ""


overrides = OverrideStore()


def suggest_keyword(path: str) -> str:
    """从一条路径里挑出适合作为「目录关键词」的一段（用于手动修正弹窗默认值）。

    优先取 AppData\\Local|Roaming\\<段> 或 Program Files\\<段>，
    否则取路径里最后一个像软件名的目录段。
    """
    pl = str(path or "").replace("/", "\\")
    segs = [s for s in re.split(r"[\\/]+", pl) if s]
    skip = {"users", "appdata", "local", "locallow", "roaming", "programdata",
            "program files", "program files (x86)", "windows", "desktop",
            "documents", "downloads", "temp", "tmp", "cache", "data",
            "public", "default", "system32"}
    try:
        u = (os.environ.get("USERNAME") or "").lower()
        if len(u) >= 2:
            skip.add(u)
    except Exception:
        pass

    def valid(s: str) -> bool:
        return (len(s) >= 3 and s.lower() not in skip
                and not s.isdigit()
                and not re.search(r"\.[a-z0-9]{1,5}$", s, re.I))

    # 取「最后一个系统通用段」之后的那一段 —— 正是 AppData\Local\<软件名>
    sys_idx = -1
    for i, s in enumerate(segs):
        if s.lower() in skip or s.endswith(":") or len(s) < 3:
            sys_idx = i
    if sys_idx >= 0 and sys_idx + 1 < len(segs) and valid(segs[sys_idx + 1]):
        return segs[sys_idx + 1].lower()
    for s in reversed(segs[:-1]):
        if valid(s):
            return s.lower()
    return ""


# ============================================================
# 二、数字签名（批量 + 缓存）
# ============================================================

def _load_sig_cache() -> dict:
    try:
        if SIG_CACHE_FILE.exists():
            d = json.loads(SIG_CACHE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save_sig_cache(d: dict):
    try:
        SIG_CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False),
                                  encoding="utf-8")
    except Exception:
        pass


def _fingerprint(path: str) -> str:
    try:
        st = os.stat(path)
        return "%d_%d" % (int(st.st_mtime), int(st.st_size))
    except OSError:
        return "x"


def signature_info(exe_paths: list, timeout: int = 60) -> list:
    """批量读取 .exe 的 Authenticode 签名。

    返回 [{path, status, subject, issuer, publisher, verified}]。
    结果按 mtime+size 做磁盘缓存，同一个 exe 不会反复拉起 PowerShell。
    """
    paths = [str(p) for p in exe_paths if p and os.path.isfile(str(p))][:40]
    if not paths:
        return []
    cache = _load_sig_cache()
    need, out = [], {}
    for p in paths:
        fp = _fingerprint(p)
        hit = cache.get(p)
        if hit and hit.get("fp") == fp:
            out[p] = hit["info"]
        else:
            need.append(p)
    if need:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        inp = TMP_DIR / "_sig_in.json"
        oup = TMP_DIR / "_sig_out.json"
        inp.write_text(json.dumps(need, ensure_ascii=False), encoding="utf-8")
        if oup.exists():
            oup.unlink()
        if _run_ps(_PS_SIGNATURES, oup, in_file=inp, timeout=timeout):
            data = _read_json(oup) or []
            if isinstance(data, dict):
                data = [data]
            for row in data:
                if not isinstance(row, dict) or not row.get("path"):
                    continue
                info = {
                    "path": row["path"],
                    "status": str(row.get("status") or ""),
                    "subject": str(row.get("subject") or ""),
                    "issuer": str(row.get("issuer") or ""),
                }
                info["publisher"] = _publisher(info["subject"])
                info["verified"] = info["status"].lower() == "valid"
                out[row["path"]] = info
                cache[row["path"]] = {"fp": _fingerprint(row["path"]),
                                      "info": info}
        _save_sig_cache(cache)
    return [out[p] for p in paths if p in out]


def _publisher(subject: str) -> str:
    """从证书 Subject 里提取公司名（CN / O）。"""
    m = _CN_RE.search(subject or "")
    return m.group(1).strip().strip('"') if m else ""


# ============================================================
# 三、进程关联
# ============================================================

def running_processes(timeout: int = 30) -> list:
    """列出当前进程的 Name / PID / ExecutablePath（只读，失败返回空）。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    oup = TMP_DIR / "_proc.json"
    if oup.exists():
        oup.unlink()
    if not _run_ps(_PS_PROCESSES, oup, timeout=timeout):
        return []
    data = _read_json(oup) or []
    if isinstance(data, dict):
        data = [data]
    out = []
    for p in data:
        if not isinstance(p, dict):
            continue
        out.append({"name": str(p.get("Name") or ""),
                    "pid": int(p.get("ProcessId") or 0),
                    "exe": str(p.get("ExecutablePath") or "")})
    return out


def processes_in_dir(directory: str, procs=None) -> list:
    """找出 exe 就在这个目录（或其子目录）里、当前正在运行的进程。"""
    root = os.path.normcase(os.path.abspath(str(directory))).rstrip("\\/")
    if not root:
        return []
    procs = procs if procs is not None else running_processes()
    out = []
    for p in procs:
        exe = os.path.normcase(str(p.get("exe") or ""))
        if exe and (exe == root or exe.startswith(root + "\\")):
            out.append(p)
    return out


# ============================================================
# 四、按目录聚合的「来源详情」
# ============================================================

def find_exes(directory: str, max_items: int = 6) -> list:
    """在目录里找几个最有代表性的 .exe（顶层优先，按体积倒序）。"""
    d = Path(directory)
    found = []
    try:
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() == ".exe":
                try:
                    found.append((p.stat().st_size, str(p)))
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
    found.sort(reverse=True)
    if len(found) >= 2:
        return [x[1] for x in found[:max_items]]
    # 顶层没几个就下一层找
    try:
        for sub in list(d.iterdir())[:30]:
            if not sub.is_dir():
                continue
            for p in sub.iterdir():
                if p.is_file() and p.suffix.lower() == ".exe":
                    try:
                        found.append((p.stat().st_size, str(p)))
                    except OSError:
                        pass
            if len(found) >= max_items * 2:
                break
    except (OSError, PermissionError):
        pass
    found.sort(reverse=True)
    return [x[1] for x in found[:max_items]]


def enrich(directory: str, procs=None) -> dict:
    """给一个目录算一份「来源详情」（按需调用，带缓存）。

    返回 {dir, publisher, verified, signatures, processes, exes, hint}
    """
    d = str(directory or "")
    exes = find_exes(d)
    sigs = signature_info(exes) if exes else []
    proc_list = processes_in_dir(d, procs)
    valid = [s for s in sigs if s.get("verified")]
    publisher = ""
    for s in valid:
        if s.get("publisher"):
            publisher = s["publisher"]
            break
    if not publisher and proc_list:
        publisher = proc_list[0]["name"]
    if not publisher:
        publisher = "（未取得签名信息）"
    return {
        "dir": d,
        "publisher": publisher,
        "verified": bool(valid),
        "signatures": sigs,
        "processes": proc_list,
        "exes": exes,
        "hint": ("✅ 已验证数字签名" if valid else
                 "未验证签名（绿色软件 / 个人开发 / 签名已过期都属正常）"),
    }


def describe(info: dict) -> str:
    """把详情渲染成给用户看的一段文字。"""
    lines = [f"目录：{info.get('dir', '')}",
             f"发布者：{info.get('publisher', '')}",
             f"签名：{info.get('hint', '')}"]
    for s in info.get("signatures", [])[:3]:
        lines.append(f"  · {Path(s['path']).name}：{s['status']}"
                     + (f"（{s['publisher']}）" if s.get("publisher") else ""))
    ps = info.get("processes", [])
    if ps:
        lines.append("正在运行的相关进程：" +
                     "、".join(f"{p['name']}(PID {p['pid']})" for p in ps[:5]))
    else:
        lines.append("当前没有检测到运行中的相关进程")
    return "\n".join(lines)
