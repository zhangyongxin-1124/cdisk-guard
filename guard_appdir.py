# -*- coding: utf-8 -*-
"""
guard_appdir.py — 数据目录与本机适配（v6.9 新增）
=================================================
解决一个真实发生过的严重问题：把程序整个目录复制到另一台电脑后，
软件显示的还是原电脑的扫描数据，并且写入拦截失效。

原因有两个，都在这里根治：

  1. 数据目录写死在 exe 同目录 —— 整个目录一拷贝，旧数据跟着走。
  2. config.json 里存的是绝对路径（C:\\Users\\旧用户名\\Downloads ...），
     换台电脑后这些路径根本不存在，等于监控了一个空目录。

对策：
  · 数据目录自动选一个「本机一定可写」的位置（U盘便携时仍跟 exe 走）；
  · 给每台电脑算一个指纹，发现数据来自别的电脑就自动清空重建；
  · 加载配置时自动把里面的用户目录重定位到当前登录的用户。

本模块不依赖任何项目内模块，可安全被最先导入。
"""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

DATA_DIR_NAME = "CDiskGuard"          # 非便携模式下的数据目录名
MACHINE_FILE = "machine_id.json"      # 记录「这份数据属于哪台电脑」

# 换电脑后必须重建的数据（这些文件里存的是某台具体电脑的扫描结果）
MACHINE_LOCAL_FILES = (
    "config.json",
    "root_known.json",
    "install_known.json",
    "install_time.json",
    "migrations.json",
    "folder_locations_backup.json",
    "space_snapshots.json",
    "growth_state.json",
    "cleaner_state.json",
)
MACHINE_LOCAL_PREFIX = ("today_files", "legacy_")   # today_files*.json / legacy_*.json
MACHINE_LOCAL_DIRS = ("guard_log", "隔离区")

# 跨电脑通用的数据（用户自己添加的软件名规则，与机器无关，予以保留）
PORTABLE_KEEP = ("app_rules.json",)


# ============================================================
# 一、本机指纹
# ============================================================

def _volume_serial() -> int:
    """系统盘卷序列号（重装系统才会变，换电脑必定变）。取不到返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buf_name = ctypes.create_unicode_buffer(261)
        buf_fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        sysroot = os.environ.get("SystemDrive", "C:") + "\\"
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(sysroot), buf_name, 261,
            ctypes.byref(serial), ctypes.byref(maxlen), ctypes.byref(flags),
            buf_fs, 261)
        if ok:
            return serial.value
    except Exception:
        pass
    return 0


def _system_drive_size() -> int:
    """系统盘总字节数（卷序列号取不到时的补充判据）。"""
    try:
        root = os.environ.get("SystemDrive", "C:") + "\\"
        usage = shutil.disk_usage(root)
        return usage.total
    except Exception:
        return 0


def machine_id() -> str:
    """本机指纹：同一台电脑每次算都一样，换台电脑必定不同。

    取 4 项组合：计算机名 + 系统盘卷序列号 + 系统盘容量 + 当前用户目录。
    前 3 项定机器，第 4 项定用户（一台电脑多账号也能区分）。
    """
    raw = "|".join([
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        os.environ.get("USERPROFILE", ""),
        str(_volume_serial()),
        str(_system_drive_size()),
    ])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def machine_label() -> str:
    """给人看的本机标识，写进备份目录名，方便出问题时回溯。"""
    return "{}_{}".format(os.environ.get("COMPUTERNAME", "PC"),
                          os.environ.get("USERNAME", "User"))


# ============================================================
# 二、数据目录选择
# ============================================================

def _writable(p: Path) -> bool:
    """这个目录能不能真的写文件（Program Files 之类受保护目录会返回 False）。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe.tmp"
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        probe.unlink()
        return True
    except Exception:
        return False


def _exe_dir() -> Path:
    """exe 所在目录（未打包时是源码目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _user_data_dir() -> Path:
    """Windows 标准的用户数据目录：%LOCALAPPDATA%\\CDiskGuard。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return Path(base) / DATA_DIR_NAME


def _temp_data_dir() -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / DATA_DIR_NAME


def pick_app_dir() -> Path:
    """按优先级挑一个能用的数据目录。

      1) 环境变量 GUARD_APP_DIR（调试/强制指定用）
      2) exe 同目录 且 可写  → 便携模式（U盘随身带，数据跟着走）
      3) %LOCALAPPDATA%\\CDiskGuard  → 装在 Program Files 等只读位置时走这条路
      4) %TEMP%\\CDiskGuard  → 最后兜底，保证功能不瘫痪
    """
    env = os.environ.get("GUARD_APP_DIR")
    if env:
        p = Path(env)
        if _writable(p):
            return p
    d = _exe_dir()
    if _writable(d):
        return d
    u = _user_data_dir()
    if _writable(u):
        return u
    return _temp_data_dir()


APP_DIR = pick_app_dir()
PORTABLE = (APP_DIR == _exe_dir())     # 是否便携模式（数据就在 exe 旁边）
MACHINE_ID = machine_id()


# ============================================================
# 三、跨电脑数据清理
# ============================================================

def _is_machine_local(name: str) -> bool:
    if name in MACHINE_LOCAL_FILES:
        return True
    for pre in MACHINE_LOCAL_PREFIX:
        if name.startswith(pre) and name.endswith(".json"):
            return True
    return False


def purge_foreign_data(app_dir: Path = None, quiet: bool = False) -> dict:
    """检查数据目录里的东西是不是本机的；不是就整体归档后重建。

    场景：把整个便携版目录拷到另一台电脑，里面还留着上一台电脑的
    today_files.json / legacy_C.json / config.json（写着别人的路径）。
    留着它们 = 显示别人的数据 + 监控不存在的目录（拦截永不触发）。

    返回 {'purged': bool, 'backup': 备份目录或 None, 'moved': [文件名]}
    """
    app_dir = Path(app_dir or APP_DIR)
    result = {"purged": False, "backup": None, "moved": []}
    try:
        app_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return result

    marker = app_dir / MACHINE_FILE
    stored = None
    if marker.exists():
        try:
            stored = json.loads(marker.read_text(encoding="utf-8")).get("machine_id")
        except Exception:
            stored = None

    cur = machine_id()
    if stored == cur:
        return result          # 本机数据，正常

    # 第一次运行，或数据来自另一台电脑
    moved = []
    if stored is not None:
        # 有旧数据且不匹配 → 归档，不直接删（万一误判还能捞回来）
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = app_dir / ("_外来数据_{}_{}".format(machine_label(), stamp))
        try:
            backup.mkdir(parents=True, exist_ok=True)
            for item in sorted(app_dir.iterdir()):
                if item.name == backup.name or item.name == MACHINE_FILE:
                    continue
                if item.is_dir() and item.name not in MACHINE_LOCAL_DIRS:
                    continue
                if item.is_file() and (item.name in PORTABLE_KEEP
                                       or not _is_machine_local(item.name)):
                    continue
                try:
                    shutil.move(str(item), str(backup / item.name))
                    moved.append(item.name)
                except Exception:
                    pass
            if not moved:
                try:
                    backup.rmdir()
                except Exception:
                    pass
            else:
                result["backup"] = str(backup)
        except Exception:
            pass
        result["purged"] = True
        result["moved"] = moved
        if not quiet:
            print("[守卫] 检测到数据来自另一台电脑，已归档 {} 项：{}".format(
                len(moved), ", ".join(moved[:6]) + ("..." if len(moved) > 6 else "")))
            print("[守卫] 已按本机重新初始化数据目录：{}".format(app_dir))

    try:
        marker.write_text(json.dumps({
            "machine_id": cur,
            "machine": machine_label(),
            "userprofile": os.environ.get("USERPROFILE", ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return result


def ensure_local_data(app_dir: Path = None) -> dict:
    """程序启动时调用一次：保证数据目录可用且属于本机。"""
    return purge_foreign_data(app_dir)


# ============================================================
# 四、配置里的用户路径重定位
# ============================================================

def current_user_home() -> Path:
    """当前登录用户的家目录，例如 C:\\Users\\张三。"""
    up = os.environ.get("USERPROFILE")
    if up and Path(up).exists():
        return Path(up)
    return Path.home()


def _relocate_one(path_str: str, home: Path):
    """把 C:\\Users\\别人\\xxx 改写成 C:\\Users\\当前用户\\xxx。

    只在原路径「指向另一个用户的目录」或「根本不存在」时才动它；
    用户自己改到 D 盘之类非用户目录的路径，一律保留不动。
    """
    if not path_str or not isinstance(path_str, str):
        return path_str, False
    p = Path(path_str)
    parts = p.parts
    # 形态：盘符 / Users / 用户名 / ...
    if len(parts) < 3 or parts[1].lower() != "users":
        return path_str, False
    old_user = parts[2]
    cur_user = home.name
    if old_user.lower() == cur_user.lower() and p.exists():
        return path_str, False
    # 用当前用户的家目录拼出同样的相对位置
    try:
        new_p = home.joinpath(*parts[3:]) if len(parts) > 3 else home
    except Exception:
        return path_str, False
    if not new_p.exists():
        # 目标位置在这台电脑上不存在（比如没有 Videos 目录）→ 保留原值，
        # 交给监控逻辑自己跳过，避免凭空造出一堆不存在的目录
        return str(new_p), False
    return str(new_p), True


def relocate_user_paths(cfg: dict) -> dict:
    """就地修正配置里所有指向「别人用户目录」的路径。

    覆盖：监控目录 / 全盘监控根 / 删除保护目录 / 聚合监控目录 / 安装监控目录。
    返回 {'changed': [改动说明]}
    """
    home = current_user_home()
    changed = []

    def fix_map(d: dict):
        for k, v in list(d.items()):
            nv, ok = _relocate_one(v, home)
            if ok:
                d[k] = nv
                changed.append("{}: {} -> {}".format(k, v, nv))

    def fix_list(li):
        for i, v in enumerate(li):
            nv, ok = _relocate_one(v, home)
            if ok:
                li[i] = nv
                changed.append("{} -> {}".format(v, nv))

    if isinstance(cfg.get("监控目录"), dict):
        fix_map(cfg["监控目录"])
    for key in ("全盘监控根", "删除保护目录", "聚合监控目录", "安装监控目录"):
        if isinstance(cfg.get(key), list):
            fix_list(cfg[key])
    return {"changed": changed}


if __name__ == "__main__":
    print("数据目录  :", APP_DIR)
    print("便携模式  :", PORTABLE)
    print("本机指纹  :", MACHINE_ID, "(", machine_label(), ")")
    r = ensure_local_data()
    print("清理外来数据:", r)
