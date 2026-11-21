"""
雪球动态内容生成器（热点驱动）。

【与静态版 gen_xq.py 的区别 —— 这是本次改造的核心】

静态版：预先写好 5 篇固定方法论，按日轮换。
动态版：**先抓当日雪球热门话题 → 选有板块的话题 → 去 Axiom 情报精选
         找同板块最新一条 → 取其核心观点 → 组合成当日长文。**

【用户要求的流程】
1. 登录雪球 → 抓首页「热门话题」
2. 从话题里选**涉及板块**的（如今天有半导体、家电）
3. 把「日期 + 话题 + 板块」写进 热点记录.xlsx
4. 把板块拿到 Axiom「情报精选」搜索
5. 点最新一条 → 里面有「核心观点」
6. 把核心观点带到雪球发长文，再结合这些观点补充看法 → 发帖

【🔴 合规：第 6 步是全流程最大的风险点】

"把核心观点复制到雪球" —— 实测 Axiom 的 coreView.conclusion 里大量是
**方向性建议**，直接复制即构成荐股：
  "军贸逻辑已由事件预期进入订单兑现阶段，配置重心应由单一展期交易转向
   体系化军贸与低空防务的订单兑现能力"        ← 隐含该买哪类标的
  "产业重心应由炒作英伟达映射转向有量产、订单和现实业绩支撑的国产链，
   但应优先选择位置较低且兑现节奏明确的标的"  ← 明确优先选择标的
  "【计算机策略】…关注22亿市值老牌妖股跨年机遇！"  ← 直接荐股

而 coreView.premises 是**事实性前提**，不含买卖指向：
  "储能、主干网与海风三条产业主线正从政策规划或审批节点转入需求释放阶段"
  "海外市场在关税、物流和提前备货扰动下仅维持价格平稳而预期走弱"

→ 本模块做三件事：
  a. **只取 safe_facts**（premises + summary），过滤掉 conclusion
  b. **不写任何个股名**（话题原文里的个股也要剔除）
  c. **能力边界写成事实陈述**（"它不做选股、不给买卖点"），不是免责条款
     —— 浙江证监局45号：免责声明无效

【正文结构（站内已验证的合规范式）】
① 今日盘面（引用热门话题的公开描述 + 板块，不带个股）
② 事实层（Axiom 核心观点里的前提，可安全引用）
③ 我的看法（基于前提做的推理 + 能力边界声明）
④ 风险提示
⑤ AI 标识 + 话题标签
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from content.xq_compliance import check_xq  # noqa: E402
from source.axiom_intel import (  # noqa: E402
    fetch_all, find_for_sector, grade_coreview, pick_for_sector,
)
from source.hotspot_log import to_public_desc  # noqa: E402
from source.sector_map import infer_sectors, sector_display, TAG_DISPLAY  # noqa: E402

ROOT = Path(__file__).parent.parent

# 雪球话题标签
#
# ⚠️ 2026-11-19 **上限 3 个**（用户实测结论）
#   用户手动发的 417579218 用了 3 个（#研究方法# #家电# #消费#）→ 发布成功。
#   之前尝试 4 个（投资心得/研究方法/家电/消费）→ 话题插入异常，发布失败。
#   → 固定 3 个：一个"方法论"标签 + 一个板块标签 + 一个行业/泛化标签。
#   → 顺序即优先级：方法论标签排第一（站内选材方向）。
TAGS_XQ = ["研究方法"]
TAG_MAX = 3

# 板块 → 更贴切的标签
#
# ⚠️ 标签必须是**站内存在的热门话题词**，
#    用申万一级标签（电子/机械设备）当话题标签很生硬，
#    概念词（先进封装/CPO/绿电）才是用户会搜的。
SECTOR_TAGS = {
    "半导体": ["半导体", "国产替代"],
    "先进封装": ["先进封装", "半导体"],
    "光刻胶": ["光刻胶", "半导体"],
    "算力芯片": ["算力芯片", "半导体"],
    "芯片": ["芯片", "半导体"],
    "家电": ["家电", "消费"],
    "电力设备": ["电力设备", "新能源"],
    "绿电": ["绿电", "电力设备"],
    "人形机器人": ["人形机器人", "机器人"],
    "机器人": ["机器人", "人形机器人"],
    "医药生物": ["医药", "创新药"],
    "军工": ["军工", "国防"],
    "光伏": ["光伏", "新能源"],
    "AI眼镜": ["AI眼镜", "消费电子"],
    "有色金属": ["有色", "资源"],
    "黄金": ["黄金", "有色"],
    "煤炭": ["煤炭", "资源"],
    "冰雪经济": ["冰雪经济", "消费"],
    "算力": ["算力", "人工智能"],
    "CPO": ["CPO", "光模块"],
    "光模块": ["光模块", "CPO"],
    "PCB": ["PCB", "电子"],
    "汽车": ["汽车", "新能源车"],
    "消费": ["消费", "零售"],
    "零售": ["零售", "消费"],
    "电子": ["电子", "半导体"],
    "机械设备": ["机械", "制造"],
    "计算机": ["计算机", "科技"],
    "通信": ["通信", "科技"],
    "传媒": ["传媒", "科技"],
    "食品饮料": ["食品饮料", "消费"],
}


def _strip_stock_names(text: str, hot_stocks: list[str] | None = None) -> str:
    """
    去掉正文里可能出现的个股名。

    三道防线：
      1. 热股榜交叉验证（最可靠）
      2. 六位代码
      3. 常见公司名尾（"XX股份"等）
    """
    if not text:
        return text
    if hot_stocks:
        for n in hot_stocks:
            if n:
                text = text.replace(n, "")
    text = re.sub(r"\b\d{6}\.(SH|SZ|BJ|HK)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[一-龥]{2,6}(股份|集团|控股|科技|证券|银行|保险|药业|电子)", "", text)
    return re.sub(r"[，,]{2,}", "，", text).strip("，,、 ")


def _has_company_name(text: str) -> bool:
    """判断一句话里是否含具体公司名（含则整条丢弃）。"""
    from source.sanitize import forbidden_check
    return bool(forbidden_check(text))


def _compose_view(facts: list[str], sector_name: str, topic_desc: str) -> str:
    """
    基于 Axiom 核心观点的前提，写"我的看法"。

    ⚠️ 关键：看法必须是**对事实的推理**，不是**对标的的判断**。
    合规写法："这说明关注点已经从X转向Y"（讨论逻辑）
    违规写法："建议关注该板块龙头"（指向标的）
    """
    if not facts:
        return (
            f"我的看法比较朴素：{topic_desc}这种走势，先把它当成情绪和资金的"
            f"结果来看，而不是当成结论。真正值得追的是背后有没有能验证的东西——"
            f"比如产能、订单、或者政策口径本身。如果这些还看不到，"
            f"那它就只是当天的热度。\n\n"
            f"我一般会等两到三个数据点出来再判断，而不是在第一天就下结论。"
        )

    lines = []
    lines.append(
        f"把上面几条事实放在一起看，我关注的不是{topic_desc}当天的表现，"
        f"而是这几条事实之间有没有互相印证。")
    lines.append("")
    lines.append(
        f"其中一个我觉得容易被忽略的点：这些前提的成立条件是什么。"
        f"如果其中任何一条发生变化，整条推导链就要重新走一遍——"
        f"这也是我不愿意直接照搬结论的原因。")
    lines.append("")
    lines.append(
        f"所以我自己的做法是把关注点放在这几条可验证的指标上，"
        f"而不是放在当天的盘面上。{sector_name}这种板块轮动快的方向，"
        f"追热点的盈亏比通常很差，等一个能证伪的信号反而更稳。")
    lines.append("")
    lines.append(
        f"工具在这里的作用是帮我把事实和结论分开、"
        f"把前提条件一条条列出来。它不做选股、不给买卖点、不预测涨跌，"
        f"判断还是我自己做。")
    return "\n".join(lines)


def _pick_relevant_facts(facts: list[str], sector_name: str,
                         keywords: list[str] | None = None,
                         other_sectors: list[str] | None = None) -> list[str]:
    """
    从事实句里挑出**与板块相关**的，优先用。

    ⚠️ 为什么需要（2026-11-19 实测）：
    一条「全市场复盘」情报的 coreView 里，facts 混着三类内容：
      · 全市场层面（"创业板指跌幅最大""全市场成交缩量"）→ 与家电板块无关
      · 其他板块层面（"晶圆厂收入上升""特高压招标增长"）→ 与家电无关
      · 本板块层面（"家电出口单月增速高于累计增速"）→ **这才是我们要的**
    不做筛选的话，正文会变成"用家电的话题讲全市场的事"——跑题。

    ⚠️ 2026-11-19 二次修正：**其他板块的词也要排除**
    实测「半导体」板块选中的情报里，
    facts 混进了"家电及消费电子企业依托新品、渠道和市占率优势拓展海外增长空间"
    → 讲的是家电，不是半导体 → 必须剔除。

    ⚠️ 2026-11-19 三次修正（**跨板块事实必须真排除**）：
    实测「机器人」话题选中「11月19日复盘笔记」后，
    facts 混进了"山西炼焦煤停产规模持续累积""核心城市二手房价格保持正增长"
    "海外变压器交付排产延长" —— 这些**一个都不属于机器人**，
    正文却拿它们当"机器人方向的事实"来讲 → 严重跑题。
    → 排除不能只靠"今日话题涉及的板块词"（other_sectors 太窄），
      必须用**全量行业名词库**做判定：
       · 句子命中目标板块的词 → 保留
       · 句子命中**别的行业名词**（煤炭/房地产/电网/家电…）→ 剔除
       · 两边都不命中 → 属于"全市场层面"，剔除（不能拿来充数）

    做法：
      1. 剔除命中其他行业名词的事实
      2. 优先保留命中目标板块词/关键词的事实
      3. 全市场层面的事实（指数/成交/风险偏好）→ 剔除
    """
    if not facts:
        return []
    from source.sector_map import INDUSTRY_NOUNS

    kws = [sector_name] + list(keywords or [])
    kws = [k for k in kws if k]
    # 其他板块词（需要排除的）
    others = [o for o in (other_sectors or []) if o and o != sector_name]

    # 目标板块的判定词（板块显示名 + 关键词里属于该板块的概念词）
    target_terms = {k for k in kws if k}

    # 全量行业名词 → 归属板块
    noun_owner: dict[str, str] = {}
    for noun, owner in INDUSTRY_NOUNS.items():
        noun_owner[noun] = owner

    def hits_other_industry(f: str) -> bool:
        """这句事实是否在讲别的行业？"""
        for noun, owner in noun_owner.items():
            if noun not in f:
                continue
            # 目标板块本身就是这个名词 → 不算别的行业
            if noun in target_terms or owner == sector_name:
                continue
            # 目标板块词里包含这个名词（如目标="先进封装"，名词="封装"）→ 不算
            if any(noun in t for t in target_terms):
                continue
            # 目标板块是名词所属板块的近亲（都含同一批词）→ 不算
            if any(owner and owner in t for t in target_terms):
                continue
            return True
        return False

    def hits_target(f: str) -> bool:
        return any(k in f for k in target_terms)

    # 1) 剔除别的行业的
    clean = [f for f in facts if not hits_other_industry(f)]
    # 2) 剔除明确的全市场层面（指数/成交/风险偏好）
    market_level = re.compile(
        r"(指数|成交地量|成交额|风险偏好|普跌|普涨|两市|大盘|涨跌家数|"
        r"北向资金|主力资金|情绪(?:回暖|退潮)|赚钱效应)")
    clean = [f for f in clean if not market_level.search(f)]
    # 3) 剔除**空话**（既没板块、也没行业、也没产业数据）
    #    实测混进机器人帖的："宏观与产业基本面仍有韧性""说明当期宏观数据具有韧性"
    #    "技术进步正直接转化为内容生产效率提升""产业化推进获得政策层面的技术指引"
    #    → 这些话放到任何板块都成立 = 没有信息量，引用它们只是凑字数。
    vacuous = re.compile(
        r"(仍有韧性|具有韧性|宏观(?:与产业)?基本面|说明当期|宏观数据|"
        r"政策层面的技术指引|技术进步正直接转化为|获得政策层面的|"
        r"直接转化为|协同建设|共同表明|重要支撑|持续关注|值得注意)")
    clean = [f for f in clean if not vacuous.search(f)]

    # 4) 优先命中目标板块词
    #    ⚠️ 2026-11-20 修正：**不能因为"有命中"就丢弃全部不命中的**
    #    实测「半导体」情报的 5 条事实里只有 1 条含"半导体"字样，
    #    另外 4 条（"并扩大国产半导体设备需求""设备需求增长逻辑成立的前提是
    #    下游扩产和技术突破按期实现""同时验证扩产与技术突破进度"…）
    #    全部是**贴题的半导体事实**，只是没写"半导体"三个字。
    #    旧逻辑 `if on_topic: clean = on_topic` 把它们全删了 → 只剩 1 条。
    #    → 现在：命中的排前面，其余**保留**（排序而非过滤）。
    on_topic = [f for f in clean if hits_target(f)]
    if on_topic:
        rest = [f for f in clean if not hits_target(f)]
        clean = on_topic + rest

    # 贴题度排序：命中板块词 > 含产业数据词 > 其余
    def relevance(f: str) -> int:
        if hits_target(f):
            return 0
        if re.search(r"(产能|利用率|渗透率|招标|订单|出货|交付|收入|营收|"
                     r"利润|规模|覆盖|工艺|设备|材料|芯片|封装|整机|模组|"
                     r"产线|基地|渗透|迭代|增长|下降|上升|扩大|收窄|突破|"
                     r"达到|披露|公布)", f):
            return 1
        return 2

    clean.sort(key=relevance)
    return clean


def build_hot_post(topic: dict, intel_row: dict, hot_stocks=None,
                   topic_index: int = 0) -> dict:
    """
    用「热点话题 + Axiom 核心观点」生成一篇雪球长文。

    topic:    {"text": 话题原文, "sectors": [...], "keywords": [...]}
    intel_row: Axiom 情报记录（已过记录级过滤）
    """
    sectors = topic.get("sectors") or []
    sector_name = sector_display(sectors, topic.get("text", ""))
    graded = grade_coreview(intel_row)
    # 2026-11-19：分级重构后，facts = 客观事实子句，methods = 方法/判据句
    all_facts = [f for f in graded.get("facts", []) if f]
    # 优先与板块相关的事实（否则正文会跑题）
    # ⚠️ other_sectors：今日热门话题涉及的**其他**板块词 →
    #    用于剔除"讲的是别的板块"的事实句（如半导体帖里混入家电事实）
    other_secs = [s for s in (topic.get("other_sectors") or []) if s]
    facts = _pick_relevant_facts(all_facts, sector_name,
                                 topic.get("keywords") or [],
                                 other_secs)[:6]
    methods = [m for m in graded.get("methods", []) if m][:2]
    topic_desc = to_public_desc(topic.get("text", ""), sectors, hot_stocks)

    # ① 今日盘面
    para1 = (
        f"今天{sector_name}方向有个动静：{topic_desc}。\n\n"
        f"先说清楚我的立场：这里不讨论任何个股，也不给买卖点，"
        f"更不预测后面的涨跌。我只把这个方向上最近能查到的一些事实摆出来，"
        f"再说说我的看法。"
    )

    # ② 事实层（去重 + 剔公司名）
    if facts:
        para2 = "下面是我梳理出来的几条事实：\n\n"
        seen = set()
        idx = 0
        for f in facts:
            f2 = _strip_stock_names(f, hot_stocks)
            # 去重：完全相同或包含关系都跳过
            key = re.sub(r"[\s，,。.、；;：:]", "", f2)[:40]
            if not key or key in seen:
                continue
            if any(key in k or k in key for k in seen):
                continue
            # 含公司名的整条丢弃（更保守：宁可少引一条）
            if _has_company_name(f2):
                continue
            seen.add(key)
            idx += 1
            para2 += f"{idx}. {f2}\n"
        para2 += ("\n这些是从公开信息和产业数据里整理出来的，"
                  "每一条都有它自己的时间口径和适用范围。"
                  "我不打算在这里展开讲，判断留给后面的部分。")
    else:
        para2 = (
            "我原本想在这里列几条具体的事实，但核对的时候发现，"
            "能查到的内容里混了不少带方向判断的表述，"
            "不适合直接搬过来。所以这里我只讲方法："
            "一个方向的热度到底是不是有基本面支撑，"
            "其实只要看三件事——需求有没有变化、供给有没有变化、"
            "政策口径有没有变化。这三件事都能找到可验证的数据点，"
            "而不是只能看盘面情绪。")

    # ②b 判据层（方法论）——这是"研究方法"标签真正承载的东西
    if methods:
        para2b = "顺带说说我看这类信息的一个判据：\n\n"
        for i, m in enumerate(methods, 1):
            m2 = _strip_stock_names(m, hot_stocks)
            if not m2 or _has_company_name(m2):
                continue
            para2b += f"· {m2}\n"
        para2b += ("\n这几条不是结论，是筛选口径——"
                   "满足才算有支撑，不满足就说明还只是情绪。")
        para2 = para2 + "\n\n" + para2b.rstrip()

    # ③ 我的看法
    para3 = _compose_view(facts, sector_name, topic_desc)

    # ④ 风险提示
    para4 = (
        "最后照例说清楚边界：以上是我个人的信息整理与方法记录，"
        "不构成任何投资建议；文中不涉及任何个股、代码与买卖建议。"
        "市场有风险，决策需自负。"
    )

    body = "\n\n".join([para1, para2, para3, para4])

    # 标题：用板块 + 事实角度，不用话题原文（原文含个股名）
    title_templates = [
        f"{sector_name}这个方向，今天值得把事实和情绪分开看",
        f"{sector_name}：把能验证的前提列出来，比追热度有用",
        f"关于{sector_name}，我更关心那几个可验证的前提",
    ]
    title = title_templates[topic_index % len(title_templates)]

    # 标签：方法论标签 + 板块标签 + 行业泛化标签 = **最多 3 个**
    # ⚠️ 上限 3 是用户实测结论：4 个会导致话题插入异常、发布失败。
    tags = list(TAGS_XQ)

    # ⚠️ 2026-11-20 修正：**优先用概念词做标签，不要用申万一级标签**
    #   实测「算力芯片」帖的标签是 ['研究方法','电子','计算机'] ——
    #   "电子""计算机"是申万行业分类，不是用户在雪球搜的话题词。
    #   → 优先板块显示名本身（"算力芯片"/"半导体"/"先进封装"…）
    if sector_name and sector_name not in tags:
        tags.append(sector_name)

    # 再补板块专属标签（若与板块名重复则跳过）
    for t in SECTOR_TAGS.get(sector_name, []):
        if t not in tags:
            tags.append(t)
    # 仍不足 → 用申万一级标签的中文名兜底
    if len(tags) < TAG_MAX:
        for s in sectors:
            disp = TAG_DISPLAY.get(s)
            if disp and disp not in tags:
                tags.append(disp)
                if len(tags) >= TAG_MAX:
                    break
    # 清洗：话题标签只能是干净单一名词（不能带斜杠/空格/标点）
    cleaned = []
    for t in tags:
        t = re.sub(r"[^\w一-龥+#]", "", str(t or "")).strip()
        if t and t not in cleaned:
            cleaned.append(t)
    tags = cleaned[:TAG_MAX]

    return {
        "id": f"XQ-hot-{topic_index}-{sector_name}",
        "title": title,
        "body": body,
        "tags": tags,
        "images": [],
        "ai_marked": True,
        # 元信息（用于写台账与排查）
        "_meta": {
            "topic": topic.get("text"),
            "topic_public": topic_desc,
            "sector": sector_name,
            "sectors_sw": sectors,
            "intel_title": graded["title"],
            "intel_date": graded["postDate"],
            "intel_l2": graded["categoryL2"],
            "facts_used": len(facts),
            "methods_used": len(methods),
            "facts_blocked": len(graded["blocked"]),
            "facts_caution": len(graded["directions"]),
        },
    }


def build_from_hotspots(topics: list[dict], limit: int = 1,
                        hot_stocks=None) -> list[dict]:
    """
    主流程：话题 → Axiom 情报 → 成文。

    topics: publish/xq/hotspots.fetch_hot_topics 的结果
    """
    rows = fetch_all()
    if not rows:
        print("⚠️ Axiom 情报精选拉取失败")
        return []

    picks = [t for t in topics if t.get("has_sector")][:limit * 3]
    posts = []

    for t in picks:
        kws = t.get("keywords") or []
        if not kws:
            continue
        secs = infer_sectors(t.get("text", ""))
        sector = sector_display(secs, t.get("text", ""))
        row = pick_for_sector(rows, sector, kws)
        if not row:
            print(f"  {t['text'][:24]} → Axiom 无高相关情报")
            continue
        post = build_hot_post(t, row, hot_stocks, len(posts))
        posts.append(post)
        print(f"  ✅ {t['text'][:26]}")
        print(f"     板块={post['_meta']['sector']} | 情报={post['_meta']['intel_title'][:34]}")
        print(f"     相关度={row.get('_relevance')} | 引用事实 {post['_meta']['facts_used']} 条 "
              f"(封杀{post['_meta']['facts_blocked']}/方向{post['_meta']['facts_caution']})")
        if len(posts) >= limit:
            break

    return posts


def check_posts(posts: list[dict]) -> list[dict]:
    """过合规闸门。"""
    from content.compliance import check_note
    from source.sanitize import forbidden_check

    out = []
    for p in posts:
        issues = []
        r1 = check_note({"title": p["title"], "body": p["body"],
                         "tags": p.get("tags", []), "images": p.get("images", [])},
                        platform="tgb")
        if not r1["passed"]:
            issues += [f"[通用] {v['category']}" for v in r1["violations"]]
        r2 = check_xq(p)
        if not r2["passed"]:
            issues += [f"[雪球] {v['category']}" for v in r2["violations"]]
        issues += [f"[项目] {x}" for x in
                   forbidden_check(p["title"]) + forbidden_check(p["body"])]
        p["_issues"] = issues
        out.append(p)
    return out


if __name__ == "__main__":
    from publish.xq.hotspots import fetch_hot_stocks, fetch_hot_topics
    from publish.xq.browser import XqSession

    with XqSession(headless=False) as s:
        topics = fetch_hot_topics(s)
        stocks = fetch_hot_stocks(s)

    print(f"\n热门话题 {len(topics)} 条 | 热股榜 {len(stocks)} 只\n")

    posts = build_from_hotspots(topics, limit=2, hot_stocks=stocks)
    posts = check_posts(posts)

    OUT = ROOT / "content" / "for_xq_hot"
    OUT.mkdir(parents=True, exist_ok=True)
    for p in posts:
        print("\n" + "=" * 70)
        print(f"标题: {p['title']}")
        print(f"标签: {p['tags']} | AI标识: {p['ai_marked']}")
        print(f"字数: {len(p['body'])}")
        print(f"合规: {'PASS' if not p['_issues'] else p['_issues']}")
        print("-" * 70)
        print(p["body"])
        if not p["_issues"]:
            (OUT / f"{p['id']}.json").write_text(
                json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n\n已存: {OUT}")
