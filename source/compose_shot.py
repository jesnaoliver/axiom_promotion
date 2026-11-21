"""
把系统截图加工成小红书笔记配图。

【为什么要加工，而不是直接发截图】

1. **比例**：小红书图文最优是 3:4 竖版（1080×1440）。原始截图是网页全页，
   直接发会缩得看不清字。
2. **要有信息层**：纯截图用户看不懂这是什么工具。
   做成「标题条 + 截图 + 底部要点条」的三段式，才是小红书配图的样子。
3. **必须自己做安全兜底**：图片里的文字平台要做 OCR 才能识别，
   文字违规能被文本审核拦、图片不行。所以图上文字必须自己也过合规闸门。

【版式】1080×1440
    ┌──────────────────────────┐ 0
    │  标题区（纯色底）          │ ~150
    ├──────────────────────────┤
    │                          │
    │  截图区                  │ ~1000
    │                          │
    ├──────────────────────────┤
    │  要点条（纯色底）          │ ~290
    └──────────────────────────┘ 1440
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.compliance import check_note  # noqa: E402

ROOT = Path(__file__).parent.parent
SHOTS = ROOT / "assets" / "screenshots"
OUT = ROOT / "assets" / "composed"

W, H = 1080, 1440
HEAD_H = 168
FOOT_H = 300
BODY_H = H - HEAD_H - FOOT_H

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"

BG = (17, 19, 26)
CARD = (28, 31, 40)
FG = (255, 255, 255)
SUB = (168, 176, 194)
ACCENT = (255, 92, 60)
LINE = (44, 48, 60)


def _font(size, bold=True):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def compose(src: Path, out_name: str, *, title: str, subtitle: str,
            footer: str, crop: str = "left", notes: list[str] | None = None,
            brand: str = "AXIOM") -> Path:
    """把截图加工成 3:4 笔记配图（三段式版式）。"""
    # ---- 合规：标注文案先过闸门 ----
    probe = check_note({"title": title, "body": subtitle + " " + footer,
                        "tags": [], "images": ["x.jpg"]})
    if not probe["passed"]:
        raise ValueError(f"标注文案未通过合规检查: {probe['violations']}")

    img = Image.open(src).convert("RGB")
    sw, sh = img.size

    # ---- 裁掉不需要的区域 ----
    if crop == "left":
        # 保留中间日报区（去掉左侧导航 + 右侧研报区）
        # 左导航约占 15%，日报区 15%~52%（多留一点右边界让段落不被切断）
        # 纵向：从"今日日报"卡片开始（跳过 Dashboard/每日概览 的空白头部）
        top = int(sh * 0.085)
        img = img.crop((int(sw * 0.145), top, int(sw * 0.545), sh))
    elif crop == "left_nav":
        # 保留导航 + 日报区（用于展示"这是个有完整菜单的工具"）
        img = img.crop((0, 0, int(sw * 0.545), sh))
    elif crop == "dialog":
        # 裁掉底部的版权行（"© 2026 AXIOM. All rights reserved."）
        img = img.crop((0, 0, sw, int(sh * 0.93)))

    # ---- 缩放填满正文区 ----
    # 关键：不能直接"放大到宽度"再居中裁切 —— 那会把段落右侧切断。
    # 正确做法：**按宽度整体缩放**（允许上下留白由背景填充），
    # 这样每行文字都完整可见，只是上下可能露出底色。
    ratio = W / img.width
    img = img.resize((W, int(img.height * ratio)), Image.LANCZOS)
    if img.height < BODY_H:
        # 高度不足 → 补底色
        bg_img = Image.new("RGB", (W, BODY_H), CARD)
        bg_img.paste(img, (0, 0))
        img = bg_img
    else:
        img = img.crop((0, 0, W, BODY_H))

    # ---- 画布 ----
    canvas = Image.new("RGB", (W, H), BG)
    canvas.paste(img, (0, HEAD_H))
    d = ImageDraw.Draw(canvas)

    # ---- 底部要点区 ----
    # 布局要点：要点条与风险提示**永远不能重叠**。
    # 所以底部区高度按"要点条实际需要的高度"来定，剩余空间归截图区。
    notes = (notes or [])[:4]
    note_h = 46 * len(notes) + 46          # 每条 46px + 上下留白
    foot_h = note_h + 110                  # 再加风险提示行
    foot_h = min(foot_h, H - HEAD_H - 120)  # 至少留 120px 给截图
    foot_h = max(foot_h, 150)
    foot_top = H - foot_h

    d.rectangle([0, foot_top, W, H], fill=CARD)
    d.rectangle([0, foot_top, W, foot_top + 3], fill=LINE)

    # 头部
    d.rectangle([0, 0, W, HEAD_H], fill=CARD)
    tf = _font(56)
    y = 34
    for ln in _wrap(d, title, tf, W - 200):
        d.text((54, y), ln, font=tf, fill=FG)
        y += 68
    sf = _font(30, bold=False)
    d.text((54, y + 4), subtitle, font=sf, fill=SUB)
    # 品牌角标
    bf = _font(30)
    bw = d.textlength(brand, font=bf)
    d.text((W - 54 - bw, 40), brand, font=bf, fill=ACCENT)

    # 分隔线
    d.rectangle([0, HEAD_H - 3, W, HEAD_H], fill=LINE)

    nf = _font(31, bold=False)
    y = foot_top + 30
    for n in notes:
        d.ellipse([56, y + 11, 70, y + 25], fill=ACCENT)
        d.text((90, y), n, font=nf, fill=(224, 230, 242))
        y += 46

    # 底部说明 + 风险提示
    ff = _font(27, bold=False)
    d.text((54, H - 66), footer, font=ff, fill=(140, 148, 166))

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{out_name}.jpg"
    canvas.save(out, quality=92, optimize=True)
    return out


SPECS = {
    "dashboard": {
        "src": SHOTS / "axiom_dashboard.png",
        "out": "shot_dashboard",
        "title": "我的投研日报长这样",
        "subtitle": "宏观 / 行业 / 公司 / 风险，四段结构自动生成",
        "footer": "只讲方法与结构，不讲个股 · 不构成投资建议",
        "crop": "left",
        # 【文案必须与图上实际可见的内容一致】
        # 历史问题：这里写"每段都标了置信度"，但 DOM 探针证实
        # high/medium 徽章只渲染在右侧「本周周报」栏（x≥1204），
        # 日报卡片内部并不显示置信度徽章 ——
        # 也就是说配图上根本没有这条信息，文案属于失实描述。
        # 现在只描述图上真实可见的内容。
        # 校验过 3:4 裁剪后的实际可见范围：
        #   ✓ 「今日日报」标题、板块名（宏观与海外速览 / A股行业聚焦 …）
        #   ✓ 每段下面的 bullet 要点列表
        #   ✗ 卡片右上角日期被裁掉大半（只剩 "2026-11-"）
        #   ✗ 「重点公司观点」「风险信号」段在可见范围之外
        # 副标题里的"宏观/行业/公司/风险"是日报的**整体**四段结构（真实存在，
        # 只是后两段没被裁进来），作为结构说明可以写；
        # 但要点条必须只讲画面里看得见的东西。
        "notes": ["长文被拆成一条条要点",
                  "要点前面就写清了这段讲什么",
                  "不用自己再翻长文找重点"],
    },
    "intel_list": {
        "src": SHOTS / "axiom_intel_list.png",
        "out": "shot_intel_list",
        "title": "信息太多？我的流程是三层",
        "subtitle": "先分类 → 再摘要 → 最后才看原文",
        "footer": "效率工具的用法分享 · 不构成投资建议",
        "crop": "full",
        # 【文案必须与图上实际可见的内容一致】
        # 实测本场景可见的表格列是：标题 / 作者 / 群组名称 / 一句话总结 /
        # 版本号 / 原文 / 收录时间 / 发布日期，以及底部"共 15778 条记录"。
        # **没有"分类"列** —— 分类统计来自接口字段，图上并不显示。
        # 旧文案"分类统计告诉我哪类信息突然变多"属于图上无法验证的描述，
        # 与 dashboard 那次"每段都标了置信度"是同一类失实，已改掉。
        # 现在只写图上真看得见的东西：每行都带一句话摘要、按日期筛。
        "notes": ["每行都配一句话摘要，不用点开原文", "按发布日期筛，噪音先少一半"],
    },
    "intel_detail": {
        "src": SHOTS / "axiom_intel_detail.png",
        "out": "shot_intel_detail",
        "title": "一篇长文，AI 拆成结构化要点",
        "subtitle": "核心观点 / 关键要点 / 结论，一屏看完",
        "footer": "工具演示 · 不构成投资建议",
        "crop": "dialog",
        # 图上可见：核心观点 / 推理前提 / 结论 / 中层观点 等结构标签，
        # 正文段落已整段隐去 —— 所以"原文留在工具里"是字面真实的。
        "notes": ["原文留在工具里，这里只讲方法"],
    },
    "agent": {
        "src": SHOTS / "axiom_agent.png",
        "out": "shot_agent",
        "title": "用自然语言问行情/复盘/回测",
        "subtitle": "一句话提问，工具自己去查数据",
        "footer": "AI 工作流分享 · 不构成投资建议",
        "crop": "full",
        "notes": ["我更关心它该去看什么，而不是它说了什么"],
    },
}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="all")
    args = ap.parse_args()

    todo = SPECS if args.scene == "all" else {args.scene: SPECS[args.scene]}
    for name, spec in todo.items():
        src = spec["src"]
        if not src.exists():
            print(f"❌ 源截图不存在: {src}")
            print(f"   先运行: python source/screenshot.py --scene {name}")
            continue
        try:
            p = compose(src, spec["out"], title=spec["title"],
                        subtitle=spec["subtitle"], footer=spec["footer"],
                        crop=spec["crop"], notes=spec.get("notes"))
            print(f"✅ {p}")
        except Exception as e:
            print(f"❌ {name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
