# -*- coding: utf-8 -*-
"""v6.16 开机自启闭环测试（内存假注册表，不碰真实系统）。

覆盖：
  1. 配置项「开机自启」存在且默认为真
  2. 关闭后 ensure_autostart 不得把注册表项写回（旧版缺陷）
  3. 打开时写入，且参数统一为 --minimized
  4. 开着再关掉时，会删除已有的注册表项
  5. 界面侧不再有第二套自启实现（避免两处互相覆盖）
  6. 升级继承：老配置没有该键时，按注册表真实状态回填
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def chk(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


# ---------------------------------------------------------------- 假注册表
STORE = {}


class _Key:
    def __init__(self, hive, path):
        self.hive, self.path = hive, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_winreg():
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.HKEY_LOCAL_MACHINE = "HKLM"
    fake.KEY_READ = 1
    fake.KEY_SET_VALUE = 2
    fake.REG_SZ = 1
    fake.OpenKey = lambda h, p, *a, **k: _Key(h, p)

    def _qv(k, n):
        if (k.hive, k.path, n) in STORE:
            return STORE[(k.hive, k.path, n)], 1
        raise FileNotFoundError(n)

    fake.QueryValueEx = _qv
    fake.SetValueEx = lambda k, n, r, t, v: STORE.__setitem__((k.hive, k.path, n), v)
    fake.DeleteValue = lambda k, n: STORE.pop((k.hive, k.path, n), None)
    fake.EnumKey = lambda k, i: (_ for _ in ()).throw(OSError())
    fake.CloseKey = lambda k: None
    sys.modules["winreg"] = fake


_install_fake_winreg()
sys.frozen = True
sys.executable = r"C:\Program Files\C盘守卫\C盘守卫.exe"

import c_disk_guard as cg  # noqa: E402

KEY = ("HKCU", cg.AUTOSTART_RUN_PATH, cg.AUTOSTART_KEY_NAME)

print("\n【1】配置项存在性")
chk("DEFAULT_CONFIG 含「开机自启」", "开机自启" in cg.DEFAULT_CONFIG)
chk("默认值为 True（与旧版「总是开」行为一致）",
    cg.DEFAULT_CONFIG.get("开机自启") is True)

g = cg.Guard.__new__(cg.Guard)          # 跳过 __init__，不启动真实监控
g.config = dict(cg.DEFAULT_CONFIG)

print("\n【2】用户关闭后不得写回（旧版缺陷回归）")
STORE.clear()
g.config["开机自启"] = False
g.ensure_autostart()
chk("关闭状态启动后，注册表无自启项", KEY not in STORE, f"实际={STORE.get(KEY)}")

print("\n【3】打开时写入且参数统一")
g.config["开机自启"] = True
g.ensure_autostart()
val = STORE.get(KEY, "")
chk("打开状态启动后，注册表有自启项", bool(val))
chk("启动参数为 --minimized（不再用 --silent）", val.endswith("--minimized"), val)
chk("指向当前 exe", "C盘守卫.exe" in val, val)

print("\n【4】开着再关掉，会删除已有项")
g.config["开机自启"] = False
g.ensure_autostart()
chk("关闭后注册表项被删除", KEY not in STORE, f"实际={STORE.get(KEY)}")

print("\n【5】关闭状态下重复启动仍然保持关闭（幂等）")
for _ in range(3):
    g.ensure_autostart()
chk("连跑 3 次仍无自启项", KEY not in STORE)

print("\n【6】界面侧不得有第二套自启实现")
gui_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "guard_gui.py"), encoding="utf-8").read()
chk("guard_gui.py 不再直接操作 Run 注册表", "CurrentVersion" not in gui_src)
chk("guard_gui.py 不再定义第二套 ensure_autostart",
    "def ensure_autostart" not in gui_src)

print("\n【7】升级继承：老配置没有该键时按注册表回填")
# 7a. 老用户原本开着自启 -> 升级后应保持开
STORE.clear()
g.config["开机自启"] = True
g.ensure_autostart()                     # 先造出「注册表里确实有」
ck_before = KEY in STORE
chk("前置：注册表确有自启项", ck_before)
chk("_autostart_registered() 读得到", cg._autostart_registered() is True)
# 7b. 老用户原本关着 -> 升级后应保持关
STORE.clear()
chk("注册表无项时 _autostart_registered() 返回 False",
    cg._autostart_registered() is False)

print(f"\n合计：PASS {PASS} / FAIL {FAIL}")
sys.exit(0 if FAIL == 0 else 1)
