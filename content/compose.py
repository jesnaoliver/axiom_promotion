"""
内容组装器 —— 把选题变成可直接发布的 draft.json。

正文骨架（干货类 ≥600 字推荐量更高，正文前 100 字自然埋词 2-3 次）：

  【钩子】1-2 句反直觉结论 / 真实场景切入
  【事件】今天系统里发生了什么（真实素材槽位 {{...}}）
  【过程】我试了哪几种解法 / 卡在哪
  【结论】一句人话收尾 + 方法论沉淀
  【风险提示】+ 【互动提问】

真实素材槽位约定：
  系统里没有的数据一律用 {{}} 占位，脚本会明确报出待补槽位，
  绝不允许 AI 编造具体数字（公约第 5 类：不实信息）。
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.compliance import check_note  # noqa: E402
from content.topic_bank import get_daily_plan  # noqa: E402

ROOT = Path(__file__).parent.parent
DRAFTS = ROOT / "content" / "drafts"

DISCLAIMER = "以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"

# 正文骨架模板。{title} / {angle} / {source} 由组装器填充；
# {{...}} 为待补真实素材槽位。
TEMPLATE_A = """{title}

今天想说的可能有点反常识：{angle}。

先说背景。我不是机构出身，也没有专业资质，就一个自己写工具的普通散户。{source}——这件事本身就够我折腾好几天。

我一开始的思路特别朴素：{hook_ai}

然后就翻车了。{failure}

这里插一句，如果你也在用 AI 做这类事，我踩的坑大概是同一个：{lesson}

后来我改了三件事：

1️⃣ {fix1}
2️⃣ {fix2}
3️⃣ {fix3}

改完之后，{result}

说点方法论。我现在判断一个 AI 产出可不可信，只看一件事：{method}

顺便说下这个工具我还在持续搭，目前的形态是一个能自动做交易复盘、能跑策略回测、也能回答回测相关问题的小系统。后续的迭代过程我都会如实记录下来，包括翻车的部分。

{disclaimer}

{question}"""

TEMPLATE_B = """{title}

这个结论我一开始完全不信，直到系统跑完才知道：{angle}。

先交代实验口径，免得看到数字误会：

📌 数据区间：[[区间]]
📌 样本数量：[[笔数]]
📌 成本假设：[[成本假设]]
📌 性质：模拟回测，非实盘，不代表未来表现

我原本的预期是：{expectation}

跑出来完全不是这样。{result}

拆开看，问题出在：{diagnosis}

我把参数一个个拆开测之后，发现最要命的其实是这个：{key_factor}

这里有几个容易踩的坑，我全踩过：

❌ {pitfall1}
❌ {pitfall2}
❌ {pitfall3}

现在我的回测报告里，固定会多打印这几项：[[报告字段]]

最后说一句方法论上的事：{method}

{disclaimer}

{question}"""

# ---------------------------------------------------------------- 槽位定义
#
# 每条线各自的必填槽位（必须来自 content/facts/<topic_id>.json 的真实素材）。
# A 线（搭系统日志）：8 个
# B 线（回测科普）：8 个 + 4 个口径槽位（口径不填会被合规闸门拦截）
#
# 注意：DEFAULT_SLOTS 必须按线拆开。若合成一个 dict，
# 同名键（如 method）会被后定义者覆盖，导致 A 线笔记被塞进 B 线的方法论。
REQUIRED_SLOTS = {
    "A": ["hook_ai", "failure", "lesson", "fix1", "fix2", "fix3", "result", "method"],
    "B": ["expectation", "result", "diagnosis", "key_factor",
          "pitfall1", "pitfall2", "pitfall3", "method"],
}

# B 线正文开头的 4 个口径槽位：必须填，否则触发「误导性信息」拦截
B_CALIBER_SLOTS = ["区间", "笔数", "成本假设", "报告字段"]

DEFAULT_SLOTS_A = {
    "hook_ai": "想让 AI 直接把复盘报告写出来，省掉每天半小时的整理",
    "failure": "它把我的记录写得特别顺，顺到有点不对——全是正确的废话",
    "lesson": "AI 最擅长的不是分析，是把「没有结论」写得像「有结论」",
    "fix1": "把输出格式改成固定字段，让它没有自由发挥的空间",
    "fix2": "所有它给出的判断，都必须能回指到我的原始记录里",
    "fix3": "加了一个「事实 / 推断」的分栏，把两者物理隔开",
    "result": "同样的数据，现在能看出到底哪一步走偏了",
    "method": "它说的每句话，我能不能在原始记录里找到出处",
}

DEFAULT_SLOTS_B = {
    "expectation": "这个策略至少应该是正的，结果就算不佳也不至于翻盘",
    "result": "结果和我的预期差得很远，得回头逐条看才知道问题出在哪",
    "diagnosis": "单看一个指标完全正常，组合起来才是问题",
    "key_factor": "我把成本假设调高一点，结论就整个翻转了",
    "pitfall1": "默认停牌可以直接交易",
    "pitfall2": "忘了算滑点，或者随手填个 0",
    "pitfall3": "用同一份数据调参又用它验证",
    "method": "先问「这个结论在什么条件下会失效」，再问「这个结论是什么」",
}

# 向后兼容：合并视图（仅供外部只读引用，勿再按单一 dict 使用）
DEFAULT_SLOTS = {**DEFAULT_SLOTS_A, **DEFAULT_SLOTS_B}

QUESTIONS = {
    "A": "你用 AI 做复盘或研究时，最容易踩的坑是什么？评论区挑几个我回复",
    "B": "你做回测时踩过最离谱的坑是什么？评论区说出来，我挨个看",
}

HOOKS = {
    "A": "我一开始觉得这事很简单，不就是让 AI 写个总结",
    "B": "我原本挺相信这个指标的，直到系统跑出来",
}


def fill_template(topic: dict, slots: dict | None = None) -> tuple[str, list[str]]:
    """
    填充正文骨架，返回 (正文, 未填槽位列表)。

    槽位来源分三层，优先级从低到高：
      1. 该线的默认骨架（DEFAULT_SLOTS_A / _B）—— 保证「无真实素材也能跑通」
      2. content/facts/<topic_id>.json 提供的真实素材
      3. 模板中残留的 {{...}} 占位符（仅 B 线口径槽位会残留）

    missing 列表同时反映两类情况：
      - 正文里仍是 {{...}} 的口径占位符（B 线）
      - 该线必填槽位中「没有真实素材覆盖」的槽位（A 线全靠默认值时）
    """
    track = topic["track"]
    tpl = TEMPLATE_A if track == "A" else TEMPLATE_B
    defaults = DEFAULT_SLOTS_A if track == "A" else DEFAULT_SLOTS_B

    merged = dict(defaults)
    provided = set()
    if slots:
        for k, v in slots.items():
            if v is None:
                continue
            merged[k] = v
            provided.add(k)

    fields = {k: v for k, v in merged.items() if k != "hook_ai"}
    fields.update({
        "title": "",  # 正文不重复标题
        "angle": topic.get("angle", ""),
        "source": topic.get("source", ""),
        "disclaimer": DISCLAIMER,
        "question": QUESTIONS[track],
        "hook_ai": merged.get("hook_ai", HOOKS["A"]),
    })
    body = tpl.format(**fields)
    body = body.strip()

    # B 线口径槽位：把 facts 里的真实值填进 [[...]]
    # 注意不能用 {{...}}：str.format 会把 {{ 折叠成 { ，占位符会失去可识别性。
    if track == "B":
        for key in B_CALIBER_SLOTS:
            val = slots.get(key) if slots else None
            if val:
                body = body.replace("[[%s]]" % key, str(val))

    # 找出仍未填的占位符（硬占位符，必须补真实素材才能发布）
    missing = re.findall(r"\[\[(.+?)\]\]", body)

    # 该线必填槽位中，没有真实素材覆盖的（仅有默认骨架兜底）
    uncovered = [s for s in REQUIRED_SLOTS.get(track, []) if s not in provided]
    for s in uncovered:
        if s not in missing:
            missing.append(s)

    return body, missing


def build_draft(topic: dict, slots: dict | None = None, images: list[str] | None = None) -> dict:
    body, missing = fill_template(topic, slots)
    draft = {
        "id": topic["id"],
        "title": topic["title"],
        "body": body,
        "tags": topic["tags"],
        "images": images or [],
        "cover_text": topic["cover"],
        "track": topic["track"],
        "missing_slots": missing,
    }
    return draft


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    DRAFTS.mkdir(parents=True, exist_ok=True)
    plan = get_daily_plan(days)

    print(f"生成 {days} 天排期草稿\n" + "=" * 70)
    for i, topic in enumerate(plan, 1):
        draft = build_draft(topic)
        path = DRAFTS / f"day{i:02d}_{topic['id']}.json"
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

        report = check_note({
            "title": draft["title"], "body": draft["body"],
            "tags": draft["tags"], "images": draft["images"],
        })
        status = "PASS" if report["passed"] else "BLOCK"
        print(f"Day{i} [{status}] {topic['id']} {topic['title']}")
        print(f"   正文 {report['checked_fields']['body_len']} 字 | "
              f"标签 {report['checked_fields']['tag_count']} | "
              f"待补槽位 {len(draft['missing_slots'])}")
        for v in report["violations"]:
            print(f"   ⛔ [{v['category']}] {'、'.join(map(str, v['hits']))}")
        for w in report["warnings"]:
            print(f"   ⚠️  [{w['category']}] {w['advice']}")
        print()

    print("=" * 70)
    print(f"草稿目录: {DRAFTS}")
    print("下一步：填真实素材 → 生成封面 → 合规复检 → 发布")


if __name__ == "__main__":
    main()
