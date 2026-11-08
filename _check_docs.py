# -*- coding: utf-8 -*-
"""发版前文档一致性校验：README.md / 使用说明_桌面版.txt 必须与代码一致。

用法：
    python _check_docs.py                 # 只校验仓库内文档
    python _check_docs.py --zip XXX.zip  # 额外校验发布包内的文档是否与仓库一致

退出码：0 = 全部通过；1 = 有不一致（发版必须阻断）。
纯静态解析，不 import 任何业务模块，因此不依赖 PySide6 等第三方包。
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
MANUAL = ROOT / "使用说明_桌面版.txt"
GUI = ROOT / "guard_gui.py"
FASTSCAN = ROOT / "guard_fastscan.py"
CLEANER = ROOT / "guard_cleaner.py"
ENGINE = ROOT / "c_disk_guard.py"
SPEC = ROOT / "CDiskGuardGUI.spec"
GITIGNORE = ROOT / ".gitignore"
PORTABLE_MANUAL = ROOT / "便携版" / "使用说明_桌面版.txt"

CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
          "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八",
          "十九", "二十"]


def read(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


class Checker:
    def __init__(self):
        self.results: list[tuple[bool, str, str]] = []

    def ck(self, ok: bool, name: str, detail: str = "") -> None:
        self.results.append((bool(ok), name, detail))

    def report(self) -> int:
        print("=" * 66)
        print("文档一致性校验（README.md / 使用说明_桌面版.txt vs 代码）")
        print("=" * 66)
        for i, (ok, name, detail) in enumerate(self.results, 1):
            flag = "PASS" if ok else "FAIL"
            line = f"[{i:02d}] {flag}  {name}"
            if detail and not ok:
                line += f"\n         → {detail}"
            print(line)
        fail = sum(1 for ok, _, _ in self.results if not ok)
        print("-" * 66)
        print(f"合计：PASS {len(self.results) - fail} / FAIL {fail}")
        if fail:
            print("❌ 存在不一致，禁止发版：请先修正文档或代码，再重新运行本脚本。")
        else:
            print("✅ 全部一致，可以发版。")
        return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="", help="发布包 zip 路径，校验包内文档是否与仓库一致")
    args = ap.parse_args()

    c = Checker()
    s_readme, s_manual = read(README), read(MANUAL)
    s_gui, s_fast, s_clean = read(GUI), read(FASTSCAN), read(CLEANER)
    s_engine = read(ENGINE)
    s_spec, s_ignore, s_portable = read(SPEC), read(GITIGNORE), read(PORTABLE_MANUAL)

    # ---------- 1. 版本号 ----------
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', s_gui)
    ver = m.group(1) if m else ""
    c.ck(bool(ver), "能从 guard_gui.py 读出 APP_VERSION", "未找到 APP_VERSION 定义")
    if ver:
        c.ck(f"v{ver}" in s_manual.split("\n")[0],
             "使用说明标题版本号与代码一致",
             f"代码 v{ver}，使用说明首行：{s_manual.split(chr(10))[0][:60]}")
        c.ck(f"程序内 v{ver}" in s_readme,
             "README 标注的程序内版本号与代码一致",
             f"README 缺少「程序内 v{ver}」")

    # ---------- 2. 导航页数量与名称 ----------
    pm = re.search(r"pages\s*=\s*\((.*?)\)\s*\n\s*for i,", s_gui, re.S)
    pages = re.findall(r'\("[^"]+",\s*"([^"]+)"', pm.group(1)) if pm else []
    c.ck(bool(pages), "能从 guard_gui.py 解析出导航页列表", "pages 元组解析失败")
    if pages:
        n = len(pages)
        mm = re.search(r"一共\s*(\d+)\s*个页面", s_manual)
        c.ck(bool(mm) and int(mm.group(1)) == n,
             f"使用说明的页面数 = 代码页面数（{n}）",
             f"代码 {n} 页，使用说明写的是 {mm.group(1) if mm else '未写明'}")
        rm = re.search(r"左侧竖排导航共\s*(\d+)\s*页", s_readme)
        c.ck(bool(rm) and int(rm.group(1)) == n,
             f"README 的页面数 = 代码页面数（{n}）",
             f"代码 {n} 页，README 写的是 {rm.group(1) if rm else '未写明'}")
        # 使用说明的页面清单必须逐一列出每个页面
        head = s_manual.split("二、")[0]
        missing = [t for t in pages if f"· {t}" not in head]
        c.ck(not missing, "使用说明页面清单覆盖全部导航页",
             "缺少：" + "、".join(missing))

    # ---------- 3. 扫描线程策略（v6.13 CPU-1）----------
    cpu_minus_one = "n -= 1" in s_fast
    c.ck(cpu_minus_one, "guard_fastscan.default_workers 仍是「留一核」策略",
         "代码中未找到 n -= 1，若策略变更请同步更新文档与校验规则")
    if cpu_minus_one:
        for label, txt in (("README", s_readme), ("使用说明", s_manual)):
            c.ck("CPU 核数减 1" in txt, f"{label} 写明「CPU 核数减 1」",
                 f"{label} 缺少 CPU-1 描述")
            stale = [x for x in ("按 CPU 核数取（最多 8）", "按 CPU 核数自动决定（最多 8 个）")
                     if x in txt]
            c.ck(not stale, f"{label} 无过时的线程数描述",
                 "残留：" + " / ".join(stale))

    # ---------- 4. 环境变量与引擎特性 ----------
    c.ck("GUARD_SCAN_WORKERS" in s_fast, "代码中存在 GUARD_SCAN_WORKERS",
         "环境变量改名后需同步文档与校验规则")
    for label, txt in (("README", s_readme), ("使用说明", s_manual)):
        c.ck("GUARD_SCAN_WORKERS" in txt, f"{label} 提到 GUARD_SCAN_WORKERS")
    c.ck("time.sleep(0)" in s_fast and "主动让出" in s_readme + s_manual,
         "让出机制在代码与文档中同时存在")
    c.ck("0.1" in s_fast and "节流" in s_readme + s_manual,
         "进度节流（100ms）在代码与文档中同时存在")

    # ---------- 5. 日志清理默认参数 ----------
    cfg = dict(re.findall(r'"([^"]+)"\s*:\s*(\d+|True|False)', s_clean))
    expect = [
        ("日志保留天数", "天"), ("超大日志MB", "MB"),
        ("自动周期天", "天"), ("最小清理量MB", "MB"),
    ]
    for key, unit in expect:
        val = cfg.get(key)
        c.ck(val is not None and f"默认 {val} {unit}" in s_manual,
             f"使用说明写出「{key}」默认值 {val}",
             f"代码 {key}={val}，使用说明未出现「默认 {val} {unit}」")
    c.ck(cfg.get("启用") == "False" and "默认关" in s_manual,
         "自动清理默认关闭 在代码与文档中一致")
    c.ck(cfg.get("清理前询问") == "True" and ("推荐开启" in s_manual or "默认开" in s_manual),
         "清理前询问默认开启 在代码与文档中一致")

    # ---------- 6. 源码结构表覆盖核心模块 ----------
    mods = set(re.findall(r"'([a-zA-Z_][a-zA-Z0-9_]*)'", s_spec))
    core = sorted(m for m in mods if m.startswith("guard_") or m.startswith("c_disk"))
    core += ["guard_gui", "CDiskGuardGUI.spec"]
    miss_mod = [m for m in core
                if f"`{m}.py`" not in s_readme and f"`{m}`" not in s_readme]
    c.ck(not miss_mod, "README 源码结构表覆盖 spec 中全部核心模块",
         "缺少：" + "、".join(miss_mod))
    # 表中列出的 .py 必须真实存在
    listed = re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*\.py)`", s_readme)
    ghost = [f for f in listed if not (ROOT / f).exists()]
    c.ck(not ghost, "README 列出的源码文件都真实存在",
         "不存在：" + "、".join(ghost))

    # ---------- 7. 章节编号连续性与内部小节前缀 ----------
    chapters: list[tuple[str, str]] = []   # (中文号, 标题)
    cur_no = 0
    bad_prefix: list[str] = []
    for line in s_manual.split("\n"):
        cm = re.match(r"^([一二三四五六七八九十]+)、(.+)$", line)
        if cm:
            chapters.append((cm.group(1), cm.group(2)))
            cur_no = CN_NUM.index(cm.group(1)) + 1 if cm.group(1) in CN_NUM else 0
            continue
        sm = re.match(r"^\s*(\d+)\.(\d+)", line)
        if sm and cur_no and int(sm.group(1)) != cur_no:
            bad_prefix.append(f"第{cur_no}章下出现 {sm.group(1)}.{sm.group(2)} 小节")
    want = CN_NUM[:len(chapters)]
    got = [x[0] for x in chapters]
    c.ck(got == want and len(chapters) >= 10,
         f"使用说明章节编号连续无重复（共 {len(chapters)} 章）",
         f"实际顺序：{'、'.join(got)}")
    c.ck(not bad_prefix, "内部小节编号与所属章节号一致",
         "；".join(bad_prefix[:4]))

    # ---------- 8. 交叉引用指向真实存在的章节 ----------
    refs = re.findall(r"详见第([一二三四五六七八九十]+)章", s_manual)
    title_by_no = dict(chapters)
    bad_refs = [r for r in refs if r not in title_by_no]
    log_no = next((num for num, title in chapters if "日志自动清理" in title), "")
    c.ck(bool(refs) and not bad_refs,
         "「详见第X章」都指向真实存在的章节",
         f"悬空引用：{'、'.join(bad_refs)}；全部引用 {refs}")
    c.ck(log_no in refs,
         "日志自动清理章节被交叉引用",
         f"引用 {refs}，日志自动清理实际在「{log_no}」章")

    # ---------- 9. 格式与过时表述 ----------
    forbidden = {
        "「可能需要几分钟」旧文案": "可能需要几分钟",
        "「6 个页面」旧文案": "一共 6 个页面",
        "「三之二」错乱章节": "三之二、",
    }
    for label, pat in forbidden.items():
        hit = (re.search(pat, s_readme) is not None) or (re.search(pat, s_manual) is not None)
        c.ck(not hit, f"两份文档无{label}", f"仍存在：{pat}")

    # 盘符双斜杠（C:// D://）——要排除 https:// 里的 "s://" 这类协议写法
    def has_drive_double_slash(txt: str) -> bool:
        for m in re.finditer(r"([A-Za-z])://", txt):
            if m.start() > 0 and txt[m.start() - 1].isalpha():
                continue          # https:// / ftp:// 等 URL 协议，不算
            return True
        return False

    c.ck(not (has_drive_double_slash(s_readme) or has_drive_double_slash(s_manual)),
         "两份文档无盘符双斜杠路径（C:// / D://）",
         "仍存在 Windows 路径被写成双正斜杠")

    # ---------- 10. .gitignore 与隐私说明 ----------
    for name in ("today_files", "guard_log/", "app_rules.json"):
        c.ck(name in s_ignore, f".gitignore 排除 {name}",
             "隐私说明里承诺排除，但 .gitignore 没有")
    c.ck("不联网" in s_readme and "本地运行" in s_readme,
         "README 隐私说明与「完全本地运行」一致")

    # ---------- 11. 便携版说明同步 ----------
    if PORTABLE_MANUAL.exists():
        c.ck(s_portable == s_manual, "便携版使用说明与主文档完全一致",
             "便携版/使用说明_桌面版.txt 未同步，请重新拷贝")

    # ---------- 11b. v6.14 新功能：代码与两份文档同时存在 ----------
    for mod in ("guard_migrate", "guard_attribution", "guard_policy"):
        c.ck(mod in s_spec, f"spec hiddenimports 含 {mod}",
             "新模块没进打包配置，exe 里会缺功能")
        c.ck(f"`{mod}.py`" in s_readme, f"README 源码结构表含 {mod}.py")
    for key in ("监控盘", "拦截白名单", "白名单进程", "落盘稳定秒",
                "落盘后秒级迁移"):
        c.ck(f'"{key}"' in s_engine, f"引擎默认配置含「{key}」",
             "配置键改名后需同步文档与校验规则")
    for label, txt in (("README", s_readme), ("使用说明", s_manual)):
        for kw in ("软件迁移", "监控盘选择", "拦截白名单", "模拟运行",
                   "风险等级", "数字签名", "junction"):
            c.ck(kw in txt, f"{label} 提到「{kw}」",
                 "v6.14 新功能缺说明，文档与代码不一致")
        c.ck("分步扫描" in txt, f"{label} 提到「分步扫描」")
    c.ck("progressive_scan" in s_fast and "shallow_stats" in s_fast,
         "guard_fastscan 含分步扫描引擎",
         "函数改名后需同步文档与校验规则")
    c.ck("def preview(" in s_clean and "dry_run" in s_clean,
         "guard_cleaner 含模拟运行（dry-run）")

    # ---------- 11c. v6.15 联动盘：代码与两份文档同时存在 ----------
    s_link = read(ROOT / "guard_linkdisk.py")
    c.ck(bool(s_link), "guard_linkdisk.py 存在", "联动盘模块缺失")
    c.ck("guard_linkdisk" in s_spec, "spec hiddenimports 含 guard_linkdisk",
         "新模块没进打包配置，exe 里会缺功能")
    c.ck("`guard_linkdisk.py`" in s_readme, "README 源码结构表含 guard_linkdisk.py")
    for key in ("联动盘提醒", "联动盘阈值GB", "联动盘最小提示MB",
                "联动盘冷却天", "联动盘目标盘"):
        c.ck(f'"{key}"' in s_engine, f"引擎默认配置含「{key}」")
    for label, txt in (("README", s_readme), ("使用说明", s_manual)):
        for kw in ("联动盘", "门牌号", "2GB", "还原"):
            c.ck(kw in txt, f"{label} 提到「{kw}」",
                 "v6.15 新功能缺说明，文档与代码不一致")
        c.ck("软件迁移" in txt, f"{label} 说明联动盘与软件迁移的分工")
    c.ck("def link_app(" in s_link and "def unlink_app(" in s_link,
         "guard_linkdisk 具备创建与还原能力",
         "函数改名后需同步文档与校验规则")
    c.ck((ROOT / "_test_v615_linkdisk.py").exists(),
         "联动盘专项测试脚本存在")

    # ---------- 12. 发布包内文档（可选）----------
    if args.zip:
        zp = Path(args.zip)
        c.ck(zp.exists(), f"发布包存在：{zp.name}", str(zp))
        if zp.exists():
            try:
                with zipfile.ZipFile(zp) as z:
                    names = set(z.namelist())
                    pairs = [("README.md", s_readme), ("使用说明.txt", s_manual)]
                    for arc, expect_txt in pairs:
                        c.ck(arc in names, f"发布包内含 {arc}", f"包内文件：{sorted(names)}")
                        if arc in names:
                            # Windows 文件可能是 CRLF，zip 内是原始字节；
                            # 统一换行后再比较，避免把「仅换行符不同」误判为旧文档
                            got = z.read(arc).decode("utf-8").replace("\r\n", "\n")
                            want = expect_txt.replace("\r\n", "\n")
                            c.ck(got == want,
                                 f"发布包内 {arc} 与仓库版本一致",
                                 "包内是旧文档，请重新组装 zip 并上传")
                    c.ck(not any(n.endswith(".json") for n in names),
                         "发布包不含任何开发机运行数据（*.json）",
                         "发现：" + "、".join(n for n in names if n.endswith(".json")))
            except zipfile.BadZipFile as e:
                c.ck(False, "发布包是有效 zip", str(e))

    return c.report()


if __name__ == "__main__":
    sys.exit(main())
