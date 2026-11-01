# -*- coding: utf-8 -*-
"""v6.10 跨电脑拦截修复 + 搬家不留链接 的专项测试。"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# GUARD_APP_DIR 必须在 import 引擎模块之前设置（CONFIG_PATH 在 import 时计算）
_TMP = tempfile.mkdtemp(prefix="guard_intercept_")
os.environ["GUARD_APP_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import c_disk_guard as engine
import guard_wizard

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def is_reparse(p: Path) -> bool:
    """用 Win32 API 直接查链接本身的属性（Path.stat() 会跟随联接拿到目标的属性）。"""
    import ctypes
    return bool(ctypes.windll.kernel32.GetFileAttributesW(str(p)) & 0x400)


def main():
    tmp = Path(_TMP)
    os.environ["GUARD_APP_DIR"] = str(tmp)
    try:
        # ============ 1. 默认配置 ============
        print("[1] 默认配置")
        dc = engine.DEFAULT_CONFIG
        ck("全盘监控根为 C 盘根", dc["全盘监控根"] == ["C:\\"], str(dc["全盘监控根"]))
        ck("默认不留搬家链接", dc.get("搬家后原位置留链接") is False)
        need_kw = ["\\windows\\", "\\program files\\", "\\program files (x86)\\",
                   "\\programdata\\", "\\system volume information\\"]
        miss = [k for k in need_kw if k not in dc["排除路径关键词"]]
        ck("系统目录排除词齐全", not miss, str(miss))

        # ============ 2. 老配置自动迁移 ============
        print("[2] 老配置自动升级为 C 盘全盘监控")
        (tmp / "config.json").write_text(json.dumps({
            "全盘拦截": True,
            "全盘监控根": [r"C:\Users\旧用户"],
            "排除路径关键词": ["\\cache"],
        }, ensure_ascii=False), encoding="utf-8")
        g = engine.Guard()
        cfg = g.config
        ck("监控根升级为 C:\\", cfg["全盘监控根"] == ["C:\\"], str(cfg["全盘监控根"]))
        ck("旧排除词保留", "\\cache" in cfg["排除路径关键词"])
        ck("新排除词补齐", all(k in cfg["排除路径关键词"] for k in need_kw))
        ck("搬家链接配置补齐", "搬家后原位置留链接" in cfg)
        saved = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        ck("迁移结果已写回 config.json", saved["全盘监控根"] == ["C:\\"])

        # ============ 3. move_dir_junction 两种模式 ============
        print("[3] 目录搬移：不留链接 / 留链接")
        src_root = tmp / "src"; dst_root = tmp / "dst"
        src_root.mkdir(); dst_root.mkdir()
        src = src_root / "U盘文件"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("hello", encoding="utf-8")
        (src / "sub" / "b.txt").write_text("world", encoding="utf-8")

        # 3a. 不留链接（默认）
        dest = g.move_dir_junction(src, str(dst_root), keep_link=False)
        ck("搬移成功返回目标", bool(dest) and Path(dest).is_dir(), str(dest))
        ck("C 盘原位置不再存在", not src.exists())
        ck("目标文件完整", (Path(dest) / "a.txt").read_text(encoding="utf-8") == "hello")
        ck("子目录也搬走", (Path(dest) / "sub" / "b.txt").exists())

        # 3b. 留链接
        src2 = src_root / "带链接的目录"
        src2.mkdir(parents=True)
        (src2 / "x.txt").write_text("x", encoding="utf-8")
        dest2 = g.move_dir_junction(src2, str(dst_root), keep_link=True)
        ck("留链接模式搬移成功", bool(dest2) and Path(dest2).is_dir(), str(dest2))
        ck("原位置存在（联接）", src2.exists())
        ck("原位置是 reparse point", is_reparse(src2))
        ck("经由联接可读到目标内容",
           (src2 / "x.txt").read_text(encoding="utf-8") == "x")

        # 3c. 不传 keep_link → 读配置（默认 False）
        src3 = src_root / "按配置来"
        src3.mkdir(parents=True)
        (src3 / "y.txt").write_text("y", encoding="utf-8")
        dest3 = g.move_dir_junction(src3, str(dst_root))
        ck("按配置默认不留链接", bool(dest3) and not src3.exists())

        # ============ 4. migrate_app_dir 两种模式 ============
        print("[4] migrate_app_dir 搬家")
        # 4a. 不留链接
        m_src = tmp / "mig_src" / "某软件数据"
        m_src.mkdir(parents=True)
        for i in range(30):
            (m_src / f"f{i:02d}.dat").write_bytes(b"0123456789" * 100)
        m_dst = tmp / "mig_dst"
        res = guard_wizard.migrate_app_dir(m_src, m_dst / "某软件数据",
                                           log=lambda *a: None, keep_link=False)
        ck("搬家成功", res.get("ok"), str(res))
        ck("C 盘原位置不再存在", not m_src.exists())
        ck("也无待清理残留", not Path(str(m_src) + "_守卫待清理").exists())
        ck("目标数据完整",
           len(list((m_dst / "某软件数据").glob("*.dat"))) == 30)
        ck("detail 提示原位置已清空", "不再保留" in res.get("detail", ""), res.get("detail", ""))

        # 4b. 留链接（向导旧行为）
        m_src2 = tmp / "mig_src2" / "另一软件"
        m_src2.mkdir(parents=True)
        (m_src2 / "a.dat").write_bytes(b"x" * 4096)
        res2 = guard_wizard.migrate_app_dir(m_src2, tmp / "mig_dst2" / "另一软件",
                                            log=lambda *a: None, keep_link=True)
        ck("留链接搬家成功", res2.get("ok"), str(res2))
        ck("原位置存在（联接）", m_src2.exists())
        ck("原位置是 reparse point", is_reparse(m_src2))

        # ============ 5. 排除词过滤 ============
        print("[5] 排除词对 C 盘全盘监听生效")
        ck("Windows 目录被排除", g.is_excluded(r"C:\Windows\System32\a.dll"))
        ck("Program Files 被排除", g.is_excluded(r"C:\Program Files\App\x.dll"))
        ck("Program Files (x86) 被排除", g.is_excluded(r"C:\Program Files (x86)\App\x.dll"))
        ck("AppData 被排除", g.is_excluded(r"C:\Users\a\AppData\Local\x\x.dat"))
        ck("回收站被排除", g.is_excluded(r"C:\$Recycle.Bin\x"))
        ck("C 盘根新文件不排除", not g.is_excluded(r"C:\U盘文件\setup.exe"))
        ck("用户目录新文件不排除", not g.is_excluded(r"C:\Users\演示\Desktop\照片.jpg"))

        # ============ 6. 监控根合法性 ============
        print("[6] start_watchers 输入")
        roots = g.start_watchers()
        ck("C 盘根会启动监听", roots == ["C:\\"], str(roots))
        for w in g.watchers:
            w.stop_evt.set()
        ck("watchers 已登记", len(g.watchers) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n合计：通过 {PASS}  项，失败 {FAIL} 项")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
