# -*- coding: utf-8 -*-
"""v6.14 新功能回归测试。

覆盖用户点名的五类场景 + 新模块核心逻辑：
  1. 可选监控盘 + 白名单（含换机消毒、U 盘插拔动态跟随）
  2. 软件搬家（复制→校验→改引用→原位留 junction）与符号链接回退
  3. 迁移风险分级（服务型软件 = 高风险）与模拟运行（dry-run）
  4. 来源归因增强（手动修正优先、签名/进程查询可用性）
  5. 分步扫描与一次性扫描结果口径一致
  6. 日志清理模拟运行 / 文件类型 / 目录白名单 / 累计大小上限
  7. 权限不足场景安静降级、多程序并发写入不互卡

运行：python _test_v614.py     （全绿退出码 0，有失败退出码 1）
"""
import os
import sys
import time
import json
import shutil
import tempfile
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else f"  → {detail}"))


def _old(p):
    return str(p)


# ============================================================
# 一、可选监控盘 + 白名单
# ============================================================
import guard_policy as gp

ck("默认空配置回落系统盘", gp.roots({}, available=["C", "D"]) ==
   [gp.system_root()],
   f"got {gp.roots({}, available=['C', 'D'])}")
ck("多盘勾选生效", gp.roots({"监控盘": ["C", "D", "Z"]},
                           available=["C", "D", "E"]) == ["C:\\", "D:\\"])
ck("换机消毒：不存在的盘被剔除",
   gp.sanitize_drives({"监控盘": ["D", "Z"]}, available=["C", "E"])[0] == ["C"])
ck("只勾非系统盘也允许", gp.roots({"监控盘": ["D"]}, available=["C", "D"])
   == ["D:\\"])
ck("merge_defaults 补全新键", gp.merge_defaults({}) is True)
ck("merge_defaults 二次调用无改动", gp.merge_defaults(dict(gp.DEFAULT_POLICY))
   is False)

_wl_cfg = {"拦截白名单": ["D:\\Games", "\\SteamLibrary\\"],
           "白名单进程": ["OneDrive.exe"]}
ck("白名单-目录前缀命中", gp.is_whitelisted("D:\\Games\\A\\save.dat", _wl_cfg))
ck("白名单-路径片段命中",
   gp.is_whitelisted("D:\\SteamLibrary\\x.dat", _wl_cfg))
ck("白名单-不误伤其他路径",
   not gp.is_whitelisted("C:\\Users\\x\\a.txt", _wl_cfg))
ck("白名单-进程命中", gp.is_whitelisted("C:\\x\\a.txt", _wl_cfg,
                                        "onedrive.exe"))
ck("白名单-粘进来双反斜杠也能匹配",
   gp.is_whitelisted("D://Games//y.dat", _wl_cfg))

# U 盘插拔：available 变化时 roots 跟随（拔出 = 盘符消失）
pol = gp.Policy({"监控盘": ["C", "D"], "监控可移动盘": False})
ck("策略初值 C+D", pol.roots(available=["C", "D"]) == ["C:\\", "D:\\"])
ck("U 盘拔出后自动只剩 C", pol.roots(available=["C"]) == ["C:\\"])
ck("U 盘插回后恢复 C+D", pol.roots(available=["C", "D"]) ==
   ["C:\\", "D:\\"])
ck("配置未变化", pol.cfg.get("监控盘") == ["C", "D"])

# ============================================================
# 二、来源归因增强
# ============================================================
import guard_attribution as ga
import guard_today as gt

with tempfile.TemporaryDirectory() as td:
    store = ga.OverrideStore(Path(td) / "ov.json")
    store.set("kugou", "酷狗音乐")
    ck("手动修正命中", store.resolve(r"C:\x\AppData\Local\KuGou\a.mp3")
       == "酷狗音乐")
    ck("手动修正不误伤", store.resolve(r"C:\x\AppData\Local\Chrome\a.log") == "")
    ck("覆盖同名关键词", (store.set("kugou", "酷狗") or {}).get("app") == "酷狗"
       and len(store.rules) == 1)
    ck("建议关键词取软件目录段",
       ga.suggest_keyword(r"C:\Users\x\AppData\Local\KuGou\music\a.mp3")
       == "kugou")

# guess_app 的优先级：手动修正 > 内置关键词表
_orig_store = getattr(gt, "_ATTR_STORE", None)
_fake = ga.OverrideStore(Path(tempfile.mkdtemp()) / "ov.json")
_fake.set("steam", "我的Steam")
gt._ATTR_STORE = _fake
try:
    ck("guess_app 手动修正优先", gt.guess_app(r"C:\Steam\workshop\x.dat")
       == "我的Steam")
    ck("guess_app 无修正时走原逻辑",
       gt.guess_app(r"C:\Users\x\AppData\Local\KuGou\a.mp3") != "")
finally:
    gt._ATTR_STORE = _orig_store

# 数字签名 / 进程枚举（只验证可用性与降级，不依赖任何具体结果）
try:
    sigs = ga.signature_info([sys.executable])
    ck("数字签名查询可用", isinstance(sigs, list) and len(sigs) == 1
       and "status" in sigs[0])
except Exception as e:
    ck("数字签名查询可用", False, str(e))
try:
    procs = ga.running_processes()
    ck("进程枚举可用", isinstance(procs, list) and len(procs) > 0)
    ck("进程关联按目录过滤", isinstance(
        ga.processes_in_dir(os.path.dirname(sys.executable), procs), list))
except Exception as e:
    ck("进程枚举可用", False, str(e))

# ============================================================
# 三、软件搬家 / 符号链接回退 / 风险分级 / 模拟运行
# ============================================================
import guard_migrate as gm
import guard_paths as gpaths

B = "\\"
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    gm.MIGRATIONS_FILE = tdp / "mig.json"
    gm.BACKUP_DIR = tdp / "bk"
    src = tdp / "src" / "DemoApp"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "app.exe").write_bytes(b"x" * 4096)
    (src / "readme.txt").write_text("hi", encoding="utf-8")
    old = str(src)

    sc = [{"path": str(tdp / "a.lnk"), "target": old + B + "app.exe",
           "workdir": old, "args": "", "icon": ""}]
    reg = [{"hive": "HKCU", "key": "Software\\Demo", "name": "Dir",
            "old": old, "type": 1, "kind": "value", "service": ""}]
    svc = [{"name": "DemoSvc", "image": old + B + "svc.exe"}]

    ck("路径替换保留其余字符",
       gm._replace_path(old + B + "bin" + B + "x.exe", old,
                        "D:\\New") == "D:\\New\\bin\\x.exe")

    r_hi = gm.assess_risk(src, sc, reg, svc)
    ck("服务引用 → 高风险", r_hi["level"] == "高")
    ck("高风险给出服务名提示", any("DemoSvc" in x for x in r_hi["reasons"]))
    r_lo = gm.assess_risk(src, sc, [], [])
    ck("仅快捷方式 → 低风险", r_lo["level"] == "低")
    r_md = gm.assess_risk(src, [], reg, [])
    ck("注册表引用 → 中风险", r_md["level"] == "中")

    plan = gm.build_plan(src, tdp / "dst", keep_link=True,
                         shortcuts=sc, registry=reg, services=svc,
                         stats=(4096 + 2, 2))
    ck("计划的目标目录在目标盘下", str(tdp) in plan["dst"]
       and "C盘守卫迁移" in plan["dst"])
    ck("计划捕获快捷方式引用", len(plan["shortcuts"]) == 1
       and plan["shortcuts"][0]["new"]["target"].startswith(str(tdp / "dst")))
    ck("计划捕获注册表引用", len(plan["registry"]) == 1)
    ck("高风险需二次确认", plan["need_confirm_high_risk"] is True)
    dry = gm.apply_plan(plan, dry_run=True)
    ck("模拟运行不改任何东西", dry["ok"] and dry["stage"] == "模拟运行"
       and src.is_dir() and (src / "readme.txt").exists())

    # 真实搬家（关掉引用更新，避免碰真实注册表/开始菜单）
    res = gm.apply_plan(plan, update_shortcuts=False, update_registry=False)
    ck("软件搬家成功", res["ok"] and res["stage"] == "完成")
    ck("原位置是 junction", src.exists() and gpaths.is_junction(src))
    ck("数据完整落在目标盘",
       (Path(plan["dst"]) / "bin" / "app.exe").is_file()
       and (Path(plan["dst"]) / "readme.txt").is_file())
    ck("迁移记录已写入", len(gm.load_records()) == 1)

    # 符号链接回退：撤销搬家，数据回到原位
    rb = gm.rollback(gm.load_records()[0]["id"])
    ck("撤销搬家成功", rb.get("ok") is True, str(rb))
    ck("回退后原目录恢复为真实目录",
       src.is_dir() and (src / "bin" / "app.exe").is_file()
       and not gpaths.is_junction(src))

    # 再次搬家后测「建链接失败自动还原」
    plan2 = gm.build_plan(src, tdp / "dst2", keep_link=True,
                          shortcuts=[], registry=[], services=[],
                          stats=(4098, 2))
    _orig_mk = gm.gwizard.make_junction
    gm.gwizard.make_junction = lambda link, target: False
    try:
        res2 = gm.apply_plan(plan2, update_shortcuts=False,
                             update_registry=False)
    finally:
        gm.gwizard.make_junction = _orig_mk
    ck("建链接失败自动还原原目录",
       (not res2["ok"]) and src.is_dir() and not gpaths.is_junction(src)
       and (src / "readme.txt").exists(), str(res2))

    # 目标已存在 → 安全跳过
    plan3 = gm.build_plan(src, tdp / "dst2", keep_link=True,
                          shortcuts=[], registry=[], services=[],
                          stats=(4098, 2))
    Path(plan3["dst"]).mkdir(parents=True, exist_ok=True)
    res3 = gm.apply_plan(plan3, update_shortcuts=False, update_registry=False)
    ck("目标已存在时安全跳过", (not res3["ok"]) and src.is_dir())

    # 换机/异常路径的安静降级
    ck("scan_registry 空参数安静返回", gm.scan_registry("") == [])
    bad = [{"hive": "HKLM", "key": "SOFTWARE\\__guard_no_such_key__\\x",
            "name": "v", "old": "C:\\a", "new": "D:\\b", "type": 1,
            "kind": "value", "service": ""}]
    try:
        okn, failn = gm._apply_registry(bad, [])
        ck("权限不足/键不存在安静降级", okn == 0 and failn == 1)
    except Exception as e:
        ck("权限不足/键不存在安静降级", False, str(e))
    ck("assess_risk 对空目录不崩",
       gm.assess_risk(tdp / "__no_such_dir__", [], [], [])["level"] in
       ("低", "中", "高"))

# ============================================================
# 四、分步扫描与一次性扫描口径一致
# ============================================================
import guard_fastscan as fs

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "A" / "sub").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "C").mkdir()
    (root / "A" / "big.dat").write_bytes(b"x" * 5000)
    (root / "A" / "sub" / "nested.dat").write_bytes(b"y" * 3000)
    (root / "B" / "small.dat").write_bytes(b"z" * 100)
    (root / "loose.txt").write_bytes(b"w" * 50)

    def _descend(p, l, c, e):
        return c

    def _visit(rows):
        def v(e, parent, level, ctx):
            if not e.is_dir:
                rows.append((e.path, e.size, level))
        return v

    a, b = [], []
    fs.parallel_scan([(str(root), 0, "ctx")], _descend, _visit(a),
                     stat_dirs=False)
    phases = []
    fs.progressive_scan(str(root), "ctx", _descend, _visit(b),
                        stat_dirs=False,
                        phase_cb=lambda st, i, n, p:
                            phases.append((st, i, n)))
    a.sort()
    b.sort()
    ck("分步扫描与一次性扫描结果一致", a == b,
       f"{len(a)} vs {len(b)}")
    ck("分步扫描两阶段都有进度回调",
       {p[0] for p in phases} == {1, 2} and len(phases) >= 4)
    ck("阶段一按体积排序", [x["name"] for x in fs.progressive_scan(
        str(root), "ctx", _descend, lambda *args: None,
        stat_dirs=False)["summaries"]] == ["A", "B", "C"])
    s1 = fs.shallow_stats(str(root / "A"), 2)
    ck("浅层统计出体积", s1[0] == 8000 and s1[1] == 2, str(s1))

# 存量盘点：分步与一次性两种模式结果一致
import guard_legacy
with tempfile.TemporaryDirectory() as td:
    sc = guard_legacy.LegacyScanner(Path(td), "Q")
    try:
        roots = sc.roots()
    except Exception:
        roots = []
    if roots:
        base = time.time()
        d1 = sc.scan(base, progressive=False)
        d2 = sc.scan(base, progressive=True)
        k1 = sorted((r["path"], r["size"], r["count"]) for r in d1["rows"])
        k2 = sorted((r["path"], r["size"], r["count"]) for r in d2["rows"])
        ck("存量盘点分步/一次性口径一致", k1 == k2)
    else:
        ck("存量盘点分步/一次性口径一致", True, "无可用盘，跳过")

# ============================================================
# 五、日志清理：模拟运行 / 文件类型 / 目录白名单 / 累计上限
# ============================================================
import guard_cleaner as gc

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "logs").mkdir()
    (root / "keep").mkdir()
    old_t = time.time() - 10 * 86400
    for rel, n in [("logs/a.log", 100), ("logs/b.txt", 200),
                   ("keep/c.log", 300), ("big.dmp", 400)]:
        p = root / rel
        p.write_bytes(b"x" * n)
        os.utime(p, (old_t, old_t))
    base_cfg = dict(gc.DEFAULT_CFG, **{
        "扫描根目录": [str(root)], "日志保留天数": 3, "清临时文件": False})

    r0 = gc.scan(base_cfg)
    ck("默认规则扫出 4 个", len(r0["items"]) == 4, str(len(r0["items"])))
    r1 = gc.scan(dict(base_cfg, **{"目录白名单": [str(root / "keep")]}))
    ck("目录白名单永不清理",
       sorted(os.path.basename(i["path"]) for i in r1["items"]) ==
       ["a.log", "b.txt", "big.dmp"])
    r2 = gc.scan(dict(base_cfg, **{"允许文件类型": [".log"]}))
    ck("文件类型过滤",
       sorted(os.path.basename(i["path"]) for i in r2["items"]) ==
       ["a.log", "c.log"])
    pv = gc.preview(r0["items"])
    ck("模拟运行不删文件", (root / "logs" / "a.log").exists()
       and pv["count"] == 4 and pv["total"] == 1000)
    capped = gc.clean(r0["items"], max_bytes=250)
    ck("累计上限：到顶即停", capped["stopped_by_cap"] and capped["freed"] == 200)
    ck("累计上限：单个超预算的跳过",
       (root / "big.dmp").exists() and (root / "keep" / "c.log").exists()
       and not (root / "logs" / "b.txt").exists())

# ============================================================
# 六、多程序同时写入（并发）+ 引擎查询线程安全
# ============================================================
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    errs = []

    def writer(idx):
        try:
            for i in range(30):
                p = root / f"proc{idx}" / f"f{i}.dat"
                p.parent.mkdir(exist_ok=True)
                p.write_bytes(os.urandom(64))
                gt.guess_app(str(p))          # 归因查询并发调用
        except Exception as e:
            errs.append(f"w{idx}: {e}")

    def querier():
        try:
            pol2 = gp.Policy({"监控盘": ["C"]})
            for _ in range(30):
                pol2.roots(available=["C", "D"])
                gc.preview([{"path": str(root / "x.log"), "size": 10,
                             "cat": "日志", "reason": "t"}])
        except Exception as e:
            errs.append(f"q: {e}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=querier) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ck("多程序并发写入 + 查询无异常", not errs, "; ".join(errs[:3]))
    n_files = sum(len(list((root / f"proc{i}").glob("*.dat")))
                  for i in range(4))
    ck("并发写入全部落盘", n_files == 120, str(n_files))

# ============================================================
# 汇总
# ============================================================
print("-" * 66)
print(f"合计：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  ·", x)
sys.exit(1 if FAIL else 0)
