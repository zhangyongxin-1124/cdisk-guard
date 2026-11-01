# -*- coding: utf-8 -*-
"""存量盘点模块自测：不碰真实磁盘，用临时目录验证逻辑。"""
import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard_legacy as GL

OK = 0
BAD = 0


def chk(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        BAD += 1
        print(f"  ❌ {name}   {extra}")


def end():
    print(f"\n合计：通过 {OK} 项，失败 {BAD} 项")
    return 0 if BAD == 0 else 1


print("== 1. 守卫生日：自动推断并落盘 ==")
tmp = Path(tempfile.mkdtemp(prefix="gl_"))
(old := (tmp / "old.txt")).write_text("x", encoding="utf-8")
old_ts = time.time() - 86400 * 40
os.utime(old, (old_ts, old_ts))
info = GL.load_install_time(tmp)
chk("能读出一个时间戳", info["ts"] > 0, info)
chk("第二次读结果一致", abs(GL.load_install_time(tmp)["ts"] - info["ts"]) < 1)
chk("写了 install_time.json", (tmp / GL.INSTALL_FILE).exists())
chk("推断来源有说明", bool(info["source"]), info["source"])
GL.save_install_time(tmp, time.time(), "手动设置")
chk("手动设置后来源变了",
    "手动" in GL.load_install_time(tmp)["source"],
    GL.load_install_time(tmp)["source"])

print("\n== 2. 分类：把路径翻译成人话 ==")
cases = [
    (r"C:\Windows\WinSxS\abc", "system"),
    (r"C:\Program Files\WeChat", "program"),
    (r"C:\ProgramData\Microsoft\Defender", "system"),
    (r"C:\Users\me\AppData\Roaming\WeChat Files", "appdata"),
    (r"C:\Users\me\AppData\Local\Temp\xx", "cache"),
    (r"C:\Users\me\Desktop\报告.docx", "userfile"),
    (r"C:\Users\me\Documents\合同.pdf", "userfile"),
    (r"C:\Users\me\Downloads\setup.exe", "download"),
    (r"C:\Users\me\Pictures\照片.jpg", "media"),
    (r"C:\Users\me\Videos\电影.mp4", "media"),
    (r"C:\Users\me\OneDrive\文档", "cloud"),
]
for p, want in cases:
    k = GL.kind_of(p)
    chk(f"{p} -> {want}", k["key"] == want, k["key"])
chk("每个分类都有中文名与说明",
    all(GL.KINDS[k]["name"] and GL.KINDS[k]["tip"] for k in GL.KINDS))
# 非系统盘
chk("D 盘上的内容不再算系统文件",
    GL.kind_of(r"D:\软件\xx", "C")["key"] != "system")
cm = GL.can_move_text(r"D:\资料", "C")
chk("非系统盘提示不用搬", "不用搬" in cm["text"], cm["text"])
chk("系统盘 Windows 提示不要动",
    "不要动" in GL.can_move_text(r"C:\Windows\a", "C")["text"])

print("\n== 3. 月度账：改分界日期立刻重算 ==")
row = {"size": 300, "count": 3,
       "hist": {"2024-01": 100, "2025-06": 100, "2026-10": 100}}
base = datetime.datetime(2025, 6, 30).timestamp()
b, a = GL.split_by(row, base)
chk("2025-06 之前算老数据（200）", b == 200, (b, a))
chk("2025-06 之后算新增（100）", a == 100, (b, a))
chk("两者之和 = 总大小", b + a == row["size"])
b2, a2 = GL.split_by(row, datetime.datetime(2000, 1, 1).timestamp())
chk("分界设在很早：全部算新增", (b2, a2) == (0, 300), (b2, a2))
b3, a3 = GL.split_by(row, datetime.datetime(2099, 1, 1).timestamp())
chk("分界设在很晚：全部算老数据", (b3, a3) == (300, 0), (b3, a3))
chk("没有月度账时退化为全部老数据",
    GL.split_by({"size": 50}, base) == (50, 0))
s = GL.summarize([row], base)
chk("汇总 total/before/after 正确",
    (s["total"], s["before"], s["after"]) == (300, 200, 100), s)

print("\n== 4. 扫描：临时目录里造文件并汇总 ==")
root = Path(tempfile.mkdtemp(prefix="glscan_"))
(root / "a").mkdir()
(root / "a" / "b").mkdir()
(root / "a" / "b" / "deep").mkdir()
for rel, n in [("a/one.bin", 1000), ("a/b/two.bin", 2000),
               ("a/b/deep/three.bin", 3000), ("top.bin", 500)]:
    p = root / rel
    p.write_bytes(b"x" * n)

sc = GL.LegacyScanner(tmp, "C")
sc.roots = lambda: [(root, 2, set())]      # 只扫临时目录
data = sc.scan(time.time() + 86400 * 365)   # 分界设在未来 -> 全部老数据
rows = data["rows"]
chk("扫到了内容", rows and data["total"] > 0, data["total"])
paths = {r["path"] for r in rows}
chk("深度 2 处收敛：a\\b 独立成行", str(root / "a" / "b") in paths, paths)
chk("更深的内容并入 a\\b", str(root / "a" / "b" / "deep") not in paths)
chk("根目录下的散文件单独成行", str(root) in paths, paths)
ab = [r for r in rows if r["path"] == str(root / "a" / "b")][0]
chk("a\\b 合计 5000 字节（含子目录）", ab["size"] == 5000, ab["size"])
chk("a\\b 文件数 = 2", ab["count"] == 2, ab["count"])
chk("每行都记了月度账", all(r["hist"] for r in rows))
chk("每行都标了分类", all(r.get("kind") for r in rows))
chk("总大小 = 6500", data["total"] == 6500, data["total"])

# 分界设在过去 -> 全部新增
data2 = sc.scan(datetime.datetime(2000, 1, 1).timestamp())
s2 = GL.summarize(data2["rows"], data2["baseline"])
chk("分界设在过去时：全部算新增", s2["after"] == 6500 and s2["before"] == 0, s2)

print("\n== 5. 快照存取 ==")
sc.save(data)
back = sc.load()
chk("存了能读回来", back["rows"] and back["total"] == data["total"])
chk("读回来的行仍带月度账", all(r.get("hist") for r in back["rows"]))
chk("读回来的行仍带分类", all(r.get("kind") for r in back["rows"]))

print("\n== 6. 总览：没扫过的盘不报错 ==")
class FakeD:
    pass
fake = [{"letter": "C", "root": "C:\\", "name": "C 盘 · 系统",
         "type_name": "本地硬盘", "is_system": True,
         "total": 60 * 2**30, "free": 20 * 2**30, "used": 40 * 2**30,
         "pct": 66.6, "health": {"level": "良好", "icon": "🟢", "color": "#2f7d32"}},
        {"letter": "D", "root": "D:\\", "name": "D 盘", "type_name": "本地硬盘",
         "is_system": False, "total": 100 * 2**30, "free": 80 * 2**30,
         "used": 20 * 2**30, "pct": 20.0,
         "health": {"level": "良好", "icon": "🟢", "color": "#2f7d32"}}]
ov = GL.overview(tmp, time.time(), fake)
chk("每块盘都有一行", len(ov) == 2)
chk("C 盘扫过：有存量数据", ov[0]["scanned"] and ov[0]["before"] is not None)
chk("D 盘没扫过：存量为 None", ov[1]["scanned"] is False and ov[1]["before"] is None)
txt = GL.plain_summary(ov[0], time.time())
chk("C 盘一句话总结含大小与百分比", "装守卫前就有" in txt and "%" in txt, txt)
txt2 = GL.plain_summary(ov[1], time.time())
chk("没扫过的盘提示去扫描", "还没盘点过" in txt2, txt2)

print("\n== 7. size/时间格式化 ==")
ts = datetime.datetime(2026, 11, 1, 18, 9).timestamp()
chk("日期格式 2026.11.01", GL.fmt_ts(ts) == "2026.11.01", GL.fmt_ts(ts))
chk("时间格式含 18:09", GL.fmt_dt(ts).endswith("18:09"), GL.fmt_dt(ts))
chk("human_size 1.5 GB", GL.human_size(int(1.5 * 2**30)).startswith("1.5 GB"),
    GL.human_size(int(1.5 * 2**30)))

import shutil
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(root, ignore_errors=True)
sys.exit(end())
