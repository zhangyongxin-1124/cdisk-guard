# -*- coding: utf-8 -*-
"""
guard_growth.py — 聚合监控：按「来源」提醒，而不是按「文件」骚扰
================================================================
逐文件弹窗为什么必须淘汰：C 盘每分钟有几百上千次写入，真要一个个问，你什么都做不了。
所以改变思路——**不盯单个文件，盯「某个地方一共涨了多少」**：

    「检测到 微信缓存 在 C 盘近期增长 8.2 GB（现在共 21 GB），要搬到 D 盘吗？」
    【是】搬家（原位留目录联接，软件照常用，随时能撤回）
    【否】先别管（把新的体积累起来，再涨才提醒）
    【取消】这个目录以后别再问了

盯两类东西：
  1. 应用数据 / 缓存目录 —— 微信、QQ、网盘、WPS、浏览器、音乐软件…
     来源取自 guard_paths 的知识库，只盯体积最大的那几个，避免无谓扫描。
  2. 下载目录里的安装包 —— .exe/.msi/.zip/.iso 这类"装完就不用留"的东西，
     攒到一定量再问，搬走后原位留快捷方式。

只负责"发现该问了"，返回一条问题描述；真正弹窗、搬家由主程序执行。
"""

import os
import sys
import json
import time
import ctypes
import datetime
from pathlib import Path

import guard_paths as gp

# 打包成 exe 后 __file__ 指向的是 PyInstaller 的临时解压目录（退出就没了），
# 所以必须改用 exe 所在目录，否则配置和状态每关一次程序就丢一回。
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).parent
STATE_FILE = APP_DIR / "growth_state.json"

INSTALLER_EXT = {".exe", ".msi", ".msix", ".zip", ".rar", ".7z", ".iso",
                 ".cab", ".appx", ".tar", ".gz", ".tar.gz", ".dmg", ".pkg"}


def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def now():
    return time.time()


class GrowthMonitor:
    """按目录聚合成长量。"""

    MAX_TARGETS = 10          # 最多盯 10 个目录，避免每次体检都扫很久

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or {}
        self.state = self._load()
        self.installer_seen = {}      # 已经计入聚合的安装包（避免重复算）
        self.last_check = 0.0
        self.agg_dirs = [Path(p) for p in self.cfg.get("聚合监控目录", []) if p]

    # ---------- 状态 ----------
    def _load(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"apps": {}, "installers": {}}

    def _save(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception:
            pass

    # ---------- 目录监控 ----------
    def sync_targets(self, log=None):
        """把知识库里体积最大的几个目录纳入监控。"""
        home = Path(os.environ.get("USERPROFILE", ""))
        rows = gp.scan_app_targets(home, min_mb=float(self.cfg.get("聚合最小关注MB", 30)))
        for t, p, size, cnt in rows[:self.MAX_TARGETS]:
            key = t["key"]
            if key in self.state["apps"]:
                continue
            self.state["apps"][key] = {
                "label": t["label"], "path": str(p), "category": t["category"],
                "baseline": size, "baseline_ts": now(),
                "last_ask": 0.0, "muted": False,
            }
            if log:
                log(f"    纳入聚合监控：{t['label']}（基线 {human_size(size)}）")
        # 目标已经搬家/不存在的，移除
        for key in list(self.state["apps"]):
            p = Path(self.state["apps"][key]["path"])
            if not p.exists() or gp.is_junction(p):
                self.state["apps"].pop(key, None)
        self._save()

    # ---------- 检查 ----------
    def check(self, force: bool = False) -> list:
        """返回需要询问的问题列表。每项是一段描述 + 处理所需数据。"""
        interval = float(self.cfg.get("聚合检查间隔秒", 600))
        if not force and now() - self.last_check < interval:
            return []
        self.last_check = now()
        alerts = []
        thr = float(self.cfg.get("聚合增长阈值MB", 500)) * 2 ** 20
        cool = float(self.cfg.get("聚合提醒冷却秒", 14400))

        for key, st in list(self.state["apps"].items()):
            if st.get("muted"):
                continue
            p = Path(st["path"])
            if not p.exists() or gp.is_junction(p):
                self.state["apps"].pop(key, None)   # 已经搬走了，不用再盯
                continue
            cur, cnt, _ = gp.dir_size(p)
            delta = cur - st.get("baseline", cur)
            if delta < thr:
                continue
            if now() - st.get("last_ask", 0) < cool:
                continue
            st["last_ask"] = now()
            self._save()
            alerts.append({
                "kind": "app", "key": key, "label": st["label"], "path": p,
                "cur": cur, "delta": delta, "files": cnt,
                "days": max(1.0, (now() - st.get("baseline_ts", now())) / 86400),
            })

        alerts.extend(self._check_installers())
        return alerts

    def _check_installers(self) -> list:
        sti = self.state.setdefault("installers", {"baseline_ts": now(), "last_ask": 0.0,
                                                   "muted": False})
        if sti.get("muted"):
            return []
        min_mb = float(self.cfg.get("安装包最小MB", 30))
        thr = float(self.cfg.get("安装包阈值MB", 300)) * 2 ** 20
        cool = float(self.cfg.get("聚合提醒冷却秒", 14400))
        found = []
        for d in self.agg_dirs:
            if not d.exists():
                continue
            try:
                with os.scandir(str(d)) as it:
                    for e in it:
                        try:
                            if not e.is_file(follow_symlinks=False):
                                continue
                            suf = os.path.splitext(e.name)[1].lower()
                            if suf not in INSTALLER_EXT:
                                continue
                            sz = e.stat(follow_symlinks=False).st_size
                            if sz < min_mb * 2 ** 20:
                                continue
                            found.append((Path(e.path), sz, e.stat(follow_symlinks=False).st_mtime))
                        except OSError:
                            continue
            except OSError:
                continue
        if not found:
            return []
        fresh = [f for f in found if str(f[0]) not in self.installer_seen]
        total = sum(f[1] for f in fresh)
        if total < thr:
            # 还不够大就先记下这批，下次不重复计入同一个文件的增量
            for p, _, _ in found:
                self.installer_seen[str(p)] = True
            return []
        for p, _, _ in fresh:
            self.installer_seen[str(p)] = True
        if now() - sti.get("last_ask", 0) < cool:
            return []
        sti["last_ask"] = now()
        self._save()
        return [{
            "kind": "installers", "key": "installers",
            "label": "下载目录里的安装包", "path": None,
            "cur": total, "delta": total,
            "files": [f[0] for f in fresh], "total_size": total,
            "days": max(1.0, (now() - sti.get("baseline_ts", now())) / 86400),
        }]

    # ---------- 结果回写 ----------
    def settle(self, key: str):
        """用户选了"先别管"：把当前体积设成新基线，下次再涨才是新的增量。"""
        if key == "installers":
            self.state.setdefault("installers", {})["baseline_ts"] = now()
            self.installer_seen.clear()
            self._save()
            return
        st = self.state["apps"].get(key)
        if not st:
            return
        try:
            cur, _, _ = gp.dir_size(Path(st["path"]))
            st["baseline"] = cur
        except Exception:
            st["baseline"] = st.get("baseline", 0)
        st["baseline_ts"] = now()
        self._save()

    def mute(self, key: str):
        if key == "installers":
            self.state.setdefault("installers", {})["muted"] = True
        elif key in self.state["apps"]:
            self.state["apps"][key]["muted"] = True
        self._save()

    def unmute_all(self):
        for st in self.state["apps"].values():
            st["muted"] = False
        self.state.setdefault("installers", {})["muted"] = False
        self._save()

    def summary(self) -> str:
        apps = self.state.get("apps", {})
        n = sum(1 for v in apps.values() if not v.get("muted"))
        return f"盯 {n} 个目录"


# ============================================================
# 搬迁执行
# ============================================================

# ---- .lnk 快捷方式：直接用 Windows 的 IShellLinkW COM 接口 ----
# 不走 PowerShell：一是省得拉起进程，二是中文路径经过 PowerShell -Command
# 会被控制台代码页改写得乱七八糟（实测 COMException）。
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _iid(s: str) -> _GUID:
    s = s.strip("{}")
    a, b, c, d, e = s.split("-")
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = int(a, 16), int(b, 16), int(c, 16)
    rest = d + e
    g.Data4 = (ctypes.c_ubyte * 8)(*[int(rest[i * 2:i * 2 + 2], 16) for i in range(8)])
    return g


_CLSID_SHELLLINK = _iid("{00021401-0000-0000-C000-000000000046}")
_IID_ISHELLLINKW = _iid("{000214F9-0000-0000-C000-000000000046}")
_IID_IPERSISTFILE = _iid("{0000010b-0000-0000-C000-000000000046}")
_HR = ctypes.c_long


def _shelllink_obj():
    ole32 = ctypes.windll.ole32
    try:
        ole32.CoInitialize(None)
    except Exception:
        return None, None
    p = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_CLSID_SHELLLINK), None, 1,
                                ctypes.byref(_IID_ISHELLLINKW), ctypes.byref(p))
    if hr != 0 or not p:
        return None, None
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return p, vt


def _com_shortcut(link: Path, target: Path, desc: str = "") -> bool:
    p, vt = _shelllink_obj()
    if p is None:
        return False
    try:
        SetPath = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, ctypes.c_wchar_p)(vt[20])
        SetDesc = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, ctypes.c_wchar_p)(vt[7])
        SetWD = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, ctypes.c_wchar_p)(vt[9])
        QI = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                ctypes.POINTER(ctypes.c_void_p))(vt[0])
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])

        SetPath(p, str(target))
        SetDesc(p, desc or Path(target).name)
        SetWD(p, str(Path(target).parent))
        pf = ctypes.c_void_p()
        ok = False
        if QI(p, ctypes.byref(_IID_IPERSISTFILE), ctypes.byref(pf)) == 0 and pf:
            pfv = ctypes.cast(pf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            Save = ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_int)(pfv[6])
            ok = (Save(pf, str(link), True) == 0)
            Release(pf)
        Release(p)
        return ok and Path(link).exists()
    except Exception:
        return False


def make_shortcut(link: Path, target: Path, desc: str = "") -> bool:
    """在原位置留一个指向新位置的快捷方式。

    三套办法依次试：
      ① 直接调 Windows 的 IShellLinkW COM 接口（最快，也不需要拉起别的程序）
      ② PowerShell 的 WScript.Shell
      ③ 写成 .ps1 用 -File 执行（能绕开命令行参数被代码页改写的问题）
    注意：有些机器开着"受控文件夹访问"，往下载/文档这类目录写 .lnk 会被挡掉，
    那时候由 relocate_files 退到"留一张说明纸条"的方案。
    """
    if _com_shortcut(link, target, desc):
        return True
    import subprocess
    ps = ('$s=(New-Object -ComObject WScript.Shell).CreateShortcut("' + str(link) + '");'
          '$s.TargetPath="' + str(target) + '";$s.Save()')
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=30)
        if link.exists():
            return True
    except Exception:
        pass
    try:
        script = Path(link).parent / "_mklnk_tmp.ps1"
        script.write_text(ps, encoding="utf-8-sig")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(script)],
                       capture_output=True, timeout=30)
        script.unlink(missing_ok=True)
    except Exception:
        pass
    return link.exists()


def leave_note(src: Path, dst: Path) -> Path:
    """快捷方式建不出来时的兜底：留一张说明纸条，告诉你文件搬哪去了。"""
    note = src.with_name(src.name + " 已搬到.txt")
    try:
        note.write_text(
            f"这个文件已经被 C 盘守卫搬走了：\n\n"
            f"  原位置：{src}\n"
            f"  新位置：{dst}\n\n"
            f"想放回来：把新位置的文件复制回原位置，再删掉这张纸条即可。\n"
            f"（本来想给你留个快捷方式，但这台机器不允许往这个目录写 .lnk 文件）\n",
            encoding="utf-8")
        return note
    except OSError:
        return Path("")


def relocate_files(files: list, dst_root: Path, log=None) -> dict:
    """把一批文件搬到 dst_root，原位留快捷方式。逐个「先复制、校验、再删源」。"""
    import shutil
    dst_root.mkdir(parents=True, exist_ok=True)
    moved, failed, freed = 0, [], 0
    note_left = 0
    for src in files:
        try:
            if not src.exists():
                continue
            size = src.stat().st_size
            dst = dst_root / src.name
            n = 0
            while dst.exists():
                n += 1
                dst = dst_root / f"{src.stem}_{n}{src.suffix}"
            shutil.copy2(str(src), str(dst))
            if not dst.exists() or abs(dst.stat().st_size - size) > 1:
                failed.append((src.name, "校验不一致"))
                try:
                    dst.unlink()
                except OSError:
                    pass
                continue
            src.unlink()                       # 复制成功才删源
            link = src.with_suffix(src.suffix + ".lnk")
            note = Path("")
            if not make_shortcut(link, dst):
                note = leave_note(src, dst)    # 兜底：留一张纸条说明搬哪去了
            note_left += bool(str(note))
            moved += 1
            freed += size
        except Exception as e:
            failed.append((src.name, type(e).__name__))
    return {"moved": moved, "failed": failed, "freed": freed, "note": note_left}


def age_text(days: float) -> str:
    if days < 1:
        return "最近几小时"
    return f"最近 {days:.0f} 天"
