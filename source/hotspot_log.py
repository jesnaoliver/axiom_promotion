"""
热点记录表读写 —— 热点记录.xlsx。

【表结构（实测自用户提供的样例）】
Sheet1:  | 日期 | 话题 | 板块 |
        | 20261116 | 半导体设备大幅拉升，东方嘉盛涨停 | 半导体 |
Sheet2/Sheet3: 空

- 日期：YYYYMMDD 数字（如 20261116，不是 2026-11-16）
- 话题：雪球热门话题原文（含个股名 —— 内部台账，用户明确要求原样记录）
- 板块：中文板块短名（半导体 / 家电 / 人形机器人…）

【重要区分：表格 vs 雪球正文】
- 表格里保留话题原文（含"东方嘉盛涨停"这类个股名）→ 内部台账，可追溯
- 雪球正文里绝不能出现个股名 → 由 content/xq_compliance.py 拦截
  所以表格存的"话题"是原始素材，发帖时必须经过"公开描述"转换。

【公开描述的生成思路：板块词锚点分段】
不用"裁剪"（脆弱），而是按板块词锚点找可公开的那一段：

  "半导体设备大幅拉升，东方嘉盛涨停"
      -> 以"半导体"为锚点，取到下一个分隔符之前 -> "半导体设备大幅拉升"
  "家电板块反复活跃，长虹美菱2连板"
      -> "家电板块反复活跃"
  "英伟达盘中大跌，股价创阶段新低"
      -> 无板块词锚点 -> 用映射板块"电子"造描述 -> "半导体/电子方向今日走弱"
  "宇树科技大幅下挫，人形机器人概念表现疲软"
      -> 锚点"人形机器人" -> "人形机器人概念表现疲软"
"""

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import openpyxl
from openpyxl.styles import Alignment, Font

ROOT = Path(__file__).parent.parent
XLSX = ROOT / "热点记录.xlsx"

HEADER = ["日期", "话题", "板块"]

# 通用现象词（当话题没有板块锚点时，用来造中性描述）
GENERIC_MOVES = [
    ("大涨", "走强"), ("大跌", "走弱"), ("大幅拉升", "走强"),
    ("大幅下挫", "走弱"), ("拉升", "走强"), ("下挫", "走弱"),
    ("涨停", "走强"), ("跌停", "走弱"), ("上涨", "走强"), ("下跌", "走弱"),
    ("走强", "走强"), ("走弱", "走弱"), ("活跃", "表现活跃"),
    ("异动", "表现活跃"),
]


def _split_segments(topic):
    """按中文标点切段。"""
    parts = re.split(r"[，,、；;。]", topic)
    return [p.strip() for p in parts if p.strip()]


def to_public_desc(topic, sectors=None, hot_stocks=None):
    """
    把话题原文转成可公开的板块描述（不含个股名）。

    核心规则（简化版，踩过两次过度剥离的坑）：
      **只保留含板块词的段，整段保留，不做尾部剥离。**

    为什么不用尾部剥离：
      "半导体设备大幅拉升" -> 剥离会误删成"半"
      "AI眼镜概念拉升"     -> 剥离会误删成"A"
    而纯个股段（"东方嘉盛涨停"）**根本不含板块词**，本来就不会被选中，
    所以尾部剥离是多余且有害的。

    可选的热股榜交叉验证仍保留：若段内含当日热股榜个股名则删掉该名
    （处理"中芯国际半导体大涨"这种混排）。
    """
    from source.sector_map import sector_keyword_in, sector_display

    segments = _split_segments(topic)

    # 1) 找含板块词的段（第一个命中即可）
    best_seg = None
    for seg in segments:
        if sector_keyword_in(seg):
            best_seg = seg
            break

    if best_seg:
        desc = best_seg
        # 2) 可选：热股榜交叉验证（处理板块词与个股混排的情况）
        if hot_stocks:
            for name in hot_stocks:
                if name and name in desc:
                    desc = desc.replace(name, "")
            desc = re.sub(r"^[，,、\s]+|[，,、\s]+$", "", desc)
        if desc.strip():
            return desc.strip()

    # 3) 兜底：用板块 + 通用现象
    sectors = sectors or []
    name = sector_display(sectors, topic) or "相关板块"
    move = ""
    for kw, rep in GENERIC_MOVES:
        if kw in topic:
            move = rep
            break
    if not move:
        move = "出现波动"
    return f"{name}方向今日{move}"


def load_workbook():
    if XLSX.exists():
        try:
            return openpyxl.load_workbook(XLSX)
        except Exception:
            pass
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADER)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 52
    ws.column_dimensions["C"].width = 16
    return wb


def get_sheet(wb):
    """取第一个有正确表头的工作表，没有就用第一个。"""
    for ws in wb.worksheets:
        if ws.max_row >= 1:
            first = [str(c.value or "").strip() for c in ws[1][:3]]
            if first[:3] == HEADER:
                return ws
    return wb.worksheets[0]


def _pending_files():
    """列出所有待合并的备用文件。"""
    return sorted(XLSX.parent.glob("热点记录_pending_*.xlsx"))


def merge_pending() -> dict:
    """
    把待合并的备用文件并回主表。

    每次运行流程都会先调用一次 ——
    用户哪天关掉 WPS/Excel，锁释放后跑一次就自动归位了。

    返回 {"merged": n, "files": [...], "ok": bool}
    """
    files = _pending_files()
    if not files:
        return {"merged": 0, "files": [], "ok": True}

    merged = 0
    still_locked = False
    for fb in files:
        try:
            fb_wb = openpyxl.load_workbook(fb)
            fb_ws = fb_wb.worksheets[0]
            pending = []
            for r in fb_ws.iter_rows(min_row=2, values_only=True):
                if r and r[0] is not None:
                    pending.append([r[0], r[1], r[2]])
            if not pending:
                fb.unlink()
                continue

            res = append_rows([{"date": str(a), "topic": str(b), "sector": str(c)}
                               for a, b, c in pending])
            if res.get("fallback"):
                # 还是锁着，保留备用文件
                still_locked = True
                continue
            merged += res.get("added", 0)
            # 合并成功 → 删除备用文件
            try:
                fb.unlink()
            except Exception:
                pass
        except Exception:
            still_locked = True

    return {"merged": merged, "files": [str(f) for f in files], "ok": not still_locked}


def append_rows(rows):
    """
    追加热点记录。

    rows: [{"date": "20261117", "topic": "...", "sector": "..."}]
    返回 {"added": n, "skipped": n, "path": str, "fallback": str|None}

    ⚠️ **容错**：如果 xlsx 正被 WPS/Excel 打开，会抛 PermissionError
       （Windows 文件锁）。这时**不能中断整个发帖流程**，
       改为写入同目录备用文件 `热点记录_pending_YYYYMMDD.xlsx`，
       并在返回值里带上 fallback 路径提示用户合并。
    """
    wb = load_workbook()
    ws = get_sheet(wb)

    existing = set()
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[0] is None:
            continue
        existing.add((str(r[0]).strip(), str(r[1] or "").strip()))

    to_write = []
    for item in rows:
        d = str(item.get("date") or "").strip()
        t = str(item.get("topic") or "").strip()
        s = str(item.get("sector") or "").strip()
        if not d or not t:
            continue
        key = (d, t)
        if key in existing:
            continue
        existing.add(key)
        to_write.append([d, t, s])

    if not to_write:
        return {"added": 0, "skipped": len(rows), "path": str(XLSX),
                "fallback": None}

    for r in to_write:
        ws.append(r)

    try:
        wb.save(XLSX)
        return {"added": len(to_write), "skipped": len(rows) - len(to_write),
                "path": str(XLSX), "fallback": None}
    except PermissionError:
        # 文件被占用 → 写备用文件
        fb = XLSX.parent / f"热点记录_pending_{datetime.now().strftime('%Y%m%d')}.xlsx"
        fb_wb = openpyxl.Workbook()
        fb_ws = fb_wb.active
        fb_ws.title = "待合并"
        fb_ws.append(HEADER)
        for c in fb_ws[1]:
            c.font = Font(bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center")
        fb_ws.column_dimensions["A"].width = 14
        fb_ws.column_dimensions["B"].width = 52
        fb_ws.column_dimensions["C"].width = 16
        for r in to_write:
            fb_ws.append(r)
        fb_wb.save(fb)
        return {"added": 0, "skipped": len(rows),
                "path": str(XLSX), "fallback": str(fb),
                "pending": to_write}


def read_rows():
    """读出全部记录。"""
    if not XLSX.exists():
        return []
    wb = load_workbook()
    ws = get_sheet(wb)
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[0] is None:
            continue
        out.append({
            "date": str(r[0]).strip(),
            "topic": str(r[1] or "").strip(),
            "sector": str(r[2] or "").strip(),
        })
    return out


if __name__ == "__main__":
    from source.sector_map import infer_sectors

    print("=== 当前记录 ===", flush=True)
    for r in read_rows():
        print(f"  {r['date']} | {r['topic']} | {r['sector']}", flush=True)

    print("\n=== 公开描述生成自测 ===", flush=True)
    samples = [
        "半导体设备大幅拉升，东方嘉盛涨停",
        "家电板块反复活跃，长虹美菱2连板",
        "英伟达盘中大跌，股价创阶段新低",
        "AI眼镜概念拉升，瀛通通讯涨停",
        "电网设备概念活跃，顺钠股份涨停",
        "冰雪经济概念活跃，龙江交通涨停",
        "宇树科技大幅下挫，人形机器人概念表现疲软",
        "上证指数开盘跌破3600点",
    ]
    for t in samples:
        secs = infer_sectors(t)
        print(f"  {t}")
        print(f"     -> {to_public_desc(t, secs)}", flush=True)
