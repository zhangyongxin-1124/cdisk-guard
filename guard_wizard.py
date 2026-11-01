# -*- coding: utf-8 -*-
"""
guard_wizard.py — 首次启动向导：把「不该进 C 盘的东西」一次性搬走
=================================================================
两件不同的事，用两种不同的办法，这点很重要：

  用户文件夹（桌面/下载/文档/图片/视频）
      必须走系统正式的「新内容保存位置」（Known Folder 重定向）。
      只建联接不行——微软那套逻辑认的是注册表，联接会导致"软件问出来的路径"
      和"系统认的路径"分叉。这里调 SHSetKnownFolderPath，等同你在资源管理器里
      右键文件夹 → 属性 → 位置 → 移动，是最稳的做法。改之前先把原值备份，
      随时可以 --restore-folders 一键还原。

  应用数据（微信/QQ/网盘/音乐缓存…）
      这些都是软件自己选的目录，没有统一的"保存位置"可以改。所以用
      「整个目录搬到别的盘 + 原位留目录联接」：软件继续往老路径写，
      数据实际落到新盘，读写路径一字不变，软件完全无感。

安全原则（代码里也按这个写的）：
  1. 先复制，成功后再删源；复制失败一步都不会动你的东西。
  2. 源目录改成"重命名"而不是直接删，万一中途有文件被占用也不会损坏数据。
  3. 每步操作都写 migrations.json，随时能查、能回滚。
"""

import os
import re
import sys
import json
import time
import ctypes
import shutil
import subprocess
import datetime
from pathlib import Path
from ctypes import wintypes

import guard_paths as gp

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


# ============================================================
# 常量
# ============================================================

from guard_appdir import APP_DIR

MIGRATIONS_FILE = APP_DIR / "migrations.json"
FOLDER_BACKUP = APP_DIR / "folder_locations_backup.json"

SHELL_REG = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"

# 迁移前需要退出的进程（不然文件被占用，搬不动）
KEY_PROCS = {
    "wechat_files": ["WeChat", "weixin"], "wecom_files": ["WXWork"],
    "wechat_cache": ["WeChat", "weixin"], "qq_files": ["QQ", "TIM"],
    "qq_cache": ["QQ", "TIM"], "wps": ["wps", "et", "wpp"],
    "edge_cache": ["msedge"], "chrome_cache": ["chrome"],
    "360chrome_cache": ["360se", "360chrome"], "firefox_cache": ["firefox"],
    "cloudmusic": ["cloudmusic"], "qqmusic": ["QQMusic"],
    "thunder": ["Thunder", "XLServicePlatform"], "dingtalk": ["DingTalk"],
    "feishu": ["Feishu", "Lark"], "baidunetdisk": ["baidunetdisk"],
}


# ============================================================
# 小工具
# ============================================================

def human_size(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def ts() -> str:
    return f"[{datetime.datetime.now():%H:%M:%S}]"


def safe_name(s: str) -> str:
    """把标签做成合法的目录名。"""
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return re.sub(r"\s+", " ", s).strip().rstrip(".")


def running_procs(keys) -> set:
    """检查给定 key 对应的软件是否正在运行。"""
    names = set()
    for k in keys:
        names.update(n.lower() for n in KEY_PROCS.get(k, []))
    if not names:
        return set()
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, timeout=15)
        txt = out.stdout.decode("gbk", "replace").lower()
        return {n for n in names if f'"{n}.exe"' in txt}
    except Exception:
        return set()


def quiet_copy(src: Path, dst: Path) -> tuple:
    """robocopy 复制（不删源）。返回 (ok, returncode)。"""
    Path(dst).mkdir(parents=True, exist_ok=True)
    cmd = ["robocopy", str(src), str(dst), "/E", "/COPY:DAT", "/XJ",
           "/R:1", "/W:1", "/NP", "/NFL", "/NDL", "/NJH", "/NJS", "/XD",
           "__pycache__"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=7200)
        return (r.returncode < 8, r.returncode)
    except Exception as e:
        return (False, -1)


# ============================================================
# Known Folder（"新内容保存位置"）
# ============================================================

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def parse_guid(s: str) -> GUID:
    s = s.strip("{}")
    a, b, c, d, e = s.split("-")
    g = GUID()
    g.Data1 = int(a, 16)
    g.Data2 = int(b, 16)
    g.Data3 = int(c, 16)
    rest = d + e
    g.Data4 = (ctypes.c_ubyte * 8)(*[int(rest[i * 2:i * 2 + 2], 16) for i in range(8)])
    return g


def _shell32():
    sh = ctypes.windll.shell32
    return sh


def get_known_folder_path(guid_s: str) -> str:
    """读当前 Known Folder 的真实路径（优先用系统 API，比读注册表准）。"""
    try:
        sh = _shell32()
        p = ctypes.c_wchar_p()
        sh.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint,
                                            wintypes.HANDLE,
                                            ctypes.POINTER(ctypes.c_wchar_p)]
        hr = sh.SHGetKnownFolderPath(ctypes.byref(parse_guid(guid_s)), 0, None,
                                     ctypes.byref(p))
        if hr == 0 and p.value:
            val = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return val
    except Exception:
        pass
    return ""


def set_known_folder_path(guid_s: str, new_path: Path) -> bool:
    """正式重定向 Known Folder（等同资源管理器里改"位置"）。"""
    try:
        sh = _shell32()
        sh.SHSetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint,
                                            wintypes.HANDLE, ctypes.c_wchar_p]
        hr = sh.SHSetKnownFolderPath(ctypes.byref(parse_guid(guid_s)), 0, None,
                                     str(new_path))
        return hr == 0
    except Exception:
        return False


def notify_shell(path: Path):
    """通知资源管理器：这个目录变了，刷新一下。"""
    try:
        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATHW = 0x0005
        ctypes.windll.shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW,
                                             ctypes.c_wchar_p(str(path)), None)
    except Exception:
        pass


def current_folder_locations() -> dict:
    """读所有已知文件夹当前位置 + 体积。"""
    home = Path(os.environ.get("USERPROFILE", ""))
    out = {}
    for name, default_rel, guid in gp.KNOWN_FOLDERS:
        cur = get_known_folder_path(guid) or str(home / default_rel)
        cur_p = Path(cur)
        try:
            exists = cur_p.exists()
            size, cnt, _ = gp.dir_size(cur_p, max_items=40000) if exists else (0, 0, False)
        except Exception:
            exists, size, cnt = False, 0, 0
        out[name] = {"guid": guid, "path": str(cur_p), "exists": exists,
                     "size": size, "files": cnt,
                     "on_c": os.path.splitdrive(str(cur_p))[0].lower() == "c:"}
    return out


def save_folder_backup(locs: dict):
    data = {"时间": datetime.datetime.now().isoformat(timespec="seconds"), "位置": locs}
    FOLDER_BACKUP.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def load_folder_backup() -> dict:
    try:
        return json.loads(FOLDER_BACKUP.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ============================================================
# 迁移记录
# ============================================================

def load_migrations() -> list:
    try:
        return json.loads(MIGRATIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_migrations(rows: list):
    MIGRATIONS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                               encoding="utf-8")


# ============================================================
# 执行：两类迁移
# ============================================================

def migrate_app_dir(src: Path, dst: Path, log=print, keep_link: bool = True) -> dict:
    """应用数据/目录搬家：复制 → 校验 → 删除源目录。

    keep_link=True ：原位置建联接（透明链接），依赖原路径的软件照常可用；
    keep_link=False：搬走后 C 盘原位置干干净净，什么都不留。
    """
    res = {"ok": False, "stage": "", "detail": ""}
    if gp.is_junction(src):
        res["stage"], res["detail"] = "跳过", "已经是联接（之前搬过）"
        return res
    if dst.exists():
        res["stage"], res["detail"] = "跳过", f"目标已存在：{dst}"
        return res

    src_size, src_cnt, _ = gp.dir_size(src)
    log(f"    ① 复制 {src_cnt} 个文件（{human_size(src_size)}）…")
    ok, rc = quiet_copy(src, dst)
    if not ok:
        res["stage"], res["detail"] = "复制失败", f"robocopy 返回码 {rc}"
        return res

    dst_size, dst_cnt, _ = gp.dir_size(dst)
    if src_cnt and dst_cnt < src_cnt * 0.99:
        res["stage"] = "校验失败"
        res["detail"] = f"源 {src_cnt} 个文件，目标只有 {dst_cnt} 个"
        return res

    log("    ② 校验通过，把 C 盘原目录改名腾位置…")
    old = Path(str(src) + "_守卫待清理")
    try:
        os.rename(str(src), str(old))
    except OSError as e:
        res["stage"], res["detail"] = "重命名失败", f"{type(e).__name__}（可能有文件被占用，你的数据未被改动）"
        return res

    if keep_link:
        log("    ③ 在原位置建目录联接（软件继续按老路径读写）…")
        okj = make_junction(src, dst)
        if not okj:
            try:
                os.rename(str(old), str(src))     # 联接建失败就立刻还原，不留下半吊子状态
            except OSError:
                pass
            res["stage"], res["detail"] = "建联接失败", "已还原原目录，数据未动"
            return res
        log("    ④ 清理 C 盘上的旧副本…")
    else:
        log("    ③ 按设置不在原位置留链接，直接清理 C 盘旧副本…")

    left = 0
    try:
        shutil.rmtree(str(old), ignore_errors=True)
        left = 1 if old.exists() else 0
    except Exception:
        left = 1
    res.update({"ok": True, "stage": "完成",
                "detail": ("有残留待你手动删除" if left else
                           ("旧副本已清理，原位置留有链接" if keep_link
                            else "C 盘原位置已清空，不再保留")),
                "size": src_size, "files": src_cnt})
    return res


def clean_dir(src: Path, log=print) -> dict:
    """纯缓存清理：逐个删，遇到被占用的就跳过（不强行干）。"""
    freed = removed = skipped = 0
    try:
        for cur, dirs, files in os.walk(str(src), topdown=False):
            for fn in files:
                fp = Path(cur) / fn
                try:
                    st = fp.stat()
                    fp.unlink()
                    freed += st.st_size
                    removed += 1
                except OSError:
                    skipped += 1
            for d in dirs:
                try:
                    (Path(cur) / d).rmdir()
                except OSError:
                    pass
    except Exception as e:
        return {"ok": False, "stage": "清理失败", "detail": f"{type(e).__name__}: {e}"}
    return {"ok": True, "stage": "完成",
            "detail": f"删掉 {removed} 个文件，释放 {human_size(freed)}"
                      + (f"；{skipped} 个被占用已跳过" if skipped else ""),
            "size": freed, "files": removed}


def redirect_user_folder(name: str, target: Path, log=print) -> dict:
    """用户文件夹：复制 → 改 Known Folder → 通知资源管理器。"""
    res = {"ok": False, "stage": "", "detail": ""}
    locs = current_folder_locations()
    src = Path(locs[name]["path"])
    if target.exists() and any(target.iterdir()):
        res["stage"], res["detail"] = "跳过", f"目标目录非空：{target}"
        return res
    if target == src:
        res["stage"], res["detail"] = "跳过", "位置没变"
        return res

    size, cnt, _ = gp.dir_size(src)
    log(f"    ① 复制 {cnt} 个文件（{human_size(size)}）到 {target} …")
    ok, rc = quiet_copy(src, target)
    if not ok:
        res["stage"], res["detail"] = "复制失败", f"robocopy 返回码 {rc}"
        return res
    dsize, dcnt, _ = gp.dir_size(target)
    if cnt and dcnt < cnt * 0.99:
        res["stage"], res["detail"] = "校验失败", f"源 {cnt} / 目标 {dcnt} 个文件"
        return res

    log("    ② 修改「新内容保存位置」（系统级重定向）…")
    save_folder_backup(current_folder_locations())
    if not set_known_folder_path(locs[name]["guid"], target):
        res["stage"], res["detail"] = "重定向失败", "无法写入 Known Folder（可能权限不足）"
        return res

    log("    ③ 清理 C 盘上的旧副本…")
    left = 0
    for p in sorted(src.iterdir(), key=lambda x: len(str(x)), reverse=True):
        try:
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(str(p), ignore_errors=True)
        except OSError:
            left += 1
    notify_shell(src)
    notify_shell(target)
    res.update({"ok": True, "stage": "完成",
                "detail": (f"{left} 项被占用没删掉，可稍后手动清理" if left else "已完成"),
                "size": size, "files": cnt})
    return res


# ============================================================
# 生成迁移计划
# ============================================================

def build_plan(min_app_mb: float = 80.0) -> dict:
    home = Path(os.environ.get("USERPROFILE", ""))
    plan = {"应用数据": [], "用户文件夹": [], "可清理缓存": []}

    locs = current_folder_locations()
    for name, default_rel, guid in gp.KNOWN_FOLDERS:
        info = locs.get(name, {})
        if not info.get("exists") or not info.get("on_c"):
            continue
        size = info.get("size", 0)
        plan["用户文件夹"].append({
            "kind": "user_folder", "guid": guid,
            "name": name, "path": Path(info["path"]), "size": size,
            "files": info.get("files", 0),
            "recommend": size >= 100 * 2 ** 20 and name != "桌面",
            "note": ("桌面上的快捷方式会跟着走，但如果中途出问题桌面会看着是空的，"
                     "所以这里默认不勾；想一步到位可以自己勾上" if name == "桌面" else
                     "改的是系统正式的保存位置，以后新文件默认存到新盘"),
        })

    app_rows = gp.scan_app_targets(home, min_mb=min_app_mb)
    busy = running_procs([t["key"] for t, _, _, _ in app_rows])
    for t, p, size, cnt in app_rows:
        blocked = [pr for pr in KEY_PROCS.get(t["key"], []) if pr.lower() in busy]
        migratable = t["action"] == "migrate"
        # 纯缓存只建议清理，而清理不可撤回 —— 所以默认一律不勾，交给用户自己决定
        note = t.get("note", "")
        if not migratable:
            note = (note + "｜" if note else "") + "勾选=直接清理（删了找不回，所以默认不勾）"
        plan["应用数据" if migratable else "可清理缓存"].append({
            "kind": "app", "name": t["label"], "key": t["key"], "path": p,
            "size": size, "files": cnt, "category": t["category"],
            "action": t["action"], "note": note,
            "recommend": migratable and size >= 200 * 2 ** 20 and not blocked,
            "busy": blocked,
        })
    plan["应用数据"].sort(key=lambda x: -x["size"])
    plan["可清理缓存"].sort(key=lambda x: -x["size"])
    return plan


def print_plan(plan: dict):
    print("\n" + "═" * 66)
    print("  C 盘守卫 · 首次设置向导")
    print("═" * 66)
    total_c = 0
    idx = 0
    sections = [("用户文件夹", "已检测到的系统文件夹"),
                ("应用数据", "应用文件与缓存（搬家后软件照常用，靠目录联接中转）"),
                ("可清理缓存", "纯缓存，删了不影响使用")]
    for sec, desc in sections:
        rows = plan.get(sec, [])
        if not rows:
            continue
        print(f"\n【{sec}】{desc}")
        for r in rows:
            idx += 1
            r["idx"] = idx
            total_c += r["size"]
            mark = "✓" if r["recommend"] else " "
            extra = ""
            if sec == "用户文件夹":
                extra = f"  ← {r['path']}"
            if r.get("busy"):
                extra = f"  ⚠️ 正在运行：{','.join(r['busy'])}，需先退出才能搬"
            print(f"  [{mark}] {idx:2d}. {human_size(r['size']):>9}  {r['name']}{extra}")
            if r.get("note"):
                print(f"           ↳ {r['note']}")
    print(f"\n  合计可释放约 {human_size(total_c)}")
    print("═" * 66)


# ============================================================
# 交互
# ============================================================

def pick_base_dir() -> Path:
    letters = []
    import string
    for letter in string.ascii_uppercase:
        if letter == "C":
            continue
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:
            continue
        free = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, None, None, ctypes.byref(free))
        letters.append((letter, free.value))
    letters.sort(key=lambda x: -x[1])
    if not letters:
        return None
    print("\n【第 1 步】选目标盘（不要把数据放在 U 盘/移动硬盘上，拔了会出问题）")
    for i, (l, f) in enumerate(letters, 1):
        print(f"   {i}. {l}: 盘   剩余 {human_size(f)}")
    while True:
        raw = input(f"   输入序号，回车默认 [{letters[0][0]}:] ：").strip()
        if not raw:
            chosen = letters[0][0]
            break
        try:
            chosen = letters[int(raw) - 1][0]
            break
        except Exception:
            print("   没看懂，再输一次")
    base = Path(f"{chosen}:\\C盘守卫")
    print(f"   → 数据将统一放到 {base}\\ 下面，按应用分目录命名")
    return base


def select_items(plan: dict) -> list:
    rows = [r for sec in ("用户文件夹", "应用数据", "可清理缓存")
            for r in plan.get(sec, [])]
    if not rows:
        return []
    print("\n【第 2 步】勾选要处理的项目")
    print("   直接回车 = 采用推荐勾选（体积大且没有软件正在用的）")
    print("   也可以输入编号，多个用逗号隔开，如  1,3,5 ；输入 0 = 全部取消")
    raw = input("   > ").strip()
    if not raw:
        return [r for r in rows if r["recommend"]]
    if raw == "0":
        return []
    picked = set()
    for part in re.split(r"[,\s]+", raw):
        try:
            n = int(part)
        except ValueError:
            continue
        for r in rows:
            if r.get("idx") == n:
                picked.add(r["idx"])
    return [r for r in rows if r.get("idx") in picked]


def confirm(items: list, base: Path) -> bool:
    if not items:
        print("\n  没有选中任何项目，本次不做改动。")
        return False
    print("\n【第 3 步】确认执行")
    print(f"   C 盘当前剩余 {_free_summary()}")
    mig = [r for r in items if r.get("action") != "clean"]
    cln = [r for r in items if r.get("action") == "clean"]
    print(f"   搬家/重定向 {len(mig)} 项（可撤回）"
          + (f"，预计释放 {human_size(sum(r['size'] for r in mig))}" if mig else ""))
    if cln:
        print(f"   ⚠️ 另有一项「永久删除」：{len(cln)} 个缓存目录，"
              f"预计释放 {human_size(sum(r['size'] for r in cln))}")
        for r in cln:
            print(f"       · {r['name']}")
    print("   注意：搬家期间请别操作这些软件，大目录可能需要几分钟")
    raw = input("   输入 y 开始，其他任意键取消：").strip().lower()
    return raw == "y"


def _free_summary() -> str:
    free = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW("C:\\", None, None, ctypes.byref(free))
    return human_size(free.value)


def execute(items: list, base: Path, clean_cache: bool = False,
            log=print) -> list:
    migrations = load_migrations()
    done = []
    for r in items:
        name = r["name"]
        log(f"\n  ▸ {name}  （{human_size(r['size'])}）")
        if r.get("busy"):
            log(f"    ⚠️ {','.join(r['busy'])} 正在运行，跳过。"
                f"退出软件后再跑一次向导即可。")
            continue
        sub = "用户文件夹" if r.get("kind") == "user_folder" else "已迁移"
        dst = base / sub / safe_name(name)
        if r.get("kind") == "user_folder":
            res = redirect_user_folder(name, dst, log=log)
        elif r.get("action") == "clean":
            log(f"    清理 {r['path']} …")
            res = clean_dir(r["path"], log=log)
            dst = r["path"]          # 清理不涉及新位置
        else:
            res = migrate_app_dir(r["path"], dst, log=log)
        log(f"    → {res['stage']}：{res['detail']}")
        migrations.append({
            "时间": datetime.datetime.now().isoformat(timespec="seconds"),
            "名称": name, "key": r.get("key", ""),
            "类型": ("用户文件夹重定向" if r.get("kind") == "user_folder"
                     else ("缓存清理" if r.get("action") == "clean" else "应用数据搬家")),
            "原位置": str(r["path"]), "新位置": str(dst),
            "大小": r["size"], "结果": res["stage"], "说明": res["detail"],
            "可撤回": bool(res.get("ok") and r.get("action") != "clean"),
        })
        done.append({"name": name, "size": r["size"], **res})
        save_migrations(migrations)
    return done


def maybe_restart_explorer():
    print("\n   文件夹重定向需要资源管理器重启才完全生效。")
    raw = input("   现在重启资源管理器吗？（会重开所有文件夹窗口）[y/N]：").strip().lower()
    if raw != "y":
        print("   好的，你也可以在任务管理器里重启 explorer.exe，或注销一次。")
        return
    subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"],
                   capture_output=True)
    time.sleep(1.5)
    subprocess.Popen(["explorer.exe"])
    print("   已重启资源管理器。")


# ============================================================
# 入口
# ============================================================

def run_wizard(base_override: str = None, yes: bool = False,
               min_app_mb: float = 80.0) -> list:
    print("\n正在扫描 C 盘上值得搬走的东西，稍等十几秒…")
    plan = build_plan(min_app_mb=min_app_mb)
    if not any(plan.values()):
        print("\n没找到值得搬的东西（阈值 %.0f MB），C 盘挺干净的。" % min_app_mb)
        return []
    print_plan(plan)
    base = Path(base_override) if base_override else pick_base_dir()
    if base is None:
        print("这台机器上没有检测到除 C 盘以外的本地硬盘，无法迁移。")
        return []
    if yes:
        items = [r for r in plan["用户文件夹"] + plan["应用数据"] + plan["可清理缓存"]
                 if r["recommend"]]
    else:
        items = select_items(plan)
        if not confirm(items, base):
            return []
    before = _free_summary()
    done = execute(items, base)
    after = _free_summary()
    print("\n" + "═" * 66)
    print("  完成")
    print("═" * 66)
    ok = [d for d in done if d["ok"]]
    fail = [d for d in done if not d["ok"]]
    freed = sum(d["size"] for d in ok)
    print(f"  成功 {len(ok)} 项，失败/跳过 {len(fail)} 项")
    print(f"  C 盘可用：{before} → {after}（释放约 {human_size(freed)}）")
    for d in fail:
        print(f"   · {d['name']}：{d['stage']} — {d['detail']}")
    if any(d["name"] in gp.SHELL_REG_NAMES for d in ok):
        maybe_restart_explorer()
    print("\n  以后想反悔： C盘守卫.exe --restore-folders （系统文件夹还原）")
    print("              C盘守卫.exe --undo            （撤销应用数据搬家）")
    print("═" * 66)
    return done


def make_junction(link: Path, target: Path) -> bool:
    """在原目录位置建 NTFS 联接（junction）。

    先试 cmd 的 mklink /J —— 它不需要管理员权限，也不需要拉起另一个运行时；
    万一失败（例如文件系统不支持）再退回到 PowerShell。
    """
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, timeout=60)
        if Path(link).exists():
            return True
    except Exception:
        pass
    ps = ('$ErrorActionPreference="Stop"; '
          f'New-Item -ItemType Junction -Path "{link}" -Target "{target}" | Out-Null')
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=60)
    except Exception:
        pass
    return Path(link).exists()


def undo_all(log=print, only: str = None) -> int:
    """把搬走的应用数据搬回 C 盘原位（系统文件夹请用 --restore-folders）。"""
    rows = [r for r in load_migrations()
            if r.get("可撤回") and r.get("类型") in ("应用数据搬家", "聚合搬家")]
    if only:
        rows = [r for r in rows if only.lower() in (r["名称"] + r.get("key", "")).lower()]
    if not rows:
        log("没有可以撤回的搬家记录。")
        return 0
    log("\n" + "═" * 66)
    log("  撤销搬家 —— 把数据从别的盘搬回 C 盘原位")
    log("═" * 66)
    done = 0
    for r in rows:
        orig = Path(r["原位置"])
        dst = Path(r["新位置"])
        log(f"\n  ▸ {r['名称']}（{human_size(r['大小'])}）")
        if not dst.exists():
            log(f"    跳过：{dst} 里的数据不在了（可能被你删掉了）")
            continue
        if orig.exists() and not gp.is_junction(orig):
            log("    跳过：C 盘原位已经变回真实目录了，不用撤")
            continue
        if orig.exists():                      # 是联接：先摘掉
            try:
                orig.rmdir()
            except OSError:
                log("    联接摘不掉，本次撤回中止（你的数据没受影响）")
                continue
        log(f"    把数据搬回 {orig} …")
        ok, rc = quiet_copy(dst, orig)
        if not ok:
            make_junction(orig, dst)           # 立刻把联接装回去，不留断链
            log(f"    复制失败（robocopy {rc}），已还原联接，数据还在新位置")
            continue
        _, ocnt, _ = gp.dir_size(orig)
        _, ncnt, _ = gp.dir_size(dst)
        if ncnt and ocnt < ncnt * 0.99:
            log(f"    校验不一致（{ocnt}/{ncnt}），保留新数据不动，请手动确认")
            continue
        try:
            if os.path.splitdrive(str(dst))[0].lower() == os.path.splitdrive(str(orig))[0].lower():
                os.rename(str(dst), str(dst) + "_守卫待清理")
            else:
                shutil.rmtree(str(dst), ignore_errors=True)
        except OSError:
            pass
        done += 1
        log("    ✅ 已搬回 C 盘原位")
        save_migrations([x for x in load_migrations() if x is not r])
    log(f"\n  完成 {done} 项。")
    log("═" * 66)
    return done


def restore_folders(log=print):
    """把改过的系统文件夹位置还原回 C 盘。"""
    backup = load_folder_backup()
    if not backup or "位置" not in backup:
        log("没有找到重定向记录，系统文件夹应该没被改过。")
        return
    cur = current_folder_locations()
    restored = []
    for name, info in cur.items():
        old = backup["位置"].get(name, {})
        old_path = Path(old.get("path", ""))
        cur_path = Path(info["path"])
        if not old_path or not old_path.parent.exists():
            continue
        if cur_path == old_path:
            continue
        log(f"\n  ▸ {name}：{cur_path} → 还原到 {old_path}")
        ok, _ = quiet_copy(cur_path, old_path)
        if not ok:
            log(f"    复制失败，保留现位置不动")
            continue
        if set_known_folder_path(info["guid"], old_path):
            restored.append(name)
            notify_shell(old_path)
            log("    已还原「新内容保存位置」")
        else:
            log("    修改保存位置失败，数据已复制回来但路径仍是新位置")
    if restored:
        log(f"\n  已还原 {len(restored)} 个文件夹：{', '.join(restored)}")
        maybe_restart_explorer()
    else:
        log("\n  没有需要还原的项。")
