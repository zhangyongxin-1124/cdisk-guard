# -*- coding: utf-8 -*-
"""移动说明列 — 规则与集成自测"""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard_today import move_risk, TodayScanner, MOVE_LEVELS

PASS = FAIL = 0

def chk(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got={got!r} want={want!r}")

print("== 1. 各类路径的风险分级 ==")
CASES = [
    (r"C:\Windows\WinSxS\amd64_xxx",            "系统文件",   "system"),
    (r"C:\Windows\SoftwareDistribution\Download\x", "更新",   "system"),
    (r"C:\ProgramData\Microsoft\Defender\x",    "Microsoft",  "system"),
    (r"C:\Users\testuser\OneDrive\文档\报告.docx",  "OneDrive",   "cloud"),
    (r"C:\Users\testuser\WPS CloudFiles\xx",       "WPS",        "cloud"),
    (r"C:\Program Files\Adobe\Acrobat\a.exe",   "Adobe",      "program"),
    (r"C:\Program Files (x86)\Tencent\x.exe",   "腾讯",       "program"),
    (r"C:\ProgramData\Adobe\x",                 "Adobe",      "program"),
    (r"C:\Users\testuser\AppData\Local\Google\Chrome\User Data\Default\Cache\x", "Chrome", "cache"),
    (r"C:\Users\testuser\AppData\Local\Temp\a.tmp","系统",        "cache"),
    (r"C:\Users\testuser\AppData\Local\Crashpad\x","Chrome",      "cache"),
    (r"C:\Users\testuser\AppData\Roaming\WeChat Files\msg.db", "微信", "appdata"),
    (r"C:\Users\testuser\AppData\Local\Doubao\db", "豆包",        "appdata"),
    (r"C:\Users\testuser\Downloads\电影.mp4",      "未知来源",    "data"),
    (r"C:\Users\testuser\Desktop\报告.pdf",        "未知来源",    "data"),
    (r"C:\Users\testuser\Pictures\a.jpg",          "未知来源",    "data"),
    (r"C:\Users\testuser\Videos\b.mkv",            "未知来源",    "data"),
    (r"C:\Users\testuser\Documents\xx.xlsx",       "未知来源",    "data"),
    (r"C:\Users\testuser\BaiduNetdiskDownloads\x.zip", "百度网盘", "data"),
    # —— 真实数据暴露的两个误判（已修），必须守住 ——
    # 更新残留：判为「缓存/日志·可放心搬」，才会被用户真的清掉
    (r"C:\Users\testuser\AppData\Local\Doubao\User Data\update_download\a.exe",
     "豆包", "cache"),                         # 曾按文件名误判成"个人文件"
    (r"C:\Users\testuser\AppData\Local\@genieworkbuddy-desktop-updater\x.exe",
     "WorkBuddy", "cache"),
    (r"C:\Users\testuser\AppData\Local\@deepseek-aidsh-desktop-updater\x.exe",
     "DeepSeek", "cache"),
    (r"C:\Users\testuser\AppData\Local\Doubao\User Data\sandbox_runtime\b.dat",
     "豆包", "appdata"),
    (r"C:\Users\testuser\AppData\Local\Microsoft\OneDrive\21.220\x.log",
     "OneDrive", "appdata"),                  # 客户端自己的数据，不是同步内容
    (r"C:\Users\testuser\AppData\Local\Microsoft\Windows\WebCache\x.dat",
     "Windows", "system"),
    # —— 真正的用户同步目录仍要标红 ——
    (r"C:\Users\testuser\OneDrive\文档\报告.docx", "OneDrive", "cloud"),
    (r"C:\Users\testuser\OneDrive\Desktop\a.txt", "OneDrive", "cloud"),
    (r"F:\电影\xx.mkv",                          "未知来源",    "other_disk"),
    (r"E:\备份\a.zip",                           "未知来源",    "other_disk"),
]
for path, app, want in CASES:
    r = move_risk(path, app)
    chk(f"{path[:52]:<52} -> {want}", r["level"], want)

print("\n== 2. 输出字段完整性 ==")
r = move_risk(r"C:\Users\testuser\Downloads\x.mp4", "未知来源")
for k in ("level", "order", "text", "color", "tip"):
    chk(f"字段 {k} 存在且非空", bool(r.get(k) or r.get(k) == 0), True)
chk("order 是整数", isinstance(r["order"], int), True)
chk("tip 是公益性质的大白话（长度>20）", len(r["tip"]) > 20, True)
chk("没有残留的 {app} 占位符", "{" in r["tip"], False)

print("\n== 3. app 名字能进提示语 ==")
r2 = move_risk(r"C:\Users\testuser\AppData\Roaming\DingTalk\x", "钉钉")
chk("提示语里带软件名", "钉钉" in r2["tip"], True)

print("\n== 4. order 递增关系 ==")
o = {k: MOVE_LEVELS[k]["order"] for k in MOVE_LEVELS}
chk("个人文件 < 软件数据", o["data"] < o["appdata"], True)
chk("软件数据 < 程序安装位置", o["appdata"] < o["program"], True)
chk("程序安装位置 < 云盘", o["program"] < o["cloud"], True)
chk("云盘 < 系统文件", o["cloud"] < o["system"], True)
chk("其他盘 = 最安全", o["other_disk"], 0)

print("\n== 5. aggregate 带出 risk ==")
now = datetime.datetime.now().timestamp()
items = [
    {"path": r"C:\Users\testuser\Downloads\1.mp4", "name": "1.mp4",
     "size": 1024, "mtime": now, "ctime": now, "app": "未知来源", "is_dir": False},
    {"path": r"C:\Windows\Temp\x.tmp", "name": "x.tmp",
     "size": 2048, "mtime": now, "ctime": now, "app": "系统", "is_dir": False},
    {"path": r"F:\abc\y.zip", "name": "y.zip",
     "size": 512, "mtime": now, "ctime": now, "app": "未知来源", "is_dir": False},
]
rows = TodayScanner.aggregate(items)
chk("聚合行数", len(rows), 3)
chk("每行都有 risk", all("risk" in r for r in rows), True)
lv = {r["dir"]: r["risk"]["level"] for r in rows}
chk(r"Downloads -> data", lv[r"C:\Users\testuser\Downloads"], "data")
chk(r"Windows\Temp -> system", lv[r"C:\Windows\Temp"], "system")
chk(r"F:\abc -> other_disk", lv[r"F:\abc"], "other_disk")

print("\n== 6. 空/异常路径不崩 ==")
for bad in ["", None, "\\", r"C:\\", "中文路径/带斜杠"]:
    try:
        move_risk(bad, "未知来源")
        chk(f"不崩 {bad!r}", True, True)
    except Exception as e:
        chk(f"不崩 {bad!r}", f"Exception {e}", True)

print(f"\n{'='*46}\n合计：PASS {PASS} / FAIL {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
