"""
抖音内容改写器 —— **数据驱动版**。

【为什么必须重写】
初版把数字硬编码在模板里（"低置信度 2 个 / 中等 1 个 / 高置信度 1 个"、
三句当日并不存在的引文、固定的情报与研报分类）。
实测 2026-11-03 日报真实置信度分布是 high 2 / medium 2（没有 low），
那三句引文在 AXIOM 返回里**根本不存在**。

这直接违反铁律：**所有数字必须来自 AXIOM 真实数据**。
所以本文件的每个数字都必须在运行时从 API 算出，一个都不许写死。

【小红书 vs 抖音的差异（保留）】
小红书是搜索型（关键词前置 / 引导收藏）；
抖音是推荐型（前 3 秒决定是否划走 → 「问题+损失」标题 / 结论前置 / 引导评论互动）。

【抖音硬规则】
1. 标题 ≤30 字（实测上限），建议 ≤20 字且带具体数字
2. 标题用「问题 + 损失」，不用「我做了什么」
3. 开头前 2 行结论前置（不能铺垫）
4. 结尾引导评论区提问，**不引导站外、不引导收藏**（评论才是强互动）
5. 每条勾「含有AI生成内容」标识
6. 不出现"分享我今天赚了…"这类收益叙事
7. 不出现"第X天/每日更新"这类连载模板

【数据纪律（2026-11-04 强化）】
· 置信度分布、板块数、日期、数据窗口 → 全部运行时计算
· 引文 → 从当日真实 key_points / section_content 中筛选，且必须：
    ① 表达"自我限定 / 待验证"语义
    ② 不含行情方向判断
    ③ 不含任何公司名（公司名黑名单从 AXIOM 自身动态取）
    ④ 过 forbidden_check
  找不到就**不生成这篇**，绝不编造
· 公司名黑名单 = AXIOM stockMapping / mentionedStocks 的真实名单 + 已知名单
· 正文里出现的每一个数字，都要在 _verify_numbers_real() 里通过白名单校验，
  白名单之外的一律视为「编造」并中止生成。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.douyin_compliance import check_douyin, check_title_style  # noqa: E402
from source.sanitize import (  # noqa: E402
    KNOWN_COMPANIES, forbidden_check, sanitize_text,
)

ROOT = Path(__file__).parent.parent
OUT = ROOT / "content" / "for_douyin"

# 抖音话题标签：3-5 个，不带金融荐股语义
TAGS_DY = {
    "conf": ["AI工具", "效率工具", "交易复盘", "自动化", "投资复盘"],
    "sample": ["交易复盘", "AI工具", "工作流", "效率工具"],
    "window": ["AI工具", "量化学习", "投资复盘", "效率工具"],
    "intel": ["信息整理", "效率工具", "AI工具", "工作流"],
    "research": ["研究方法", "AI工具", "效率工具", "投资复盘"],
    "howto": ["AI工作流", "AI工具", "效率工具", "交易复盘"],
}

# ---------------------------------------------------------------------------
# 引文筛选器
# ---------------------------------------------------------------------------

# 「自我限定 / 待验证」信号 —— 命中即说明这句话在讲**自己还没把握**
SELF_LIMIT_MARKERS = (
    "仍需", "仍待", "仍以", "仍由", "仍依赖", "尚未", "不足以", "无法",
    "未覆盖", "不做", "不跨", "暂不", "受限", "核验", "待验证",
    "需继续", "需以", "样本", "覆盖",
)

# 行情方向词 —— 命中即视为"在讲行情/给方向"，不能当"自我限定"的证据
# （防止把「政策托底不等于趋势反转」这类方向判断当成诚实证据）
MARKET_MARKERS = (
    "反弹", "上行", "下行", "筑底", "见底", "见顶", "底部", "顶部",
    "托底", "去杠杆", "波动放大", "补跌", "增量", "减仓", "加仓",
    "布局", "主线", "弹性", "催化", "兑现", "业绩", "扩产", "放量",
    "定价", "基本面", "景气", "赛道", "拥挤", "超配", "低配",
    "涨价", "降价", "量价", "估值", "反转", "修复", "配置", "推荐",
    "买入", "卖出", "增持", "减持", "看好", "低估", "高估", "机会",
    "牛市", "熊市", "转强", "转弱", "优先", "空间",
)


def _collect_company_names(intel_rows: list, research_rows: list,
                           dash: dict | None = None) -> set:
    """
    从 AXIOM 自身取出当日真实出现的公司名，作为引文过滤黑名单。

    为什么不能只靠正则：AXIOM 的公司名大量不带标准后缀
    （如「奥普特」「今世缘」「冰山冷热」），正则抓不到，
    但一旦写进正文就是「个股名称」违规。
    → 所以直接用数据源给的名单，比猜正则可靠。

    三个来源合并：
      1. AXIOM 的权威映射（stockMapping.mentioned_stocks / mentionedStocks）
         —— 这是最准的，带 validated 标记
      2. 日报正文里的并列名单模式（A、B和C分别…）—— 覆盖只出现在日报里的公司
      3. 支撑来源标题（【XX公司】、XX公司：…）—— 覆盖研报标题里的公司
    """
    names: set = set(KNOWN_COMPANIES)

    def add(n):
        n = str(n).strip()
        if len(n) >= 2:
            names.add(n)

    # 1) 权威映射
    for r in intel_rows or []:
        for s in (r.get("stockMapping") or {}).get("mentioned_stocks") or []:
            add(s.get("name") if isinstance(s, dict) else s)
    for r in research_rows or []:
        for s in r.get("mentionedStocks") or []:
            add(s.get("name") if isinstance(s, dict) else s)

    # 2) 日报正文并列名单
    for sec in (dash or {}).get("sections") or []:
        text = " ".join(list(sec.get("key_points") or [])
                        + [sec.get("section_content") or ""])
        for m in re.finditer(r"([一-龥]{2,6}(?:、[一-龥]{2,6})*(?:和|与)[一-龥]{2,6})的", text):
            for part in re.split(r"[、和与]", m.group(1)):
                add(part)
        for m in re.finditer(
                r"([一-龥]{2,6})的[一-龥]{0,8}(?:弹性|兑现|供给|订单|放量|布局|主线|溢价|扩产|重组|驱动)",
                text):
            add(m.group(1))
        # 3) 支撑来源标题
        for t in sec.get("supporting_source_titles") or []:
            title = t.get("title", "") if isinstance(t, dict) else str(t)
            for m in re.findall(r"【([^】]{2,12})】", title):
                add(m)
            for m in re.findall(r"([一-龥]{2,6})(?:：|:|｜)", title):
                add(m)

    # 全称去掉公司后缀，让匹配更宽松（"XX股份有限公司" → "XX"）
    out = set()
    for n in names:
        out.add(n)
        for suf in ("股份有限公司", "有限责任公司", "集团股份", "股份", "集团",
                    "科技", "生物", "医药"):
            if n.endswith(suf) and len(n) > len(suf) + 1:
                out.add(n[: -len(suf)])
    return {n for n in out if len(n) >= 2}


def _pick_self_limit_quotes(sections: list, company_names: set,
                            limit: int = 3) -> list[str]:
    """
    从当日真实日报里挑出「AI 主动交代自己还没把握」的句子。

    规则（缺一不可）：
      1. 命中 SELF_LIMIT_MARKERS —— 确实在讲还没把握/待验证
      2. 不命中 MARKET_MARKERS —— 不是在讲行情方向
      3. 不含任何公司名（含 AXIOM 当日动态名单）
      4. 过 forbidden_check —— 没有代码、导流词
      5. 长度 8~60 —— 是一句话，能当引文念出来
    """
    out: list[str] = []
    for sec in sections or []:
        pieces = list(sec.get("key_points") or []) + [sec.get("section_content") or ""]
        for piece in pieces:
            for sent in piece.replace("；", "。").replace("，", "。").split("。"):
                s = sent.strip()
                if not (8 <= len(s) <= 60):
                    continue
                if not any(m in s for m in SELF_LIMIT_MARKERS):
                    continue
                if any(m in s for m in MARKET_MARKERS):
                    continue
                if any(name in s for name in company_names):
                    continue
                if forbidden_check(s):
                    continue
                t = sanitize_text(s)
                if not t or t in out:
                    continue
                out.append(t)
                if len(out) >= limit:
                    return out
    return out


def _section_stats(sec: dict) -> dict:
    """单个板块的真实支撑数据（全部来自 API 字段，不推算）。"""
    return {
        "title": sec.get("section_title", ""),
        "conf": sec.get("confidence_level", ""),
        "posts": len(sec.get("source_post_ids") or []),
        "research": len(sec.get("source_research_ids") or []),
        "titles": len(sec.get("supporting_source_titles") or []),
    }


def _conf_label(level: str) -> str:
    return {"low": "低置信度", "medium": "中等置信度", "high": "高置信度"}.get(
        level, level or "未知置信度")


# ---------------------------------------------------------------------------
# 统计口径（**模板与数字白名单共用**）
# ---------------------------------------------------------------------------
# 为什么必须共用同一份实现：
#   模板里会出现**派生数字**（不是 API 里的原始字段），
#   例如情报篇的「我关注的 2 类里有 1 条，剩下 9 条我不看」
#   —— 9 = 总条数 10 - 命中条数 1，是真实算出来的。
#   如果白名单只认原始字段值，就会把这个**真实数字**误判成「编造」，
#   整篇被跳过（2026-11-09 实测：DY-intel 因无据数字 '9' 被拦）。
#   所以模板与白名单都调用下面这几个函数，口径永远一致：
#   模板算出什么数，白名单就一定认什么数；
#   反过来，白名单不认的数（真·编造）照样拦得住。

def _dash_stats(dash: dict) -> dict:
    """日报板块统计：板块数、置信度分布、零交叉验证板块数。"""
    stats = [_section_stats(s) for s in (dash.get("sections") or [])]
    levels: dict = {}
    for s in stats:
        levels[s["conf"]] = levels.get(s["conf"], 0) + 1
    zero_cross = [s for s in stats if s["research"] == 0]
    return {
        "stats": stats,
        "n": len(stats),
        "levels": levels,
        "zero_cross": zero_cross,
        "zero_cross_n": len(zero_cross),
    }


def _intel_stats(items: list) -> dict:
    """情报分类统计：分类计数、我关注的两类、剩余条数。"""
    cats: dict = {}
    for it in items or []:
        k = it.get("categoryL1") or "其他"
        cats[k] = cats.get(k, 0) + 1
    focus = {k: v for k, v in cats.items() if k in ("INDUSTRY", "COMPANY")}
    focus_n = sum(focus.values())
    n = len(items or [])
    return {
        "n": n,
        "cats": cats,
        "focus_k": len(focus),
        "focus_n": focus_n,
        "rest_n": n - focus_n,
    }


def _research_stats(items: list) -> dict:
    """研报分类统计。"""
    cats: dict = {}
    for it in items or []:
        k = it.get("categoryL1") or "其他"
        cats[k] = cats.get(k, 0) + 1
    return {"n": len(items or []), "cats": cats}


# ---------------------------------------------------------------------------
# 各篇改写
# ---------------------------------------------------------------------------

def rewrite_conf(dash: dict, company_names: set) -> dict | None:
    """置信度机制 → 抖音版。所有数字与引文均来自当日真实日报。"""
    secs = dash.get("sections") or []
    if not secs:
        return None

    date = dash.get("reportDate") or ""
    stats = [_section_stats(s) for s in secs]
    n_sections = len(stats)

    counts = {"low": 0, "medium": 0, "high": 0}
    for s in stats:
        if s["conf"] in counts:
            counts[s["conf"]] += 1
    zero_cross_n = len([s for s in stats if s["research"] == 0])

    # 分布行：只写当天真实存在的置信度档位（没有 low 就不写 low）
    dist_lines = []
    for level in ("high", "medium", "low"):
        if counts[level]:
            dist_lines.append(f"· {_conf_label(level)} {counts[level]} 个")
    if not dist_lines:
        return None

    # 研报交叉验证数量（真实字段 source_research_ids 的长度）
    detail_lines = []
    for s in stats:
        detail_lines.append(
            f"· {s['title']}：{s['research']} 份研报做交叉验证，{s['posts']} 条原始信息")

    # 引文优先取自置信度不高（low/medium）的板块 —— 那里才是"承认没把握"的地方
    low_or_med = [s for s in secs if s.get("confidence_level") in ("low", "medium")]
    quotes = _pick_self_limit_quotes(low_or_med, company_names, limit=3)
    if not quotes:
        quotes = _pick_self_limit_quotes(secs, company_names, limit=3)
    if not quotes:
        # 当天日报里找不到真实的"自我限定"原句 → 不编造，直接不生成这篇
        return None

    quote_block = "\n".join(f"「{q}」" for q in quotes)

    # 研报交叉验证为 0 的板块数量（真实统计）
    zero_cross = [s for s in stats if s["research"] == 0]
    cross_note = (
        f"今天有 {zero_cross_n} 个板块，一份研报都没有。" if zero_cross else "")

    body = f"""你有没有遇到过这种情况：AI给你一份分析，看着特别专业，你信了。

最难受的不是它说错了，是它说错了你不知道它错。
一个每次都自信满满给你结论的工具，你根本不会去验证它。

我自己的做法是让它把置信度标出来，然后我只信标了高的那部分。

{date} 那份日报，一共 {n_sections} 个板块：
{chr(10).join(dist_lines)}

而且这个标记不是随口给的，它跟你能查到几份研报是对得上的：

{chr(10).join(detail_lines)}

{cross_note}

还有几处它自己标了"还没到能下结论"：

{quote_block}

我第一反应是：这也敢输出？删掉不就完了。

后来想明白了，这恰恰是我最需要它的原因。
一个会说"这块我还没把握"的工具，你反而会信任它认真说话的部分。

现在我的规则很简单：结论和置信度对不上的地方，就是当天必须自己查的地方。

顺便说它怎么算置信度的，逻辑不复杂：
背后只有很少原始材料支撑就标低，有交叉验证才标高。
这不是模型在谦虚，是它在数自己手里有几张牌。

最大的变化不是准确率，是我终于知道哪天的判断是有底的、哪天是虚的。

你现在用的AI工具，会标不确定度吗？
还是只看它说了什么？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return {
        "id": "DY-conf",
        "title": f"{n_sections}个板块AI标了{counts['high']}个高置信，剩{counts['medium']}个自己查",
        "body": body,
        "tags": TAGS_DY["conf"],
        "source": "dashboard.daily.confidence",
        "source_date": date,
    }


def rewrite_sample(dash: dict, company_names: set) -> dict | None:
    """样本量 / 口径 → 抖音版。用每个板块的真实原始材料数量。"""
    secs = dash.get("sections") or []
    if not secs:
        return None
    date = dash.get("reportDate") or ""

    stats = [_section_stats(s) for s in secs]
    material_lines = []
    for s in stats:
        material_lines.append(
            f"· {s['title']}：{s['posts']} 条原始信息、{s['research']} 份研报、{s['titles']} 条来源标题")

    with_cross = [s for s in stats if s["research"] > 0]
    without_cross = [s for s in stats if s["research"] == 0]
    without_cross_n = len(without_cross)
    if not without_cross:
        # 今天每个板块都有交叉验证，这条就换成"支撑条数差在哪"
        thin = min(stats, key=lambda s: s["titles"])
        thin_note = (f"支撑最少的是「{thin['title']}」，"
                     f"它底下是 {thin['titles']} 条来源标题、{thin['research']} 份研报。")
    else:
        thin_note = (f"其中 {without_cross_n} 个板块一份研报都没有，"
                     f"底下的原始信息只有 "
                     f"{'、'.join(str(s['posts']) for s in without_cross)} 条。")

    body = f"""你有没有遇到过这种情况：AI给你一堆结论，你照着复盘了一个月才发现，有些它自己都标了口径不够，只是你没看。

最难受的不是结论错，是你根本没去看口径。

我自己的做法是加一条硬规则：任何一段结论，我看不到它的样本量和时间窗口，就不采信。

{date} 那份日报，每个板块后面都挂着它的来源数量：

{chr(10).join(material_lines)}

{thin_note}

差得很明显。哪个板块底下的材料只有个位数，我一眼就能看出来。
这种结论我最多当线索看，不当依据。

这一条加进去之后，变化很直接——
过去我一天要读十几段结论，现在真正会动手细看的是有交叉验证的那几个板块，
剩下的要么材料不够，要么时间窗口不覆盖我要回答的问题。

工具给的信息越全，人越容易漏掉口径这件事。
所以我现在的做法是反过来——
先看它标的口径，再决定要不要看它的结论。

你现在用的AI工具，会主动标样本量吗？
还是得自己翻原始数据？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return {
        "id": "DY-sample",
        "title": f"{len(stats)}个板块，材料只有个位数的我不看结论",
        "body": body,
        "tags": TAGS_DY["sample"],
        "source": "dashboard.daily.sample",
        "source_date": date,
    }


def rewrite_window(dash: dict) -> dict | None:
    """数据窗口 → 抖音版。窗口起止全部来自真实字段。"""
    secs = dash.get("sections") or []
    if not secs:
        return None
    date = dash.get("reportDate") or ""
    w0 = (dash.get("dataWindowStart") or "")[:10]
    w1 = (dash.get("dataWindowEnd") or "")[:10]
    if not w0 or not w1:
        return None

    # 窗口长度（小时）—— 由真实起止时间算出
    try:
        from datetime import datetime
        t0 = datetime.fromisoformat(dash["dataWindowStart"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(dash["dataWindowEnd"].replace("Z", "+00:00"))
        hours = round((t1 - t0).total_seconds() / 3600)
    except Exception:
        hours = None

    span_txt = f"{hours}小时" if hours else "不到一天"

    body = f"""你有没有遇到过这种情况：AI给你的分析看着挺新，其实数据是前一天晚上的。

最难受的不是内容有问题，是你按它做了决定。

我自己的做法是看任何AI生成的东西时，先找两样东西：
一是数据窗口到哪了，二是样本来自哪。

这两样在正文里通常不会明写，得自己去找。

举个我这几天遇到的实际情况——
我看的是 {date} 那份日报，它自己标的窗口是 {w0} 到 {w1}，
跨度 {span_txt}，起点是傍晚，不是早上。

而我脑子里的"今天"是自然日。

时间窗口错位一点点，结论就能差很远。
比如这份日报里如果提到某个"本周"口径的统计，
我第二天早上照着看的时候，那个"本周"其实已经过去一天多了。

找到之后我发现，很多看着很新的结论其实已经过期了。

工具给你答案是一件事，给你答案的时效边界是另一件事。
后者往往更影响你用它做决策。

你平时看AI生成的分析，会在意它用的是哪段时间的数据吗？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return {
        "id": "DY-window",
        "title": f"AI答案对不对，先看它取的哪{span_txt}",
        "body": body,
        "tags": TAGS_DY["window"],
        "source": "dashboard.daily.window",
        "source_date": date,
    }


# 我关注的两类在库里的中文名。顺序固定，与正文表述一致。
FOCUS_CAT_NAMES = {
    "INDUSTRY": "行业层面的变化",
    "COMPANY": "个别公司的基本面",
}


def rewrite_intel(items: list, total: int | None) -> dict | None:
    """情报精选 → 抖音版。分类与条数全部用真实 categoryL1 计数。"""
    if not items:
        return None
    st = _intel_stats(items)
    n = st["n"]
    cats = st["cats"]
    focus_n = st["focus_n"]
    focus_k = st["focus_k"]
    cat_lines = "\n".join(f"· {k} {v} 条" for k, v in sorted(cats.items(),
                                                           key=lambda x: (-x[1], x[0])))

    # 2026-11-10 实测踩坑：原模板把「关注类」写死成两类并列
    # （"一类是行业层面的变化，一类是个别公司的基本面"），
    # 但 focus_k 是**本批实际命中的关注类个数**。
    # 当本批只命中 1 类时，会写出
    # 「我真正会看的只有 1 类——一类是…，一类是…」这种自相矛盾的话
    # （今天已发布的 16287 条那篇就是这个问题）。
    # → 改为按**实际命中的关注类**生成描述；一个都没命中则本篇不成立。
    focus_cats = [k for k in ("INDUSTRY", "COMPANY") if k in cats]
    if not focus_cats:
        return None
    if len(focus_cats) == 1:
        focus_desc = f"只有 {focus_k} 类——{FOCUS_CAT_NAMES[focus_cats[0]]}。"
        this_cats = "这一类"
    else:
        names = "，".join(f"一类是{FOCUS_CAT_NAMES[k]}" for k in focus_cats)
        focus_desc = f"只有 {focus_k} 类——{names}。"
        this_cats = "这两类"

    total_txt = f"，整个库 {total} 条" if total else ""

    body = f"""你有没有遇到过这种情况：信息太多，看完黄花菜都凉了。

最难受的不是信息少，是看了两天发现全是重复的。

我自己的做法是让工具先做分类和摘要，我只看被筛出来的那部分。

我拉的这一批一共 {n} 条{total_txt}，工具给的分类是：

{cat_lines}

关键不是它分得多准，是它分完之后我才发现，我真正会看的{focus_desc}
这批里落在{this_cats}上的只有 {focus_n} 条，剩下 {n - focus_n} 条我不会看。

以前我凭感觉筛，现在有了一个客观的分类依据，
筛选这件事就不消耗意志力了。

我更看重的是另一个变化：
我开始用"这批信息里有没有出现我没预期到的分类"来判断——
当某一个分类突然冒出来很多条，说明最近这个方向上事情变多了。
这是个纯粹的统计信号，跟内容本身说的对错没关系。

信息太多的时候，真正稀缺的不是信息，是结构。
把结构给你，你才有判断力；只给你更多条目，你只是更焦虑。

你每天的信息量有多少？是怎么筛的？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    total_prefix = f"{total}" if total else str(n)
    return {
        "id": "DY-intel",
        "title": f"{total_prefix}条信息，我只看其中{focus_k}类",
        "body": body,
        "tags": TAGS_DY["intel"],
        "source": "intelligence.categories",
        "source_date": "",
    }


def rewrite_research(items: list, total: int | None) -> dict | None:
    """机构研报 → 抖音版。只讲分类方法，不转述任何结论。"""
    if not items:
        return None
    n = len(items)
    cats = _research_stats(items)["cats"]
    cat_lines = "\n".join(f"· {k} {v} 条" for k, v in sorted(cats.items(),
                                                           key=lambda x: (-x[1], x[0])))

    total_txt = f"，整个库 {total} 份" if total else ""

    body = f"""你有没有遇到过这种情况：研报存了一堆，想找的时候全找不到。

最难受的不是存得少，是存的时候没想清楚以后怎么找。

我以前的分类方式是按行业分——宏观一个文件夹，行业一个，公司调研一个。
结果每个文件夹都是几百条，往回翻的时候极其痛苦。

现在我改成按研究类型分，不按行业分。

工具帮我把最近这一批（{n} 份{total_txt}）归成了这几类：

{cat_lines}

你会发现一件事——同样是宏观的内容，
有的是政策解读，有的是利率路径推演，有的是流动性跟踪。
它们对你的用法完全不同：
政策解读是背景信息，利率路径才直接关系到你的判断。

按行业分，你看到的是"这周计算机有什么消息"；
按研究类型分，你看到的是"这周有多少人在讨论利率路径"。

后者的信息量要大得多。

我现在的习惯是：先按类型筛，再看内容。
而且更关心一件事——某个类型的数量突然变多的时候，
说明最近大家的注意力往哪偏了。

这个信号比任何单篇研报的结论都更早出现，也更客观。

你存资料是按行业分还是按类型分？
说实话我以前也没想清楚，改过来之后才发现差别这么大。

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    total_prefix = f"{total}" if total else str(n)
    return {
        "id": "DY-research",
        "title": f"{total_prefix}份研报，我按类型存不按行业存",
        "body": body,
        "tags": TAGS_DY["research"],
        "source": "research.categories",
        "source_date": "",
    }


def rewrite_agent() -> dict:
    """智能体提问法 → 抖音版。纯方法论，不含任何数据。"""
    body = """你有没有遇到过这种情况：问AI研究类问题，问完等一大段答案，看完还是不知道该干嘛。

最难受的不是答案没用，是它太长，长到你跳过了重点。

我以前是这么问的：「今天有什么新闻值得关注的」

现在我换了个问法，每次都拆成三段问：
① 这件事的分部是什么结构？
② 每个分部里，最容易被忽略的约束是什么？
③ 要验证我自己的想法，需要看哪几个数？

第三个问题最关键——
它逼着工具给出"该看什么"，而不是"答案是什么"。

区别在哪？
前者会给我一段看起来很专业的话，
后者会告诉我该去翻哪个数、哪个口径、哪个时间窗。

我把这套问法固化到流程里了，它就变成一张"今天该看什么"的清单。

这比它直接给我结论有用得多，
因为结论会过期，而"该看什么"这个能力不会。

工具的价值不在于替你回答，在于它能不能把问题拆到你真正能动手的粒度。

你问AI问题的时候，是要答案还是要方法？

以上只是我自己的记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。"""

    return {
        "id": "DY-howto",
        "title": "我把问AI拆成3问，答案终于能落地",
        "body": body,
        "tags": TAGS_DY["howto"],
        "source": "agent.methodology",
        "source_date": "",
    }


# ---------------------------------------------------------------------------
# 数字真实性校验
# ---------------------------------------------------------------------------

def _verify_numbers_real(note: dict, allowed: set) -> list[str]:
    """
    正文里出现的每一个数字都必须落在白名单内。

    白名单 = 本次运行从 AXIOM 算出的真实数值 + 少量方法论常量。
    任何白名单之外的数字 → 判定为"编造"，该篇直接不生成。

    这是对「所有数字必须来自 AXIOM 真实数据」这条铁律的机器化执行，
    防止以后又有人在模板里写死一个数字。
    """
    text = f"{note['title']}\n{note['body']}\n{' '.join(note['tags'])}"
    bad = []
    for m in re.findall(r"\d+(?:\.\d+)?", text):
        if m in allowed:
            continue
        # 日期拆出来的数字片段（2026 / 11 / 03）单独放行
        if m in {"2026", "2025", "01", "02", "03", "04", "05", "06",
                 "07", "08", "09", "10", "11", "12", "13", "14", "15",
                 "16", "17", "18", "19", "20", "21", "22", "23", "24",
                 "25", "26", "27", "28", "29", "30", "31"}:
            continue
        bad.append(m)
    return sorted(set(bad))


def _allowed_numbers(dash: dict, intel_rows: list, research_rows: list,
                     intel_total: int | None, research_total: int | None) -> set:
    """
    收集本次运行中所有"有据可查"的数字。

    ⚠️ 关键设计：**白名单与模板共用同一套统计函数**
    （_dash_stats / _intel_stats / _research_stats）。
    模板会写出派生数字（如情报篇「剩下 9 条」= 总条数 - 命中条数），
    这些数字是真实算出来的，却不在 API 的原始字段里。
    2026-11-09 实测踩坑：白名单只收原始值 → DY-intel 的 '9' 被判「编造」→ 整篇被跳过。
    共用口径后，模板能算出的数白名单一定认；
    白名单不认的数（真·编造/写死）仍然拦得住 —— 闸门只修口径，不放松。
    """
    a: set = set()

    # ---- 日报侧 ----
    d = _dash_stats(dash)
    a.add(str(d["n"]))                       # 板块数
    a.add(str(d["zero_cross_n"]))            # 零交叉验证板块数
    a.update(str(v) for v in d["levels"].values())   # 各置信度档位板块数
    for s in d["stats"]:
        a.update({str(s["posts"]), str(s["research"]), str(s["titles"])})
    # 计数为 0 是真实情况（如"0 个高置信"），必须允许
    a.add("0")

    # ---- 情报侧 ----
    i = _intel_stats(intel_rows)
    a.add(str(i["n"]))                       # 情报条数
    a.update(str(v) for v in i["cats"].values())     # 各分类条数
    a.add(str(i["focus_k"]))                 # 我关注的几类
    a.add(str(i["focus_n"]))                 # 关注类里的总条数
    a.add(str(i["rest_n"]))                  # 剩下的条数（= n - focus_n）★派生值

    # ---- 研报侧 ----
    r = _research_stats(research_rows)
    a.add(str(r["n"]))                       # 研报条数
    a.update(str(v) for v in r["cats"].values())     # 各分类条数

    # ---- 库总量 ----
    if intel_total:
        a.add(str(intel_total))
    if research_total:
        a.add(str(research_total))

    # 数据窗口跨度（小时）
    try:
        from datetime import datetime
        t0 = datetime.fromisoformat(dash["dataWindowStart"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(dash["dataWindowEnd"].replace("Z", "+00:00"))
        a.add(str(round((t1 - t0).total_seconds() / 3600)))
    except Exception:
        pass
    # 方法论常量：三段问法、关注类别数等
    a.update({"3", "2", "1"})
    return a


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_douyin_notes() -> list[dict]:
    """基于 AXIOM 真实数据生成抖音版内容，并过四层闸门。"""
    from source.axiom_api import AxiomClient

    c = AxiomClient()
    if not c.ensure_login():
        raise SystemExit("AXIOM 登录失败")

    dash = c.daily_dashboard()
    intel = c.intelligence()
    intel_rows = intel if isinstance(intel, list) else (intel.get("items") or [])
    research = c.research_analysis()
    research_rows = research if isinstance(research, list) else (research.get("items") or [])

    # 各集合的真实总量（来自接口 meta.total，不写死）
    intel_total = research_total = None
    try:
        meta = (c._request(
            "GET", "/v1/intelligence?page=1&pageSize=1"
                   "&sortBy=postDate&sortOrder=DESC").get("meta") or {})
        intel_total = meta.get("total")
    except Exception:
        pass
    try:
        meta = (c._request(
            "GET", "/v1/research-analysis?page=1&pageSize=1"
                   "&sortBy=createdAt&sortOrder=DESC").get("meta") or {})
        research_total = meta.get("total")
    except Exception:
        pass

    company_names = _collect_company_names(intel_rows, research_rows, dash)
    allowed = _allowed_numbers(dash, intel_rows, research_rows,
                               intel_total, research_total)

    notes = [
        rewrite_conf(dash, company_names),
        rewrite_sample(dash, company_names),
        rewrite_window(dash),
        rewrite_intel(intel_rows, intel_total),
        rewrite_research(research_rows, research_total),
        rewrite_agent(),
    ]
    notes = [n for n in notes if n]

    # 四层闸门：通用 → 抖音专属 → 数据层 → 项目层
    from content.compliance import check_note

    cleaned = []
    for n in notes:
        issues = []

        r1 = check_note({"title": n["title"], "body": n["body"],
                         "tags": n["tags"], "images": ["x.jpg"]})
        if not r1["passed"]:
            issues += [f"[通用] {v['category']}" for v in r1["violations"]]

        r2 = check_douyin({"title": n["title"], "body": n["body"],
                           "tags": n["tags"], "images": ["x.jpg"]})
        if not r2["passed"]:
            issues += [f"[抖音] {v['category']}" for v in r2["violations"]]

        issues += [f"[数据] {x}" for x in
                   forbidden_check(n["title"], company_names)
                   + forbidden_check(n["body"], company_names)]

        # 数字真实性闸门：正文里出现白名单外的数字 → 判定为编造
        bad_nums = _verify_numbers_real(n, allowed)
        if bad_nums:
            issues += [f"[数据] 正文出现无据数字: {bad_nums}"]

        if issues:
            print(f"⚠️  跳过 {n['id']}：{issues}")
            continue

        n["_warnings"] = r2["warnings"]
        n["_title_advice"] = check_title_style(n["title"])
        cleaned.append(n)

    return cleaned


if __name__ == "__main__":
    notes = build_douyin_notes()
    OUT.mkdir(parents=True, exist_ok=True)
    for n in notes:
        p = OUT / f"{n['id']}.json"
        p.write_text(__import__("json").dumps(n, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"✅ {n['id']}  {n['title']}  ({len(n['body'])}字, {len(n['tags'])}标签)")
        for w in n.get("_warnings", []):
            print(f"      ⚠️  {w['category']}")
        for a in n.get("_title_advice", []):
            print(f"      💡 {a}")
    print(f"\n共 {len(notes)} 篇 -> {OUT}")
