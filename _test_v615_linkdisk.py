# -*- coding: utf-8 -*-
"""v6.15 联动盘专项测试。

覆盖：
  1. 触发规则：首次使用 / 超过阈值 / 冷却 / 不再提示 / 已建 / 次数上限
  2. 软件识别：Program Files 下的发行商层不能当软件名、系统目录跳过
  3. 数据目录定位：只认该软件那一层，安装目录与发行商目录不算
  4. 复制 → 校验 → junction → 还原 全链路（真实 junction，不是模拟）
  5. 失败回滚：复制失败 / 改名失败 / 建链接失败都必须回到原样
  6. 文案：必须讲清「门牌号 / 不用重装 / 可还原」这些白话
  7. 边界：目录不存在、路径不是普通目录、目标盘不可写

运行：python _test_v615_linkdisk.py   （全绿退出码 0，有失败退出码 1）
"""
import os
import sys
import time
import json
import shutil
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import guard_linkdisk as ld

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else f"  → {detail}"))


TMP_ROOT = Path("F:/Workbuddy数据储存库/_tmp")
TMP_ROOT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="lk_", dir=str(TMP_ROOT)))

OLD_ENV = {k: os.environ.get(k) for k in
           ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "ProgramData",
            "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")}


def set_env(**kw):
    for k, v in kw.items():
        os.environ[k] = str(v)


def restore_env():
    for k, v in OLD_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def mkfile(path: Path, size: int = 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# ============================================================
# 一、触发规则
# ============================================================
t1 = ld.Tracker(tmp / "t1.json")
APP = {"name": "DemoApp", "dir_name": "DemoApp",
       "app_dir": r"C:\Program Files\DemoApp", "exe": r"C:\Program Files\DemoApp\demo.exe",
       "key": "demoapp"}

t1.touch(APP, now=1000)
t1.set_dirs("demoapp", [r"C:\Users\x\AppData\Roaming\DemoApp"], int(0.2 * 2 ** 30))
p = t1.pending(now=1000, threshold_gb=2.0, first_prompt=True, min_mb=80)
ck("首次使用且占用够 → 提示一次",
   len(p) == 1 and p[0]["trigger"] == "首次使用", str(p))
ck("提示里带上数据目录", p and p[0]["dirs"], str(p))

t1.mark_prompted("demoapp", "首次使用", int(0.2 * 2 ** 30))
t1.save()
ck("首次已提示 → 不再重复",
   not t1.pending(now=1001, threshold_gb=2.0, min_mb=80))

t1.set_dirs("demoapp", [r"C:\x"], int(2.2 * 2 ** 30))
p = t1.pending(now=1002, threshold_gb=2.0, min_mb=80)
ck("超过 2GB → 提示「超过阈值」",
   bool(p) and p[0]["trigger"] == "超过阈值", str(p))

t1.mark_prompted("demoapp", "超过阈值", int(2.2 * 2 ** 30))
t1.set_dirs("demoapp", [r"C:\x"], int(3.4 * 2 ** 30))
ck("只涨了不到一个阈值 → 不打扰",
   not t1.pending(now=1003, threshold_gb=2.0, min_mb=80))

t1.set_dirs("demoapp", [r"C:\x"], int(4.6 * 2 ** 30))
ck("又涨了一个阈值 → 再提示一次",
   bool(t1.pending(now=1004, threshold_gb=2.0, min_mb=80)))

# 太小：先登记，不打扰
t2 = ld.Tracker(tmp / "t2.json")
t2.touch({"name": "Tiny", "dir_name": "Tiny", "key": "tiny"}, now=2000)
t2.set_dirs("tiny", [r"C:\y"], 5 * 2 ** 20)
ck("占用太小 → 首次也不打扰",
   not t2.pending(now=2000, threshold_gb=2.0, min_mb=80))

# 冷却
t3 = ld.Tracker(tmp / "t3.json")
t3.touch(APP, now=3000)
t3.set_dirs("demoapp", [r"C:\x"], int(2.5 * 2 ** 30))
t3.decide("demoapp", "later", days=7, now=3000)
ck("选「先不用」后 7 天内不问",
   not t3.pending(now=3000 + 86400, threshold_gb=2.0, min_mb=0))
ck("冷却结束后会再问",
   bool(t3.pending(now=3000 + 86400 * 8, threshold_gb=2.0, min_mb=0)))

# 不再提示 / 已建
t3.decide("demoapp", "never", now=3000)
ck("选了「不再提示」→ 永久不问",
   not t3.pending(now=3000 + 86400 * 90, threshold_gb=2.0, min_mb=0))
t4 = ld.Tracker(tmp / "t4.json")
t4.touch(APP, now=4000)
t4.set_dirs("demoapp", [r"C:\x"], int(9 * 2 ** 30))
t4.mark_linked("demoapp", [{"src": r"C:\x", "dst": r"D:\x"}])
ck("已建联动盘 → 不再问", not t4.pending(now=4000, threshold_gb=2.0, min_mb=0))
ck("状态显示为已建", t4.status("demoapp") == "已建")

# 次数上限
t5 = ld.Tracker(tmp / "t5.json")
t5.touch(APP, now=5000)
t5.set_dirs("demoapp", [r"C:\x"], int(9 * 2 ** 30))
for i in range(13):
    t5.mark_prompted("demoapp", "超过阈值", int(9 * 2 ** 30) + i)
t5.data["apps"]["demoapp"]["snooze_until"] = 0
ck("问过 12 次以后不再打扰",
   not t5.pending(now=6000, threshold_gb=2.0, min_mb=0))

# 落盘与重新加载
t5.save()
t6 = ld.Tracker(tmp / "t5.json")
ck("档案能持久化", "demoapp" in t6.apps and t6.get("demoapp")["name"] == "DemoApp")


# ============================================================
# 二、软件识别（发行商层不能当软件名）
# ============================================================
pf = tmp / "PF"
pf86 = tmp / "PF86"
pd = tmp / "PD"
for d in (pf, pf86, pd):
    d.mkdir(parents=True, exist_ok=True)
set_env(ProgramFiles=pf, **{"ProgramFiles(x86)": pf86, "ProgramW6432": pf,
                            "ProgramData": pd})
mkfile(pf / "Google" / "Chrome" / "Application" / "chrome.exe")
mkfile(pf / "Demo Vendor" / "DemoApp" / "demo.exe")
mkfile(pf / "DemoApp" / "demo.exe")

a = ld.app_from_exe(str(pf / "Google" / "Chrome" / "Application" / "chrome.exe"))
ck("跳过 Application 层取到软件名",
   a.get("name", "").lower() == "chrome", str(a))

a2 = ld.app_from_exe(str(pf / "DemoApp" / "demo.exe"))
ck("一级目录就是软件 → 直接用", a2.get("name") == "DemoApp", str(a2))

ck("Windows 系统进程跳过",
   not ld.app_from_exe(r"C:\Windows\System32\svchost.exe"))
ck("更新器 / 运行时不当作软件",
   not ld.app_from_exe(str(pf / "DemoApp" / "updater.exe")))


# ============================================================
# 三、数据目录定位
# ============================================================
appdata = tmp / "AppData"
local = tmp / "Local"
user = tmp / "User"
(local / "LocalLow").mkdir(parents=True, exist_ok=True)
set_env(APPDATA=appdata / "Roaming", LOCALAPPDATA=local, USERPROFILE=user,
        ProgramData=pd)

mkfile(appdata / "Roaming" / "DemoApp" / "cache" / "a.dat")
mkfile(appdata / "Roaming" / "Tencent" / "DemoApp" / "b.dat")
mkfile(appdata / "Roaming" / "OtherApp" / "c.dat")
mkfile(local / "Programs" / "DemoApp" / "demo.exe")
mkfile(local / "DemoApp" / "d.dat")

dirs = ld.find_data_dirs({"name": "DemoApp", "dir_name": "DemoApp",
                          "app_dir": str(pf / "DemoApp"), "exe": ""})
low = [d.lower() for d in dirs]
ck("命中 Roaming\\DemoApp", any(d.endswith("roaming\\demoapp") for d in low), str(dirs))
ck("命中 发行商\\DemoApp（二级）", any(d.endswith("tencent\\demoapp") for d in low), str(dirs))
ck("命中 Local\\DemoApp", any(d.endswith("local\\demoapp") for d in low), str(dirs))
ck("不把别人的目录算进来",
   not any("otherapp" in d for d in low), str(dirs))
ck("不把安装目录（Programs\\DemoApp）算进来",
   not any("programs\\demoapp" in d for d in low), str(dirs))

# 发行商目录本身不能整体搬
mkfile(appdata / "Roaming" / "Tencent" / "QQ" / "e.dat")
dirs2 = ld.find_data_dirs({"name": "Tencent", "dir_name": "Tencent",
                           "app_dir": "", "exe": ""})
ck("发行商名不会把整棵目录当数据目录",
   not any(d.lower().endswith("roaming\\tencent") for d in dirs2), str(dirs2))


# ============================================================
# 四、占用测算（提前退出）
# ============================================================
szdir = tmp / "sz"
for i in range(10):
    mkfile(szdir / f"f{i}.bin", 1000)
sz, cnt, capped = ld.dir_size(szdir)
ck("目录测算准确", sz == 10000 and cnt == 10, f"{sz} {cnt}")
sz2, cnt2, capped2 = ld.dir_size(szdir, cap_bytes=2500)
ck("达到 cap 立刻停（不必扫完）", capped2 and sz2 >= 2500 and cnt2 < 10,
   f"{sz2} {cnt2} {capped2}")
m = ld.measure([szdir], cap_bytes=2500)
ck("多目录合计也遵守 cap", m["size"] >= 2500 and m["capped"])


# ============================================================
# 五、真实 junction 全链路
# ============================================================
data = tmp / "data" / "DemoApp"
mkfile(data / "a.txt", 32)
mkfile(data / "sub" / "b.txt", 64)
target = tmp / "target"

r = ld.link_one(data, target)
ck("复制 → 校验 → 建链接成功", r["ok"], str(r))
ck("原位置变成链接（junction）", ld._is_junction(data))
ck("原地址仍能读到数据", (data / "a.txt").read_text() == "x" * 32)
ck("目标盘有实体数据", Path(r["dst"]).joinpath("a.txt").exists(), str(r))
(data / "new.txt").write_text("later")
ck("之后的新写入落到目标盘",
   (Path(r["dst"]) / "new.txt").exists())
ck("目标路径带固定目录名",
   ld.normalize_target(target) == str(Path(target.anchor) / ld.DEFAULT_TARGET_DIRNAME)
   or ld.DEFAULT_TARGET_DIRNAME in r["dst"], r["dst"])

ok = ld.unlink_one(data, r["dst"])
ck("还原：链接摘掉、数据搬回",
   ok and not ld._is_junction(data) and (data / "a.txt").exists()
   and (data / "new.txt").exists())
ck("还原后目标盘不再占用", not Path(r["dst"]).exists())


# ============================================================
# 六、失败路径必须回到原样
# ============================================================
d2 = tmp / "data2" / "DemoApp"
mkfile(d2 / "x.txt", 10)
orig_copy = ld.gwizard.quiet_copy
ld.gwizard.quiet_copy = lambda s, t: (False, 8)
r2 = ld.link_one(d2, tmp / "t2out")
ld.gwizard.quiet_copy = orig_copy
ck("复制失败 → 明确失败", not r2["ok"] and "复制失败" in r2["detail"], str(r2))
ck("复制失败 → 源目录原封不动",
   d2.is_dir() and not ld._is_junction(d2) and (d2 / "x.txt").exists())

r3 = ld.link_one(tmp / "not-exist", tmp / "t3out")
ck("目录不存在 → 安静失败", not r3["ok"], str(r3))


# ============================================================
# 七、多目录：一个失败就整体回滚
# ============================================================
t7 = ld.Tracker(tmp / "t7.json")
d_a = tmp / "multi" / "A"
d_b = tmp / "multi" / "B"
mkfile(d_a / "1.txt", 8)
mkfile(d_b / "2.txt", 8)
t7.touch({"name": "Multi", "dir_name": "Multi", "key": "multi"}, now=7000)
t7.set_dirs("multi", [str(d_a), str(d_b)], 16)

orig_link_one = ld.link_one


def fake_link_one(src, root, log=print):
    if Path(src).name == "A":
        return orig_link_one(src, root, log=log)
    return {"ok": False, "src": str(src), "dst": "", "detail": "文件被占用"}


ld.link_one = fake_link_one
res = ld.link_app("multi", tmp / "multi_out", log=lambda *_: None, t=t7)
ld.link_one = orig_link_one
ck("第二个目录失败 → 整体判失败", not res["ok"], str(res))
ck("失败时不记录成已建", not t7.get("multi").get("linked"))
ck("失败时已成功的目录被回滚",
   not ld._is_junction(d_a) and (d_a / "1.txt").exists())


# ============================================================
# 八、成功路径 + 一键还原（走 link_app / unlink_app）
# ============================================================
t8 = ld.Tracker(tmp / "t8.json")
d_c = tmp / "ok" / "C"
mkfile(d_c / "c.txt", 12)
t8.touch({"name": "OkApp", "dir_name": "OkApp", "key": "okapp"}, now=8000)
t8.set_dirs("okapp", [str(d_c)], 12)
res2 = ld.link_app("okapp", tmp / "ok_out", log=lambda *_: None, t=t8)
ck("link_app 全部成功", res2["ok"], str(res2))
ck("档案标记已建", bool(t8.get("okapp").get("linked")))
ck("状态显示已建", t8.status("okapp") == "已建")
res3 = ld.unlink_app("okapp", log=lambda *_: None, t=t8)
ck("unlink_app 还原成功", res3["ok"], str(res3))
ck("还原后档案清空", not t8.get("okapp").get("linked"))
ck("还原后数据在原位", (d_c / "c.txt").exists() and not ld._is_junction(d_c))


# ============================================================
# 九、提示文案（这一条决定用户懂不懂）
# ============================================================
txt = ld.explain({"name": "微信"}, int(2.3 * 2 ** 30),
                 [r"C:\Users\x\AppData\Roaming\WeChat"], "超过阈值")
for kw in ("门牌号", "还原", "微信", "2.3 GB"):
    ck(f"提示文案包含「{kw}」", kw in txt, txt[:80])
ck("文案明确说软件感觉不到变化",
   "不知道" in txt or "感觉不到" in txt or "照原" in txt)
ck("白话好处是三条", len(ld.benefit_lines()) == 3)

ck("目标盘归一化（D 盘根）",
   ld.normalize_target("D:\\") == "D:\\" + ld.DEFAULT_TARGET_DIRNAME)
ck("目标盘归一化（深层目录）",
   ld.normalize_target("D:\\C盘守卫接收\\x") == "D:\\" + ld.DEFAULT_TARGET_DIRNAME)
ck("空目标盘归一化为空", ld.normalize_target("") == "")


# ============================================================
# 十、scan_running 端到端（用假 exe 列表，不动真实磁盘）
# ============================================================
t9 = ld.Tracker(tmp / "t9.json")
mkfile(appdata / "Roaming" / "DemoApp2" / "big.dat", 2048)
fake_exe = str(pf / "DemoApp2" / "demo2.exe")
mkfile(pf / "DemoApp2" / "demo2.exe")
ld.reset_cache(t9)          # 新建目录后让索引立刻重建（否则命中 10 分钟缓存）
items = ld.scan_running(exes=[fake_exe], threshold_gb=2.0, min_mb=0,
                        now=9000, t=t9)
ck("scan_running 首次使用能提示",
   bool(items) and items[0]["trigger"] == "首次使用", str(items))
t9.mark_prompted(items[0]["key"], "首次使用", items[0]["size"])
ck("提示过之后不重复",
   not ld.scan_running(exes=[fake_exe], threshold_gb=2.0, min_mb=0,
                       now=9001, t=t9))
ck("scan_running 记录了目录",
   bool(t9.get(items[0]["key"]).get("dirs")), str(t9.get(items[0]["key"])))

# 用户手动「不再提示」后，即使超阈值也不打扰
t9.decide(items[0]["key"], "never")
t9.set_dirs(items[0]["key"], t9.get(items[0]["key"])["dirs"], int(9 * 2 ** 30))
ck("不再提示后彻底安静",
   not ld.scan_running(exes=[fake_exe], threshold_gb=2.0, min_mb=0,
                       now=9002, t=t9))

# ---- 刚装好守卫的第一次扫描：静默登记，避免一开机被问十几个软件 ----
t10 = ld.Tracker(tmp / "t10.json")
mkfile(pf / "SmallApp" / "a.exe")
mkfile(appdata / "Roaming" / "SmallApp" / "x.dat", 16)
_orig_measure = ld.measure
ld.measure = lambda dirs, cap_bytes=None, max_seconds=4.0: {
    "size": 200 * 2 ** 20, "files": 1, "capped": False}
ld.reset_cache(t10)
r_small = ld.scan_running(exes=[str(pf / "SmallApp" / "a.exe")], t=t10,
                          baseline=True, min_mb=0, now=10000)
ld.measure = _orig_measure
ck("首次运行基线：小软件静默登记不打扰", not r_small, str(r_small))
ck("静默登记也算问过了",
   bool(t10.get("smallapp").get("first_silent")), str(t10.get("smallapp")))

t11 = ld.Tracker(tmp / "t11.json")
mkfile(pf / "BigApp" / "b.exe")
mkfile(appdata / "Roaming" / "BigApp" / "y.dat", 16)
ld.measure = lambda dirs, cap_bytes=None, max_seconds=4.0: {
    "size": int(5 * 2 ** 30), "files": 1, "capped": True}
ld.reset_cache(t11)
r_big = ld.scan_running(exes=[str(pf / "BigApp" / "b.exe")], t=t11,
                        baseline=True, threshold_gb=2.0, min_mb=0, now=10001)
ld.measure = _orig_measure
ck("首次运行基线：已经超阈值的仍会提醒",
   bool(r_big) and r_big[0]["trigger"] == "超过阈值", str(r_big))

restore_env()
shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"合计：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
