# -*- coding: utf-8 -*-
"""_test_appdir.py — 跨电脑适配回归测试

覆盖用户实际踩到的故障：把程序整个目录拷到另一台电脑后，
显示的是上一台电脑的数据、写入拦截失效。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import guard_appdir as ga

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {extra}")


print("=" * 60)
print("1) 数据目录选择")
print("=" * 60)
d = ga.pick_app_dir()
ck("数据目录非空", d is not None and str(d), str(d))
ck("数据目录可写", ga._writable(d), str(d))
ck("APP_DIR 与 pick_app_dir 一致", str(ga.APP_DIR) == str(d), str(ga.APP_DIR))
ck("当前用户目录存在", ga.current_user_home().exists(), str(ga.current_user_home()))

print()
print("=" * 60)
print("2) 本机指纹")
print("=" * 60)
m1 = ga.machine_id()
m2 = ga.machine_id()
ck("指纹稳定（同机两次一致）", m1 == m2, f"{m1} vs {m2}")
ck("指纹非空且长度 16", len(m1) == 16, m1)
ck("机器标签含用户名", os.environ.get("USERNAME", "") in ga.machine_label(),
   ga.machine_label())

# 模拟换一台电脑：改环境变量后指纹必须变
old_env = dict(os.environ)
os.environ["COMPUTERNAME"] = "OTHER-PC"
os.environ["USERPROFILE"] = r"C:\Users\somebodyelse"
os.environ["USERNAME"] = "somebodyelse"
m3 = ga.machine_id()
ck("换电脑后指纹变化", m3 != m1, f"{m1} vs {m3}")
os.environ.update(old_env)
ck("恢复环境后指纹还原", ga.machine_id() == m1)

print()
print("=" * 60)
print("3) 外来数据清理（核心场景：整个目录拷到新电脑）")
print("=" * 60)
tmp = Path(tempfile.mkdtemp(prefix="guard_fresh_"))
try:
    # 造一份"上一台电脑"的数据
    (tmp / "today_files.json").write_text(
        json.dumps([{"path": r"C:\Users\旧用户\Downloads\a.zip"}], ensure_ascii=False),
        encoding="utf-8")
    (tmp / "legacy_C.json").write_text('{"rows":[]}', encoding="utf-8")
    (tmp / "root_known.json").write_text('["Desktop","Downloads"]', encoding="utf-8")
    (tmp / "config.json").write_text(
        json.dumps({"监控目录": {"下载": r"C:\Users\旧用户\Downloads"}},
                   ensure_ascii=False), encoding="utf-8")
    (tmp / "app_rules.json").write_text('{"rules":[]}', encoding="utf-8")
    (tmp / "machine_id.json").write_text(
        json.dumps({"machine_id": "0000000000deadbe"}), encoding="utf-8")

    ck("清理前 today_files.json 存在", (tmp / "today_files.json").exists())
    r = ga.purge_foreign_data(tmp, quiet=True)
    ck("判定为外来数据", r["purged"] is True, str(r))
    ck("扫描数据已移走", not (tmp / "today_files.json").exists())
    ck("存量快照已移走", not (tmp / "legacy_C.json").exists())
    ck("根目录基线已移走", not (tmp / "root_known.json").exists())
    ck("旧配置已移走", not (tmp / "config.json").exists())
    ck("用户规则保留（跨机器通用）", (tmp / "app_rules.json").exists())
    ck("生成了备份目录", r["backup"] and Path(r["backup"]).exists(), str(r["backup"]))
    ck("备份目录里能找回旧数据",
       (Path(r["backup"]) / "today_files.json").exists() if r["backup"] else False)
    ck("machine_id.json 已更新为本机",
       json.loads((tmp / "machine_id.json").read_text(encoding="utf-8"))["machine_id"]
       == ga.machine_id())
    # 再次调用不应重复清理
    r2 = ga.purge_foreign_data(tmp, quiet=True)
    ck("二次调用不误判", r2["purged"] is False, str(r2))
    ck("二次调用后 app_rules.json 仍在", (tmp / "app_rules.json").exists())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print("4) 首次运行（无 machine_id.json）")
print("=" * 60)
tmp2 = Path(tempfile.mkdtemp(prefix="guard_first_"))
try:
    (tmp2 / "app_rules.json").write_text("{}", encoding="utf-8")
    r3 = ga.purge_foreign_data(tmp2)
    ck("首次运行不清理已有文件", r3["purged"] is False, str(r3))
    ck("首次运行保留 app_rules.json", (tmp2 / "app_rules.json").exists())
    ck("首次运行写入指纹", (tmp2 / "machine_id.json").exists())
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print()
print("=" * 60)
print("5) 配置里的用户路径重定位")
print("=" * 60)
home = ga.current_user_home()
cfg = {
    "监控目录": {
        "下载": str(Path(r"C:\Users\旧用户") / "Downloads"),
        "桌面": str(home / "Desktop"),          # 本机的，不该动
    },
    "全盘监控根": [str(Path(r"C:\Users\旧用户"))],
    "删除保护目录": [str(Path(r"C:\Users\旧用户") / "Documents")],
    "聚合监控目录": [str(Path(r"C:\Users\旧用户") / "Downloads")],
    "单文件询问阈值MB": 10,                     # 非路径项，不该动
}
res = ga.relocate_user_paths(cfg)
ck("发生重定位", len(res["changed"]) >= 3, str(res["changed"]))
ck("监控目录·下载 已改指当前用户",
   cfg["监控目录"]["下载"].lower() == str(home / "Downloads").lower(),
   cfg["监控目录"]["下载"])
ck("本机原有路径不动",
   cfg["监控目录"]["桌面"] == str(home / "Desktop"))
ck("全盘监控根已改指", cfg["全盘监控根"][0] == str(home), cfg["全盘监控根"])
ck("删除保护目录已改指",
   cfg["删除保护目录"][0].lower() == str(home / "Documents").lower(),
   cfg["删除保护目录"][0])
ck("非路径项不受影响", cfg["单文件询问阈值MB"] == 10)

# 非用户目录的自定义路径必须原样保留
cfg2 = {"监控目录": {"下载": r"D:\我的下载"}}
res2 = ga.relocate_user_paths(cfg2)
ck("非用户目录路径不改动", cfg2["监控目录"]["下载"] == r"D:\我的下载",
   cfg2["监控目录"]["下载"])
ck("无谓改动为 0", res2["changed"] == [], str(res2["changed"]))

print()
print("=" * 60)
print("6) 真实模块加载（数据目录已切换为统一入口）")
print("=" * 60)
import guard_rules
ck("guard_rules 数据目录 == APP_DIR",
   str(guard_rules.DATA_FILE.parent) == str(ga.APP_DIR),
   str(guard_rules.DATA_FILE.parent))
import c_disk_guard
ck("c_disk_guard 数据目录 == APP_DIR",
   str(c_disk_guard.APP_DIR) == str(ga.APP_DIR), str(c_disk_guard.APP_DIR))
ck("C_USER 指向当前用户（不再写死他人用户名）",
   str(c_disk_guard.C_USER).lower() == str(home).lower(),
   str(c_disk_guard.C_USER))
ck("默认配置的监控目录属于当前用户",
   all(p.lower().startswith(str(home).lower())
       for p in c_disk_guard.DEFAULT_CONFIG["监控目录"].values()),
   str(c_disk_guard.DEFAULT_CONFIG["监控目录"]))
ck("默认全盘监控根为 C 盘根（全盘拦截）",
   [r.lower() for r in c_disk_guard.DEFAULT_CONFIG["全盘监控根"]] == ["c:\\"],
   str(c_disk_guard.DEFAULT_CONFIG["全盘监控根"]))

print()
print("=" * 60)
print(f"合计：{PASS} PASS / {FAIL} FAIL")
print("=" * 60)
sys.exit(1 if FAIL else 0)
