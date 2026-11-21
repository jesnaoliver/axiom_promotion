"""
从 AXIOM 真实数据生成小红书内容。

【核心设计原则】

这条链路最重要的一个发现：**最有说服力的内容不是"我的工具有多神"，
而是"我的工具在什么时候知道自己不知道"。**

AXIOM 日报里每一段都带 `confidence_level`（low/medium/high）和样本量，
比如"当日宏观样本仅1篇，结论可信度受覆盖面限制"、
"当日有效行业样本为 0，暂无可提炼的机构行业观点"。

这种"敢承认数据不够"的态度：
  · 天然规避了《社区金融生态公约》的"误导性信息"（不夸大、有口径）
  · 是最强的信任信号 —— 读者看到 AI 敢说"我不知道"，才敢信它说"我知道"的时候
  · 完全不涉及个股、买卖点、收益承诺

所以内容主线设计为：**「一个会主动说"这个我不确定"的 AI，是怎么被我用在日常复盘里的」**

每篇笔记的结构：
  1. 钩子：反直觉的真实现象（来自当日数据的 confidence/样本量）
  2. 场景：我的系统今天拿到了什么
  3. 机制：它是怎么判断"够不够"的（这是工具能力，也是方法论）
  4. 我的用法：怎么用这个"不确定度"改进决策
  5. 边界：明确不做什么（合规 + 诚实）
  6. 互动：抛一个具体问题

【铁律】
  · 不出现任何具体公司名、股票代码
  · 不出现任何方向性市场判断
  · 不出现任何站外域名/联系方式
  · 品牌只做"工具署名"，不做"引导动作"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from source.axiom_api import AxiomClient  # noqa: E402
from source.sanitize import (  # noqa: E402
    brand_line, forbidden_check, sanitize_dashboard,
    sanitize_intelligence, sanitize_research, sanitize_text,
)

ROOT = Path(__file__).parent.parent
OUT = ROOT / "content" / "from_axiom"

# 置信度 → 中文表述 + 内容价值判断
CONF_TEXT = {
    "low": "低置信度",
    "medium": "中等置信度",
    "high": "高置信度",
}

# ---------------------------------------------------------------- 真实素材提取

# 「自我限定」标记 —— 命中即说明这句话在讲**自己的信息不够**，而不是在讲行情。
# 只用这类句子，才能证明"AI 主动说自己不确定"这个事实。
HEDGE_MARKERS = (
    "样本", "覆盖", "无法形成", "暂无可提炼", "未覆盖", "未提供", "未给出",
    "未涉及", "有待", "尚未", "受限于", "不足以", "不能", "未能", "不等于",
    "不代表", "受限", "仅1篇", "仅 1 篇", "为 0", "为0", "不跨日",
    "结论可信度", "观点覆盖面", "完整性受限", "暂不", "不做",
)

# 反向排除：命中即视为"在讲行情/在给方向"，不能当"自我限定"的证据。
# 这一步是为了防止把「政策托底不等于趋势反转」这种行情判断当成诚实证据。
MARKET_MARKERS = (
    "反弹", "反弹信号", "修复", "上行", "下行", "企稳", "筑底", "见底", "见顶",
    "底部", "顶部", "托底", "去杠杆", "波动放大", "补跌", "增量", "减仓",
    "加仓", "布局", "主线", "弹性", "催化", "兑现", "业绩", "扩产", "放量",
    "涨价", "降价", "增持", "减持", "配置", "风险偏好", "拥挤", "超配", "低配",
    "反弹", "持续主导", "定价", "基本面", "景气", "赛道",
)


def _pick_hedge_quotes(sections: list[dict], limit: int = 3) -> list[str]:
    """
    从日报真实内容里挑出「AI 主动交代自己信息不够」的句子。

    规则（缺一不可）：
      1. 命中 HEDGE_MARKERS —— 确实在讲样本/覆盖/不确定
      2. 不命中 MARKET_MARKERS —— 不是在讲行情方向
      3. 过 forbidden_check —— 没有公司名、代码、导流词
      4. 长度 8~60 —— 是一句话，能当引文念出来
    """
    out: list[str] = []
    for sec in sections:
        cands = list(sec.get("key_points") or [])
        content = sec.get("section_content") or ""
        for piece in cands + [content]:
            for sent in piece.replace("；", "。").split("。"):
                s = sent.strip()
                if not (8 <= len(s) <= 60):
                    continue
                if not any(m in s for m in HEDGE_MARKERS):
                    continue
                if any(m in s for m in MARKET_MARKERS):
                    continue
                if forbidden_check(s):
                    continue
                if s in out:
                    continue
                out.append(s)
                if len(out) >= limit:
                    return out
    return out


def _daily_report_count(client) -> int:
    """AXIOM 已产出的日报期数（去重后的 reportDate 数量）—— 真实统计，不写死。"""
    try:
        rows = client.daily_history(page=0, page_size=100)
    except Exception:
        return 0
    dates = {
        r.get("reportDate") for r in (rows or [])
        if isinstance(r, dict) and r.get("reportDate")
        and (r.get("frequency") in (None, "daily"))
    }
    return len(dates)

# 标签组：只放安全词，绝不放个股/研报/星球相关词
TAGS_SAFE = [
    "AI工具", "效率工具", "交易复盘", "自动化",
    "量化学习", "投资复盘", "工作流", "AI工作流",
]
TAGS_SEARCH = [
    "回测", "量化交易", "Python量化", "量化学习",
    "策略回测", "程序化交易", "金融工程", "投资复盘",
]


def _fmt_conf(level: str) -> str:
    return CONF_TEXT.get(level, level or "未知")


def gen_from_dashboard(dash: dict) -> list[dict]:
    """从每日概览生成笔记。"""
    sd = sanitize_dashboard(dash)
    secs = sd.get("sections", [])
    if not secs:
        return []

    date = sd.get("report_date", "")
    window = sd.get("data_window", [])

    notes = []

    # --- 笔记 1：置信度机制 ---
    confs = [(s["section_title"], s["confidence_level"], s["source_titles_count"])
             for s in secs]
    low_sections = [c for c in confs if c[1] == "low"]
    med_sections = [c for c in confs if c[1] == "medium"]
    high_sections = [c for c in confs if c[1] == "high"]

    # 引文必须来自**当天真实日报**，不能写死。
    # 优先取低置信度板块里 AI 交代"自己信息不够"的原句；
    # 当天没有低置信度板块时，退而取中等置信度板块的真实限定句。
    raw_secs = dash.get("sections", []) or []
    quotes = _pick_hedge_quotes(raw_secs, limit=3)
    quote_sections = [s for s in raw_secs
                      if s.get("confidence_level") in ("low", "medium")]
    if not quotes:
        quotes = _pick_hedge_quotes(quote_sections, limit=3)
    if not quotes:
        # 当天日报里找不到"自我限定"原句 —— 不编造，直接不生成这篇
        return []

    quote_block = "\n".join(f"「{q}」" for q in quotes)
    title1 = f"我让AI写日报，它主动说“这个我不确定”"

    # 「低置信度 N 个」这个说法只有在当天真有低置信度板块时才成立。
    # 没有低置信度板块时，改说中等置信度，避免出现"0 个低置信度却列出低置信度引文"的矛盾。
    if low_sections:
        dist_line = (f"· 低置信度 {len(low_sections)} 个\n"
                     f"· 中等 {len(med_sections)} 个\n"
                     f"· 高置信度 {len(high_sections)} 个")
        quote_intro = "低置信度的那几段，结尾长这样："
    else:
        dist_line = (f"· 中等置信度 {len(med_sections)} 个\n"
                     f"· 高置信度 {len(high_sections)} 个")
        quote_intro = "标成中等的那段，结尾长这样："

    body1 = f"""先说个我一开始没料到的现象。

我给自己的投研流程接了个AI日报模块，跑起来之后，它输出的第一句话经常是
“这个我不确定”，而不是结论。

{date} 那份日报，一共 {len(secs)} 个板块：
{dist_line}

{quote_intro}

{quote_block}

我第一反应是：这也敢输出？直接删掉不就完了。

后来我想明白了 —— 这恰恰是我最需要它的原因。

我做复盘最怕的不是"AI 说错了"，是"AI 说错了但我不知道它错"。
一个每次都自信满满给你结论的工具，你根本不会去验证它。
但一个会说"这块样本不够，我不下结论"的工具，你反而会信任它认真说话的部分。

所以我现在的规则很简单：{brand_line()}，但我只看两处 ——
一是它下了什么结论，二是它标了多高的置信度。
这两处对不上的地方，就是我当天必须自己去查的地方。

顺便说下它是怎么算置信度的，逻辑不复杂：
一段结论如果背后只有很少的原始材料支撑，它就标低；
材料够、还有研报交叉验证，它才标高。
这不是模型在"谦虚"，是它在数自己手里有几张牌。

这套机制我用了几个月，最大的变化不是准确率，
是我终于知道自己哪天的判断是"有底"的、哪天是"虚"的。

你用AI做研究时，会看它给的不确定性标注吗？
还是只看它说了什么、没说什么？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    notes.append({
        "id": f"AX-{date}-conf",
        "title": title1,
        "body": body1,
        "tags": TAGS_SAFE[:6],
        "source": "dashboard.daily.confidence",
        "source_date": date,
        "confidence_note": f"本篇基于 {date} 日报的真实置信度分布生成",
        # 封面与正文共用同一组真实数字，避免图文不一致
        "conf_stats": {
            "low": len(low_sections),
            "medium": len(med_sections),
            "high": len(high_sections),
        },
    })

    # --- 笔记 2：样本量与口径 ---
    if low_sections:
        low_title = low_sections[0][0]
        # 引文取自当天该板块的真实原句
        low_raw = next((s for s in raw_secs
                        if s.get("section_title") == low_title), None)
        low_quotes = _pick_hedge_quotes([low_raw] if low_raw else [], limit=1)
        if not low_quotes:
            return notes
        sample_quote = low_quotes[0]
        body2 = f"""说个我自己踩过的坑。

以前我用AI整理每日信息，它的输出我从来不看口径，只看结论。
结果有段时间我每天照着它的结论复盘，复盘了一个月才发现，
有些结论它自己都标注了"样本量不足"，只是我没看。

{date} 那份日报里，「{low_title}」这一段的自我评价是：

「{sample_quote}」

样本不够，它还是把这段写出来了，但明确说了"不下结论"。

我后来加了一条硬规则：任何一段结论，我看不到它的样本量和时间窗口，就不采信。

这一条加进去之后，变化很直接 ——
过去我一天要读十几段结论，现在真正动手的只有三四段，
剩下的要么样本不够、要么时间窗口不覆盖我要回答的问题。

这听起来像是把效率做低了，其实是反过来的：
我原来花的时间，有一多半是在验证那些根本不该采信的结论。

工具给的信息越全，人越容易漏掉口径这件事。
所以我现在的做法是反过来 ——
先看它标的口径，再决定要不要看它的结论。

你现在用的AI工具，会主动标样本量吗？
还是你得自己去翻原始数据？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""
        notes.append({
            "id": f"AX-{date}-sample",
            "title": "看不到样本量的结论，我一律不采信",
            "body": body2,
            "tags": TAGS_SAFE[:5] + ["交易纪律"],
            "source": "dashboard.daily.sample",
            "source_date": date,
        })

    # --- 笔记 3：数据窗口 ---
    if window and window[0]:
        body3 = f"""有个细节，我一开始完全没注意。

{date} 那份日报，它自己标了数据窗口：
{window[0]} 到 {window[1]}。

注意，不是"今天"，不是"最近"。

这个差别有多大？举个我这周遇到的实际情况 ——
我周日晚上看的是当天生成的那份日报，
但它覆盖的窗口是前一天的傍晚到当天傍晚，
而我脑子里的"今天"是自然日。

时间窗口错位一点点，结论就能差很远。
比如周日的日报里如果提到"本周资金流入某方向"，
我周一早上照着看的时候，那个"本周"其实已经过了两天了。

所以我现在看任何AI生成的东西，会先找两样东西：
一是数据窗口到哪了，二是样本来自哪。

这两样东西在正文里通常不会明写，得自己去找。
找到之后我发现，很多看着很新的结论其实已经过期了。

工具给你答案是一件事，给你答案的时效边界是另一件事。
后者往往更影响你用它做决策。

你平时看AI生成的分析，会在意它用的是哪段时间的数据吗？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""
        notes.append({
            "id": f"AX-{date}-window",
            "title": "AI给的答案，有多新是另一回事",
            "body": body3,
            "tags": TAGS_SAFE[:6],
            "source": "dashboard.daily.window",
            "source_date": date,
        })

    return notes


def gen_from_intelligence(items: list, dash: dict | None = None) -> list[dict]:
    """从情报精选生成笔记 —— 只讲"信息怎么分类"，不讲内容本身。"""
    if not items:
        return []

    cats = {}
    for it in items:
        s = sanitize_intelligence(it)
        l1 = s.get("category_l1") or "其他"
        l2 = s.get("category_l2") or ""
        cats.setdefault(l1, []).append(l2)

    n = len(items)
    date = items[0].get("postDate", "")
    cat_desc = "、".join(f"{k}（{len(v)} 条）" for k, v in sorted(cats.items()))

    body = f"""我订阅了好几个信息源，最早的做法是全量看完再复盘。

后来发现根本看不完 —— 每天几百条，看完黄花菜都凉了。

现在我的做法是：让工具先做分类和摘要，我只看被筛出来的部分。

这是我今天拉到的数据：这一批一共 {n} 条，工具给我的分类是：

{cat_desc}

关键不是它分得多准，是它分完之后，我才发现自己每天真正会看的
只有两类 ——
一类是行业层面的变化，一类是个别公司的基本面。
剩下的（大部分是新闻、快讯、重复观点）我是不会看的。

以前我凭感觉筛，现在有了一个客观的分类依据，
筛选这件事就不消耗意志力了。

我更看重的是另一个变化：
我开始用"这批信息里有没有出现我没预期到的分类"来判断
—— 当某一个分类突然冒出来很多条，那说明最近这个方向上事情变多了。
这是个纯粹的统计信号，跟内容本身说的对错没关系。

信息太多的时候，真正稀缺的不是信息，是"结构"。
把结构给你，你才有判断力；只给你更多条目，你只是更焦虑。

你每天的信息量有多少？是怎么筛的？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return [{
        "id": f"AX-{date}-intel",
        "title": "信息太多时，稀缺的不是信息，是结构",
        "body": body,
        "tags": TAGS_SAFE[:6],
        "source": "intelligence.categories",
        "source_date": date,
    }]


def gen_from_research(items: list, dash: dict | None = None,
                      total: int | None = None) -> list[dict]:
    """从机构研报生成笔记 —— 只讲"研报怎么读"，不讲研报结论。"""
    if not items:
        return []

    cats = {}
    for it in items:
        s = sanitize_research(it)
        c = s.get("category") or "其他"
        cats.setdefault(c, []).append(s.get("topic_hint") or "")

    date = items[0].get("createdAt", "")[:10]
    # 总量来自接口 meta.total（真实值），不写死
    total_txt = f"{total}" if total else "上千条"

    body = f"""我收集研报的方式换过一次，思路挺有意思的。

以前是分类存 —— 宏观的存一个文件夹，行业的存一个，公司调研再一个。
结果每个文件夹都是几千条，往回翻的时候极其痛苦。

现在我改成按研究类型分类，不按行业分。

工具帮我把最近这一批（{len(items)} 条，总量约 {total_txt} 条）归成了这几类：

{chr(10).join(f'· {k}：{len(v)} 条' for k, v in sorted(cats.items()))}

你会发现一件事 —— 同样是宏观的内容，有的是政策解读，有的是利率路径推演，
有的是流动性跟踪。它们对你的用法完全不同：
政策解读是背景信息，利率路径才直接关系到你的判断。

按行业分，你看到的是"这周计算机有什么消息"；
按研究类型分，你看到的是"这周有多少人在讨论利率路径"。

后者的信息量要大得多。

我现在的习惯是：先按类型筛，再看内容。
而且我更关心一件事 —— 某个类型的数量突然变多的时候，
说明最近大家的注意力往哪偏了。

这个信号比任何单篇研报的结论都更早出现，也更客观。

你存研报是按行业分还是按类型分？
说实话我以前也没想清楚，改过来之后才发现差别这么大。

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return [{
        "id": f"AX-{date}-research",
        "title": "我存研报改了分类方式，效率差了一个量级",
        "body": body,
        "tags": TAGS_SEARCH[:6],
        "source": "research.categories",
        "source_date": date,
    }]


def gen_from_agent(question: str, answer: dict) -> list[dict]:
    """从智能体问答生成笔记 —— 只讲"怎么问"，不讲答案里的个股判断。"""
    body = f"""我以前问AI研究类问题，是这么问的：

「{question}」

问完就等一个长答案。

现在我换了个问法，每次都拆成三段问：
① 这件事的分部是什么结构？
② 每个分部里，最容易被忽略的约束是什么？
③ 要验证我自己的想法，需要看哪几个数？

第三个问题最关键 ——
它逼着工具给出"该看什么"，而不是"答案是什么"。

区别在哪？
前者会给我一段看起来很专业的话，
后者会告诉我该去翻哪个数、哪个口径、哪个时间窗。

我把这套问法固化到流程里了，每天跑一遍，
它就变成一张"今天该看什么"的清单。

这比它直接给我结论有用得多，
因为结论会过期，而"该看什么"这个能力不会。

工具的价值不在于替你回答，
在于它能不能把问题拆到你真正能动手的粒度。

你问AI问题的时候，是要答案还是要方法？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return [{
        "id": "AX-agent-howto",
        "title": "我改了个问AI的方式，答案质量差很多",
        "body": body,
        "tags": ["AI工具", "AI工作流", "效率工具", "交易复盘", "投资复盘", "工作流"],
        "source": "agent.methodology",
        "source_date": "",
    }]


def build_all() -> list[dict]:
    """拉取全部数据并生成笔记。"""
    c = AxiomClient()
    if not c.ensure_login():
        raise SystemExit("登录失败")

    dash = c.daily_dashboard()
    intel = c.intelligence()
    intel_rows = intel if isinstance(intel, list) else (intel.get("items") or [])
    research = c.research_analysis()
    research_rows = research if isinstance(research, list) else (research.get("items") or [])

    # 真实总量（来自接口 meta.total）—— 不写死，避免图文数字对不上
    research_total = None
    try:
        meta = c._request(
            "GET", "/v1/research-analysis?page=1&pageSize=1"
                   "&sortBy=createdAt&sortOrder=DESC").get("meta") or {}
        research_total = meta.get("total")
    except Exception:
        research_total = None

    notes = []
    notes += gen_from_dashboard(dash)
    notes += gen_from_intelligence(intel_rows, dash)
    notes += gen_from_research(research_rows, dash, total=research_total)
    notes += gen_from_agent("今天有什么新闻值得关注的", {})

    # 日报累计期数（真实统计）—— 供封面提要使用
    daily_count = _daily_report_count(c)
    for n in notes:
        n["daily_count"] = daily_count

    # 统一过一遍禁词检查
    cleaned = []
    for n in notes:
        issues = forbidden_check(n["title"]) + forbidden_check(n["body"])
        if issues:
            print(f"⚠️  跳过 {n['id']}：{issues}")
            continue
        cleaned.append(n)

    return cleaned


# 每篇笔记对应的系统截图场景（产品演示图）
SHOT_FOR_NOTE = {
    "conf": "dashboard",       # 置信度 → 仪表盘结构
    "sample": "dashboard",     # 样本量 → 仪表盘结构
    "window": "dashboard",     # 数据窗口 → 仪表盘
    "intel": "intel_list",     # 信息分类 → 情报精选列表
    "research": "intel_detail",  # 研报方法 → 情报精选详情
    "howto": "agent",          # 提问法 → 智能体
}


def attach_screenshot(note: dict, shots_dir: Path) -> str:
    """
    给笔记挂上系统截图。

    截图来源：source/screenshot.py 生成的**已脱敏**原始截图，
    再由 source/compose_shot.py 加工成 3:4 笔记配图。

    这里只做"挂载"——如果加工好的配图不存在就跳过，
    绝不影响文字内容的发布（配图是加分项，不是必需项）。
    """
    nid = note["id"]
    scene = None
    for key, sc in SHOT_FOR_NOTE.items():
        if nid.endswith(key):
            scene = sc
            break
    if not scene:
        return ""

    composed = shots_dir / f"shot_{scene}.jpg"
    if not composed.exists():
        return ""

    # 注意：不要覆盖 note["images"] —— 封面已经占第一位了。
    # 这里追加到末尾（截图作为第二张），并去重。
    imgs = list(note.get("images") or [])
    if str(composed) not in imgs:
        imgs.append(str(composed))
    note["images"] = imgs
    note["screenshot_scene"] = scene
    return str(composed)


if __name__ == "__main__":
    notes = build_all()
    OUT.mkdir(parents=True, exist_ok=True)
    for n in notes:
        p = OUT / f"{n['id']}.json"
        p.write_text(json.dumps(n, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ {n['id']}  {n['title']}  ({len(n['body'])}字, {len(n['tags'])}标签)")
    print(f"\n共 {len(notes)} 篇 -> {OUT}")
