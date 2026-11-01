# -*- coding: utf-8 -*-
"""方案C清理：将用户确认的缓存/旧版本移动到 F 盘暂存区（不删除任何文件）"""
import os, shutil, time
from pathlib import Path

STASH = Path(r"F:\Workbuddy 存储数据库\清理暂存_20261031")
U = Path(r"C:\Users\HIAPAD\AppData\Local")

JOBS = {
    "A_WPS旧版本": [
        U / "Kingsoft/WPS Office/11.1.0.10495",
        U / "Kingsoft/WPS Office/11.1.0.10700",
        U / "Kingsoft/WPS Office/11.1.0.9912",
    ],
    "B_更新器缓存": [
        U / "@genieworkbuddy-desktop-updater",
        U / "kimi-desktop-updater",
    ],
    "C_开发浏览器系统缓存": [
        U / "uv/cache",
        U / "Microsoft/Edge/User Data/Default/Cache",
    ],
    "D_豆包缓存": [
        U / "Doubao/User Data/hot_fix",
        U / "Doubao/User Data/gecko_cache",
    ],
}

def move_tree(src: Path, dst: Path, moved: list, skipped: list):
    """递归移动；锁定文件跳过并记录。返回移动字节数。"""
    total = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        s, d = Path(entry.path), dst / entry.name
        try:
            if entry.is_dir(follow_symlinks=False):
                total += move_tree(s, d, moved, skipped)
                try:
                    s.rmdir()
                except OSError:
                    skipped.append(str(s))
            else:
                shutil.move(str(s), str(d))
                moved.append(entry.name)
                total += entry.stat().st_size if False else 0  # size counted below
        except (PermissionError, OSError) as e:
            skipped.append(f"{entry.path} ({type(e).__name__})")
    return total

def dir_size(p: Path) -> int:
    t = 0
    for cur, dirs, files in os.walk(p):
        for f in files:
            try: t += os.path.getsize(os.path.join(cur, f))
            except OSError: pass
    return t

print("=" * 60)
grand = 0
for group, items in JOBS.items():
    gdst = STASH / group
    gdst.mkdir(parents=True, exist_ok=True)
    for src in items:
        if not src.exists():
            print(f"[跳过] 不存在: {src}")
            continue
        before = dir_size(src)
        moved, skipped = [], []
        if src.name == "Temp" or src.name == "cache" and "Edge" in str(src):
            # 目录整体可能被锁：移动内部内容
            total = move_tree(src, gdst / src.name, moved, skipped)
        else:
            # 整目录移动（同名冲突则加后缀）
            dst = gdst / src.name
            i = 1
            while dst.exists():
                dst = gdst / f"{src.name}({i})"
                i += 1
            try:
                shutil.move(str(src), str(dst))
                total = before
                moved = ["(整目录)"]
            except (PermissionError, OSError) as e:
                skipped = [f"{src} ({type(e).__name__})"]
                total = 0
        grand += total
        gb = total / 2**30
        print(f"[{'OK' if gb > 0.001 else '部分'}] {src.name}: 释放 {gb:.2f} GB"
              f" | 移动 {len(moved)} 项 | 跳过 {len(skipped)} 项(锁定)")
        for s in skipped[:5]:
            print(f"        锁定: {s}")

free_after = shutil.disk_usage("C:\\").free / 2**30
print("=" * 60)
print(f"合计释放: {grand/2**30:.2f} GB | C 盘当前剩余: {free_after:.2f} GB")
print(f"暂存备份位置: {STASH}（观察 2 周无异常后可自行删除）")
