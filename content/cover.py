"""
封面图生成器 —— 用 Pillow 绘制 3:4 竖版封面（1080x1440）。

设计要点（依据小红书封面点击率经验）：
  - 主标题 ≤8 字，字号足够大，一眼可读
  - 左上角小标签做系列化锚点（如「搭系统日志」）
  - 中部放 3 条内容提要，填满版面且提升信息密度
  - 底部固定风险提示 + 标签区，强化合规印象
  - 深色底 + 高对比文字，缩略图下依然清晰

不用外部字体依赖，直接用系统字体（Windows 微软雅黑）。
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",    # 黑体
    "C:/Windows/Fonts/Deng.ttf",      # 等线
]

PALETTES = [
    {"bg": (24, 26, 32),   "fg": (255, 255, 255), "accent": (255, 92, 60),  "label": (150, 158, 175)},
    {"bg": (16, 30, 40),   "fg": (255, 255, 255), "accent": (0, 200, 170),  "label": (130, 150, 165)},
    {"bg": (38, 22, 28),   "fg": (255, 255, 255), "accent": (255, 190, 60),  "label": (170, 150, 150)},
    {"bg": (22, 24, 34),   "fg": (255, 255, 255), "accent": (130, 140, 255), "label": (140, 145, 165)},
    {"bg": (18, 34, 26),   "fg": (255, 255, 255), "accent": (140, 220, 110), "label": (130, 160, 140)},
]

W, H = 1080, 1440


def _load_font(size: int, bold: bool = True):
    for path in (FONT_CANDIDATES if bold else FONT_CANDIDATES[1:] + FONT_CANDIDATES[:1]):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(text: str, font, max_width: int, draw) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def make_cover(
    title: str,
    label: str = "搭系统日志",
    out_path: str | Path = None,
    palette_idx: int = 0,
    footer: str = "记录 · 不构成投资建议",
    bullets: list[str] | None = None,
    badge: str = "",
) -> Path:
    """生成一张封面图，返回输出路径。"""
    p = PALETTES[palette_idx % len(PALETTES)]
    img = Image.new("RGB", (W, H), p["bg"])
    d = ImageDraw.Draw(img)

    # ---- 背景：细网格 + 右上角色块 ----
    for i in range(0, H, 90):
        d.line([(0, i), (W, i)], fill=(p["bg"][0] + 8, p["bg"][1] + 8, p["bg"][2] + 8), width=1)
    d.ellipse([W - 250, -230, W + 250, 270], fill=(p["bg"][0] + 6, p["bg"][1] + 6, p["bg"][2] + 6))

    # ---- 左上角系列标签 ----
    label_font = _load_font(44)
    lw = d.textlength(label, font=label_font)
    d.rounded_rectangle([80, 100, 80 + lw + 64, 186], radius=43,
                        fill=(p["accent"][0] // 5 + 26, p["accent"][1] // 5 + 26, p["accent"][2] // 5 + 26))
    d.text((112, 118), label, font=label_font, fill=p["accent"])

    # ---- 右上角序号徽章（系列化锚点） ----
    if badge:
        bfont = _load_font(52)
        bw = d.textlength(badge, font=bfont)
        d.rounded_rectangle([W - 80 - bw - 56, 100, W - 80, 186], radius=43,
                            fill=(p["bg"][0] + 22, p["bg"][1] + 22, p["bg"][2] + 22))
        d.text((W - 80 - bw - 28, 116), badge, font=bfont, fill=p["accent"])

    # ---- 主标题 ----
    size = 112
    title_font = _load_font(size)
    max_w = W - 160
    lines = _wrap(title, title_font, max_w, d)
    while len(lines) > 3 and size > 64:
        size -= 10
        title_font = _load_font(size)
        lines = _wrap(title, title_font, max_w, d)

    y = 268
    line_h = int(size * 1.32)
    for i, ln in enumerate(lines):
        d.text((80, y), ln, font=title_font, fill=p["fg"])
        if i == len(lines) - 1:
            tw = d.textlength(ln, font=title_font)
            d.rectangle([80, y + line_h - 26, 80 + tw, y + line_h - 14], fill=p["accent"])
        y += line_h

    # ---- 中部：3 条内容提要 ----
    bullet_font = _load_font(40, bold=False)
    num_font = _load_font(44)
    by = max(y + 70, 620)
    if bullets:
        d.rectangle([80, by - 44, 86, by - 44 + 3 * 118], fill=p["accent"])
        for i, b in enumerate(bullets[:3]):
            cy = by + i * 118
            d.ellipse([112, cy + 4, 168, cy + 60], outline=p["accent"], width=3)
            nb = str(i + 1)
            d.text((140 - d.textlength(nb, font=num_font) / 2, cy + 12), nb, font=num_font, fill=p["accent"])
            blines = _wrap(b, bullet_font, W - 300, d)
            d.text((196, cy + 16), blines[0], font=bullet_font, fill=p["fg"])
            if len(blines) > 1:
                d.text((196, cy + 68), blines[1], font=bullet_font, fill=p["fg"])

    # ---- 分隔线 ----
    d.rectangle([80, 1150, 1000, 1154], fill=(p["label"][0] // 2, p["label"][1] // 2, p["label"][2] // 2))

    # ---- 底部：风险提示 + 标签 ----
    foot_font = _load_font(38, bold=False)
    d.text((80, 1196), footer, font=foot_font, fill=p["label"])

    tag_font = _load_font(36)
    d.text((80, 1300), "#交易复盘  #量化工具  #AI工作流", font=tag_font, fill=p["label"])

    out = Path(out_path) if out_path else ROOT / "assets" / f"cover_{abs(hash(title)) % 100000}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92, optimize=True)
    return out


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "AI复盘第14天"
    out = make_cover(
        title,
        out_path=sys.argv[2] if len(sys.argv) > 2 else None,
        bullets=["效率对比", "踩坑实录", "方法沉淀"],
        badge="D14",
    )
    print(out)
