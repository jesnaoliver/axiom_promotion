"""
为 AXIOM 生成的内容批量做封面图。

复用 content/cover.py 的绘制逻辑，但标签组换成"方法论"系列。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.cover import make_cover  # noqa: E402

ROOT = Path(__file__).parent.parent
SRC = ROOT / "content" / "from_axiom"
ASSETS = ROOT / "assets"

# 每篇的封面文案（≤8字）与 3 条提要
#
# 【重要】提要里凡涉及数字的，必须来自当天真实数据 —— 由 scheduler 传入的
# stats 覆盖（见 scheduler.build_from_axiom）。这里的默认值只作兜底，
# 数字取自真实接口的典型量级，不写死某个具体日期的值。
COVER_SPEC = {
    "conf": {
        "text": "它主动说不确定",
        "bullets": ["每段都标了置信度", "低置信度说清了原因", "我反而更信它了"],
        "label": "搭系统日志",
    },
    "sample": {
        "text": "看不到样本量就不信",
        "bullets": ["踩过的坑：只看结论", "加了一条硬规则", "真正动手的只剩几段"],
        "label": "搭系统日志",
    },
    "window": {
        "text": "答案有多新是另一回事",
        "bullets": ["日报自己标了数据窗口", "跨日≠自然日", "过期结论最坑"],
        "label": "搭系统日志",
    },
    "intel": {
        "text": "稀缺的是结构",
        "bullets": ["信息多到看不完", "先分类再筛选", "分类突变=统计信号"],
        "label": "信息整理",
    },
    "research": {
        "text": "按类型分研报",
        "bullets": ["按行业分=按内容堆", "按类型分=按用法分", "效率差一个量级"],
        "label": "研报方法论",
    },
    "howto": {
        "text": "改问法提质量",
        "bullets": ["别问答案", "问该看什么", "拆到能动手的粒度"],
        "label": "AI工作流",
    },
}


def cover_bullets_for(note: dict, spec: dict) -> list[str]:
    """
    生成封面提要。

    数字全部来自当天真实数据（note["conf_stats"] / note["daily_count"]），
    绝不使用写死的具体数值 —— 否则图文会互相矛盾。
    """
    nid = note.get("id", "")
    stats = note.get("conf_stats") or {}
    daily = note.get("daily_count") or 0

    if nid.endswith("conf"):
        low = stats.get("low", 0)
        med = stats.get("medium", 0)
        high = stats.get("high", 0)
        bullets = [f"今天{low}低/{med}中/{high}高"]
        if daily:
            bullets.append(f"日报已跑通第{daily}期")
        bullets.append("我反而更信它了")
        return bullets[:3]
    if nid.endswith("intel"):
        return spec["bullets"]
    return spec["bullets"]


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    count = 0
    for p in sorted(SRC.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        nid = d["id"]
        # 从 id 尾部匹配规格
        spec = None
        for key, v in COVER_SPEC.items():
            if nid.endswith(key):
                spec = v
                break
        if not spec:
            spec = COVER_SPEC["conf"]

        out = ASSETS / f"cover_{nid}.jpg"
        make_cover(
            title=spec["text"],
            label=spec["label"],
            out_path=out,
            palette_idx=count % 5,
            footer="记录 · 不构成投资建议",
            bullets=spec["bullets"],
            badge="AX",
        )
        d["cover"] = str(out)
        d["images"] = [str(out)]
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ {nid}  {spec['text']}  -> {out.name}")
        count += 1
    print(f"\n共 {count} 张封面")


if __name__ == "__main__":
    main()
