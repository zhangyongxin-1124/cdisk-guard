# -*- coding: utf-8 -*-
"""生成 C盘守卫应用图标：从母版 icon_source.jpg 抠掉白底 → 输出多尺寸 .ico + 预览图

用法：
    python make_icon.py

产物：
    guard_icon.ico          —— 供 PyInstaller 打包与运行时窗口/托盘图标使用
    guard_icon_preview.png  —— 带棋盘格的透明效果预览，用于 README 与人工核对

说明：
    母版是白色背景的方图（蓝 C + 绿盾 + 白锁，外圈淡绿光晕）。
    抠图分两步走，缺一不可：
      1) 连通性判断：从画布四边出发，把「与边缘连通的白/近白区域」（背景 + 光晕）
         标记为背景。主体内部的白色（锁体）不与边缘连通，因此完整保留——
         若只按白度阈值抠图，锁体会被误抠掉。
      2) 边缘处理：主体边界向内收缩 1px + 高斯羽化做出抗锯齿过渡，
         再按混合比例反解边缘像素的真实主体色（去白污染），
         深色背景下才不会出现灰白毛边。
"""
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "icon_source.jpg"
OUT = HERE / "guard_icon.ico"
PREVIEW = HERE / "guard_icon_preview.png"

# 主体核心阈值：白度 < CORE_T 的像素属于主体（蓝C/绿盾/深色描边）。
# 实测 215 时主体核心完整（光晕白度 >=225 全部排除在外）。
CORE_T = 215
# 连通域扫描网格间距（像素）：把主体所有连通块都标出来，防止 C 与盾
# 因 JPEG 浅缝断开而漏掉其中一块
GRID_STEP = 40
# 抠图后向内收缩 + 羽化，再对边缘 RGB 去白，深色背景上无白边
ERODE_PX = 1
FEATHER_PX = 1.2
PAD_RATIO = 0.03          # 主体外留白比例
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32),
             (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def _erode_feather(alpha: Image.Image, erode_px: int, feather_px: float) -> Image.Image:
    """对 alpha 通道做最小值滤波(收缩) + 高斯羽化，去掉贴着主体的白边。"""
    if erode_px > 0:
        k = erode_px * 2 + 1
        # MinFilter 取邻域最小值 → 边界向内收缩 k//2 像素
        alpha = alpha.filter(ImageFilter.MinFilter(k))
    if feather_px > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather_px))
    return alpha


def _decontaminate(im: Image.Image, alpha: Image.Image) -> Image.Image:
    """边缘去白（decontaminate against white）。

    母版里主体边缘的像素是「主体色 × a + 白 × (1-a)」的混合。
    已知估计出的 a（alpha 通道），反解主体色：
        C_true = (C_obs - 255 * (1 - a)) / a
    不做这一步，混合像素会以"半透明的浅色"留在深色背景上，形成白边。

    逐像素实现（约 150 万像素，几秒内完成；不依赖 numpy / ImageMath）。
    """
    w, h = im.size
    rgb = list(im.getdata())          # [(r,g,b), ...]
    alphas = alpha.tobytes()          # 长度 w*h
    out = []
    n = w * h
    for i in range(n):
        a = alphas[i]
        if a == 0:
            out.append((0, 0, 0, 0))
            continue
        r, g, b = rgb[i]
        if a == 255:
            out.append((r, g, b, 255))
            continue
        af = a / 255.0
        inv = 255.0 * (1.0 - af)
        rr = int((r - inv) / af)
        gg = int((g - inv) / af)
        bb = int((b - inv) / af)
        out.append((max(0, min(255, rr)),
                    max(0, min(255, gg)),
                    max(0, min(255, bb)), a))
    res = im.copy().convert("RGBA")
    res.putdata(out)
    return res


def _flood_bg(wh: Image.Image, tol: int) -> Image.Image:
    """从画布四边 flood fill，返回背景区域掩码（255 = 背景，0 = 非背景）。"""
    marks = wh.copy()
    w, h = marks.size
    seeds = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
             (w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)]
    for xy in seeds:
        # 已被填成 0（背景标记值）的点跳过，避免重复扫描
        if marks.getpixel(xy) == 0:
            continue
        ImageDraw.floodfill(marks, xy, 0, thresh=tol)
    # 填成 0 的即背景；转成 255=背景 / 0=主体的掩码
    return marks.point(lambda v: 255 if v == 0 else 0)


def build_alpha(wh: Image.Image) -> Image.Image:
    """计算透明通道：255=主体（含内部白锁），0=背景与光晕。

    策略（不猜光晕，从主体出发）：
      1) core  —— 白度 < CORE_T 的像素即主体核心，光晕（更白）天然排除；
      2) body  —— 在 core 里做连通域标记（网格播种，C 与盾即使被 JPEG
         浅缝断开也都能收进来）；
      3) holes —— body 之外的像素里，凡是不与画布边缘连通的都是
         主体内部的洞（白色锁体），填回不透明；
      4) 其余（连通到画布边缘的非主体区 = 背景 + 光晕）全部透明。
    """
    w, h = wh.size
    core = wh.point(lambda v: 255 if v < CORE_T else 0)

    # 1+2) 连通域标记（两轮法）：
    #   第一轮逐种子 flood，记录每个连通域的面积与种子；
    #   第二轮只把「面积 >= 最大面积 5%」的域标出来 ——
    #   光晕碎片是独立小域，借此全部排除；C 与盾即使被 JPEG
    #   浅缝断开成两个域，也都在 5% 之上，都会保留。
    body1 = core.copy()
    areas = []            # [(面积, 种子坐标), ...]
    prev = 0
    for y in range(0, h, GRID_STEP):
        for x in range(0, w, GRID_STEP):
            if body1.getpixel((x, y)) == 255:
                ImageDraw.floodfill(body1, (x, y), 64, thresh=127)
                now = body1.histogram()[64]
                areas.append((now - prev, (x, y)))
                prev = now
    if not areas:
        raise SystemExit("主体核心为空，请检查母版或调高 CORE_T")
    max_area = max(a for a, _ in areas)
    keep_seeds = [s for a, s in areas if a >= max_area * 0.05]

    body = core.copy()
    for s in keep_seeds:
        if body.getpixel(s) == 255:
            ImageDraw.floodfill(body, s, 64, thresh=127)
    body_m = body.point(lambda v: 255 if v == 64 else 0)

    # 3) 洞检测：非主体区里，连通到画布边缘的是外部背景；其余是洞
    outside = body_m.point(lambda v: 0 if v else 255)     # 洞 + 外部
    marks = outside.copy()
    seeds = [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
             (w // 2, 1), (w // 2, h - 2), (1, h // 2), (w - 2, h // 2)]
    for xy in seeds:
        if marks.getpixel(xy) == 64:
            continue
        ImageDraw.floodfill(marks, xy, 64, thresh=127)
    holes = marks.point(lambda v: 255 if v == 255 else 0)  # 未连到边缘 = 洞

    return ImageChops.lighter(body_m, holes)


def build_master() -> Image.Image:
    """抠白底 → 裁到主体 → 居中放进带留白的正方形画布"""
    if not SRC.exists():
        raise SystemExit(f"找不到图标母版: {SRC}")

    im = Image.open(SRC).convert("RGB")

    # whiteness = min(R,G,B)：越接近 255 说明越接近白底
    r, g, b = im.split()
    wh = ImageChops.darker(r, ImageChops.darker(g, b))

    # 背景 = 与画布边缘连通的白/近白区域；主体（含内部白锁）全部不透明
    alpha = build_alpha(wh)

    # 收缩 + 羽化：把 JPEG 白边压掉，边缘过渡更自然
    alpha = _erode_feather(alpha, ERODE_PX, FEATHER_PX)

    # 边缘 RGB 去白：反解混合前的主体色，深色背景上不再发灰发白
    im = _decontaminate(im, alpha)

    bbox = alpha.getbbox()
    if not bbox:
        raise SystemExit("抠图后为空，请检查母版")
    im = im.crop(bbox)

    # 居中放进正方形画布（留一点白，避免贴边）
    side = max(im.size)
    pad = max(4, int(side * PAD_RATIO))
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(im, (pad + (side - im.width) // 2,
                       pad + (side - im.height) // 2), im)
    return canvas


def checkerboard(size: int, cell: int = 16) -> Image.Image:
    bg = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            if (x // cell + y // cell) % 2 == 0:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(232, 232, 232))
    return bg


def main():
    master = build_master()
    print("母版处理完成:", master.size, "模式", master.mode)

    # ---- 多尺寸 ico ----
    master.save(OUT, sizes=ICO_SIZES)
    print("已生成:", OUT.name, "尺寸:", [s[0] for s in ICO_SIZES])

    # ---- 预览图：棋盘格上叠加，直观展示透明效果 ----
    pv = 256
    icon = master.resize((pv, pv), Image.LANCZOS)
    bg = checkerboard(pv)
    bg.paste(icon, (0, 0), icon)
    bg.save(PREVIEW)
    print("已生成:", PREVIEW.name)

    # ---- 顺便导出一份透明 PNG，便于网页/文档复用 ----
    png = HERE / "guard_icon_256.png"
    icon.save(png)
    print("已生成:", png.name)


if __name__ == "__main__":
    main()
