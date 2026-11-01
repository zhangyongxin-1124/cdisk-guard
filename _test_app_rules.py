# -*- coding: utf-8 -*-
"""软件存储限制（用户自定义规则）测试：规则 CRUD + 命中 + 限制流程分流"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import guard_rules

# 测试路径里的「用户名」段必须跟着当前电脑走：
# guard_rules._sys_segments() 会动态读取 USERNAME 并排除它，
# 若此处写死某个用户名（比如 yongx），换台电脑跑就该段会被误当成"软件名"，
# 于是整盘文件都会误命中。用当前用户名可让这个测试在任何机器上都有意义。
ME = (os.environ.get("USERNAME") or os.environ.get("USER") or "testuser").strip().lower()

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ---------- 1. 自动生成关键词 ----------
print("【1】关键词自动生成")
cases = [
    ("酷狗音乐", "酷狗音乐"),
    ("酷狗", "酷狗"),
    ("KuGou Music", "kugou"),
    ("Steam", "steam"),
    ("微信", "微信"),
    ("网易云音乐", "网易云音乐"),
    ("某个下载工具", "某个下载工具"),
]
for name, want in cases:
    kws = guard_rules._auto_keywords(name)
    joined = "".join(kws)
    ck(f"{name} -> 含 {want}", want.lower() in joined.lower(), kws)

# 泛词不能被单独留下：'音乐' 不应是唯一关键词
kws = guard_rules._auto_keywords("酷狗音乐")
ck("不产生孤立泛词'音乐'", "音乐" not in kws or len(kws) > 1, kws)

# ---------- 1b. 从样本路径补英文关键词（中文名 + 英文目录的关键场景）----------
print("【1b】样本路径补关键词")
s1 = guard_rules._keywords_from_sample(rf"C:\Users\{ME}\AppData\Local\KuGou\music\a.mp3")
ck("英文目录被提取", "kugou" in s1, s1)
ck("不含系统目录 users", "users" not in s1, s1)
ck("不含 appdata", "appdata" not in s1, s1)
ck("不含当前用户名", ME not in s1, s1)
s2 = guard_rules._keywords_from_sample(r"D:\Games\SteamLibrary\steamapps\x.dat")
ck("多段只取前几个", "steamlibrary" in s2 or "games" in s2, s2)
s3 = guard_rules._keywords_from_sample("")
ck("空路径不炸", s3 == [])
# 组合效果：中文名 + 英文路径 -> 两条路都能命中
rs0 = guard_rules.AppRules(path=os.path.join(tmpdir0 := tempfile.mkdtemp(), "r0.json"))
rs0.add("酷狗音乐", sample_path=rf"C:\Users\{ME}\AppData\Local\KuGou\music\a.mp3")
ck("中文路径可命中", rs0.match(rf"C:\Users\{ME}\Desktop\酷狗音乐\a.mp3", "") is not None)
ck("英文路径也可命中", rs0.match(rf"C:\Users\{ME}\AppData\Local\KuGou\x.dat", "") is not None)
ck("无关路径不命中", rs0.match(rf"C:\Users\{ME}\AppData\Local\Other\x.dat", "") is None)
shutil.rmtree(tmpdir0, ignore_errors=True)

# ---------- 2. 规则 CRUD ----------
print("【2】规则增删改 + 持久化")
tmpdir = tempfile.mkdtemp(prefix="guard_rules_")
rs = guard_rules.AppRules(path=os.path.join(tmpdir, "app_rules.json"))
ck("初始为空", rs.rules == [])
r1 = rs.add("酷狗音乐")
ck("添加成功", bool(r1.get("id")))
ck("关键词非空", len(r1["keywords"]) > 0, r1["keywords"])
ck("落盘文件存在", os.path.exists(os.path.join(tmpdir, "app_rules.json")))
r2 = rs.add("Steam", ["steam"])
ck("第二条不同 id", r2["id"] != r1["id"])
dup = rs.add("酷狗音乐")
ck("同名去重", dup["id"] == r1["id"])
ck("总数 2", len(rs.rules) == 2)

rs2 = guard_rules.AppRules(path=os.path.join(tmpdir, "app_rules.json"))
ck("重新加载保持 2 条", len(rs2.rules) == 2, len(rs2.rules))
ck("重载后关键词保留", len(rs2.rules[0]["keywords"]) > 0)

rs.set_enabled(r2["id"], False)
ck("停用生效", rs.get(r2["id"])["enabled"] is False)
ck("启用计数", rs.active_count() == 1, rs.active_count())
rs.remove(r1["id"])
ck("删除生效", len(rs.rules) == 1)

# ---------- 3. 命中判断 ----------
print("【3】命中判断")
rs3 = guard_rules.AppRules(path=os.path.join(tmpdir, "r3.json"))
rs3.add("酷狗音乐", ["kugou", "酷狗"])
hit = [
    (rf"C:\Users\{ME}\AppData\Local\KuGou\music\a.mp3", ""),
    (r"C:\Program Files (x86)\KuGou\KuGouMusic.exe", ""),
    (rf"C:\Users\{ME}\Desktop\酷狗音乐安装包.exe", ""),
    (r"D:\x\y.exe", "酷狗音乐"),
]
for p, app in hit:
    ck(f"命中 {p[:40]}", rs3.match(p, app) is not None)

miss = [
    (rf"C:\Users\{ME}\AppData\Local\Other\a.txt", ""),
    (r"C:\Windows\Temp\x.log", ""),
    (r"D:\QQMusic\a.mp3", ""),
]
for p, app in miss:
    ck(f"不误伤 {p[:40]}", rs3.match(p, app) is None, rs3.match(p, app))

# 停用的规则不命中
rs3.add("测试停用", ["zzz_test_kw"])
rid = rs3.rules[-1]["id"]
ck("启用时命中", rs3.match(r"C:\zzz_test_kw\a", "") is not None)
rs3.set_enabled(rid, False)
ck("停用后不命中", rs3.match(r"C:\zzz_test_kw\a", "") is None)

# 命中计数累加
before = rs3.rules[0].get("hits", 0)
rs3.match(r"C:\kugou\a", "")
ck("命中计数累加", rs3.rules[0]["hits"] > before)

# ---------- 4. 内置品牌对照表可关闭 ----------
print("【4】内置来源对照表开关")
import guard_today
guard_today.set_builtin_signatures(True)
on = guard_today.guess_app(r"C:\Users\testuser\AppData\Local\KuGou\x.dat")
guard_today.set_builtin_signatures(False)
off = guard_today.guess_app(r"C:\Users\testuser\AppData\Local\KuGou\x.dat")
guard_today.set_builtin_signatures(True)
ck("开启时给出名字", on != "未知来源", on)
ck("关闭后不出现厂商名", "酷狗" not in off and "音乐" not in off, off)
ck("关闭后回退到目录名", off.lower().startswith("kugou"), off)

print(f"\n合计：PASS {PASS} / FAIL {FAIL}")
shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(1 if FAIL else 0)
