"""
脱敏层 —— 把 AXIOM 原始数据转成**可发布**的小红书素材。

这是整个链路里最关键的一道闸门。原因：

小红书《社区金融生态公约》八类禁止内容里，有四类会直接被 AXIOM 的原始数据触发：
  1. **非法荐股** —— 情报精选/日报里全是「股票名称 + 6位代码 + 板块观点」
  2. **误导性信息** —— 收益/胜率缺时间区间与统计口径
  3. **恶意炒作市场走势** —— 「本轮调整已出现相对底部特征」这类方向性判断
  4. **违规导流** —— 站点域名、品牌名

所以这里的规则是：**只保留"方法论"层，剥离"标的"层**。

具体做法：
  - 具体公司名 → 抽象为「某行业头部公司」/ 行业名 + 序号
  - 6 位股票代码 → 全部移除
  - 板块方向性判断 → 改写为「我的工具在做 X 分析」的动作陈述
  - 研报原文标题 → 改写为「研报里这类结论长什么样」的结构描述
  - 任何站外标识 → 只保留在"品牌署名"位置，不进正文与标签

【重要】脱敏后仍必须过 compliance.py 的闸门，两层都要过。
"""

import re

# ---------------------------------------------------------------- 规则

# 6 位股票代码（含 .SH/.SZ 后缀）
RE_STOCK_CODE = re.compile(r"\b\d{6}(?:\.(?:SH|SZ|HK|BJ))?\b")

# 常见公司名后缀（用于识别"这里在讲某家公司"）
# 注意三点：
#  1. 必须用**负向前瞻**排除"电子材料""医药生物"这类普通词组
#  2. 前缀长度要**短且固定**（{2,4}）—— 贪婪匹配会把
#     「只有很少原始材料」整段吞成公司名。
#     改成 {2,4} 后只会匹配到"少材料"这类短片段，再由 NEG_PREFIX 过滤。
#  3. 前缀里**不能出现助词**（的/了）—— 否则
#     「块底下的材料」「它的原始材料」这类指代短语会被当成公司名。
#     真正的公司名前缀不会带助词，所以这条负向断言是安全的。
COMPANY_SUFFIX = re.compile(
    r"(?![一-龥A-Za-z]{0,3}[的了])"
    r"[一-龥A-Za-z]{2,4}"
    r"(?:股份|科技|集团|控股|有限公司|实业|生物|医药|装备|证券|银行|保险|"
    r"地产|建设|电力|能源|通信|传媒|智能|精密|重工|光电|化学|环保|航空|物流|食品|"
    r"乳业|药业|医疗|软件|电机|机器人|技术|电子|材料)"
    r"(?![一-龥]{0,4}(?:材料|板块|行业|产业|设备|制造|服务|科技|技术))"
)

# 「通用限定词」前缀 —— 这些是行业/概念形容词，不是公司名。
#
# 为什么需要它：「原始材料」「电子材料」「行业材料」「新材料」这类词，
# 词尾正好落在 COMPANY_SUFFIX 的后缀表里，正则会当成公司名。
# 但它们在正文里是纯普通词组，一旦被误判：
#   · forbidden_check 会报"正文含疑似具体公司名" → 整篇被拦下
#   · sanitize_text 会把正常词替换成"头部标的" → 正文语义被破坏
#
# 所以这里显式列出高频通用前缀，命中的**不算公司名**。
# 判定方式：把匹配到的词拆成「前缀 + 后缀」，
# 前缀落在下表 → 判为普通词组。
GENERIC_PREFIXES = {
    # 材料/资源类
    "原始", "新型", "新型", "高端", "优质", "关键", "基础", "复合", "固体",
    "液体", "气体", "化学", "建筑", "工程", "包装", "装饰", "纺织", "服装",
    "汽车", "家电", "家居", "安防", "军工", "机械", "钢铁", "有色", "煤炭",
    "石油", "农业", "消费", "上游", "下游", "中间", "原料", "辅料", "一级",
    "二级", "三级", "整体", "局部", "具体", "相关", "上述", "部分", "全部",
    "更多", "较少", "很少", "大量", "少量", "足够", "额外", "典型", "常见",
    "行业", "产业", "电子", "电力", "通信", "传媒", "环保", "航空", "物流",
    "食品", "医药", "医疗", "乳业", "药业", "软件", "智能", "精密", "重工",
    "光电", "装备", "证券", "保险", "银行", "地产", "建设", "能源", "实业",
    "机器人", "技术", "电子", "材料", "集团", "控股", "股份",
}

# ---- 时间/序数/方位前缀（2026-11-19 新增）----
#
# 为什么需要：
#   正文里高频出现「今天家电这个方向」「昨日半导体」「本月消费」
#   而「传媒/材料/电子/医药/机械/通信/电力/能源/…」都在公司后缀表里
#   → COMPANY_SUFFIX 会匹配成「今天传媒」「昨日材料」→ 误判为公司名 → 正文被误杀。
#
#   实测：2026-11-19 跑 dry-run 时，
#     "[项目] 正文含疑似具体公司名: 今天传媒"  ← 整篇被拦
#
# → 前缀命中下列词（或含这些字）→ 判为「时间/序数修饰 + 板块词」，不是公司名。
TIME_SERIAL_PREFIXES = {
    "今天", "今日", "昨日", "昨天", "明日", "明天", "本周", "上周", "下周",
    "本月", "上月", "下月", "今年", "去年", "明年", "近期", "目前", "当前",
    "当下", "今日", "当天", "近日", "日前", "最近", "目前", "同期", "环比",
    "年初", "年末", "季度", "全年", "过去", "未来", "眼下", "最近",
}


# ---- 板块/概念词白名单 ----
# ⚠️ 这些词以"公司后缀"结尾（人形机器人 / 固态电池 / 光刻胶…），
#    会被 COMPANY_SUFFIX 误判成公司名。
#    它们是**板块词**，必须排除 —— 而且这是**跨平台**问题：
#    小红书/抖音/雪球都会中招。
SECTOR_TERM_EXCLUDE = {
    "人形机器人", "机器人", "固态电池", "锂电池", "电池", "光伏", "风电",
    "储能", "芯片", "半导体", "光刻胶", "光模块", "算力", "数据中心",
    "云计算", "信创", "智能驾驶", "新能源车", "创新药", "CXO", "疫苗",
    "中药", "军工", "国防军工", "商业航天", "卫星互联网", "算力芯片",
    "冰雪经济", "谷子经济", "跨境电商", "预制菜", "同花顺", "大模型",
    "AI眼镜", "智能终端", "消费电子", "医疗器械", "电力设备", "工程机械",
    "特高压", "氢能源", "工业母机", "减速器", "灵巧手", "培育钻石",
}


def is_company_like(token: str) -> bool:
    """
    判断一个「前缀+后缀」形态的词是否像公司名。

    用于把 COMPANY_SUFFIX 的匹配结果再筛一遍：
      · 前缀是通用限定词（原始材料/电子材料/行业材料…）→ 不是公司名
      · 前缀含助词（它的原材料）→ 不是公司名
      · 前缀含虚词/副词（时又没有技术 / 当作历史材料）→ 不是公司名
      · 长度太短（<3）→ 不是公司名
    """
    if not token or len(token) < 3:
        return False
    # 板块词白名单优先
    # ⚠️ 必须用**子串**判断：正则可能把词前后的字一起吃掉
    #    （"今天人形机器人" → token="天人形机器人"）
    if token in SECTOR_TERM_EXCLUDE:
        return False
    for t in SECTOR_TERM_EXCLUDE:
        if len(t) >= 3 and t in token:
            return False
    for suf in sorted(_SUFFIX_LIST, key=len, reverse=True):
        if token.endswith(suf) and len(token) > len(suf):
            prefix = token[: -len(suf)]
            if prefix in GENERIC_PREFIXES:
                return False
            if any(c in prefix for c in "的了是在和与或并且"):
                return False
            # 虚词/副词/连词 —— 真正的公司名前缀不会含这些
            #   「时又没有技术」「当作历史材料」「这类技术」都不是公司名
            if any(c in prefix for c in "又没作当时这类那整块些个把被就还也很更最"):
                return False
            # ⚠️ 时间/序数/方位前缀（2026-11-19 新增）
            #   「今天传媒」「昨日半导体」「本月家电」—— 这些是**时间修饰+板块词**，
            #   绝不是公司名。但 "传媒/材料/电子/医药/机械" 都在公司后缀表里，
            #   正则会把「今天」+「传媒」当成公司名 → 误杀正文。
            #   → 前缀落在时间/序数词表 → 判为普通词组。
            if prefix in TIME_SERIAL_PREFIXES:
                return False
            # 前缀含时间/序数字 → 同样不是公司名
            if any(c in prefix for c in "今昨明前后本上下一二三四五六七八九十日月年季"):
                return False
            return True
    return True

# 后缀表（供 is_company_like 拆分前缀用）
_SUFFIX_LIST = [
    "股份", "科技", "集团", "控股", "有限公司", "实业", "生物", "医药",
    "装备", "证券", "银行", "保险", "地产", "建设", "电力", "能源",
    "通信", "传媒", "智能", "精密", "重工", "光电", "化学", "环保",
    "航空", "物流", "食品", "乳业", "药业", "医疗", "软件", "电机",
    "机器人", "技术", "电子", "材料",
]

# 已知公司名单（脱敏时优先精确匹配，避免依赖后缀猜测）
# 来源：AXIOM 情报精选的 stockMapping 中出现过的名称（实测样本）
KNOWN_COMPANIES = [
    "新华保险", "中国人寿", "中控技术", "万华化学", "上海飞科电器", "恒工精密",
    "英杰电气", "长江电力", "沐曦股份", "迪安诊断",
    "紫光股份", "龙芯中科", "科远智慧", "冰轮环境", "协创数据", "同花顺",
    "中科曙光", "东方财富", "三六零", "东方电气", "北摩高科", "贵州茅台",
    "英杰电气", "拓荆科技", "中微半导体设备", "中控技术", "万华化学",
    "太极实业", "阿里巴巴",
    # 无标准后缀的公司名（靠 COMPANY_SUFFIX 正则抓不到，必须显式登记）
    # 来源：AXIOM 日报/研报实测出现过的名字
    "宁波精达", "麦格米特", "高争民爆", "华鑫证券",
]

# ⚠️ 海外品牌/公司名（2026-11-19 新增）
#
# 为什么必须显式登记：
#   COMPANY_SUFFIX 靠中文公司后缀（股份/科技/集团…）识别公司名，
#   而「英伟达」「昇腾云」「台积电」「SK海力士」这些**没有中文公司后缀**
#   → 正则一个都抓不到 → 漏进正文。
#
#   实测漏洞：2026-11-19 的算力帖正文里就写着
#     "英伟达管理层更迭的核心影响是研发容错与生态重心重估"
#     "台积电和SK海力士等上游环节"
#   forbidden_check 全部返回 [] —— **合规闸门完全失效**。
#
#   雪球规则：不得出现个股名称/代码。英伟达(美股)、台积电(台股) 同样算。
BRAND_COMPANIES = [
    "英伟达", "NVIDIA", "台积电", "TSMC", "SK海力士", "海力士", "美光",
    "英特尔", "Intel", "AMD", "英伟达的", "三星电子", "三星",
    "昇腾云", "昇腾", "openPangu", "盘古", "Optimus", "特斯拉", "Tesla",
    "苹果", "Apple", "微软", "Microsoft", "谷歌", "Google", "亚马逊", "Amazon",
    "Meta", "OpenAI", "Anthropic", "寒武纪", "海光", "中芯国际", "拓普集团",
    "大港股份", "襄阳轴承", "华能蒙电", "美利云", "华工科技", "安泰集团",
    "宁德时代", "比亚迪", "隆基", "阳光电源", "药明康德", "恒瑞",
    "阿里巴巴", "腾讯", "字节跳动", "百度", "京东", "小米", "华为",
    "联想", "长电科技", "通富微电", "华天科技", "兆易创新", "北方华创",
    "中微公司", "东山精密", "沪电股份", "生益科技", "胜宏科技",
    # ⚠️ 2026-11-19 补：品牌名 + 行业词的组合（无中文公司后缀 → 正则抓不到）
    #   "地平线机器人交流要点" → "地平线" 是公司名，"机器人" 是行业词
    #   "耐科装备" / "凯美特气" / "中国重汽" 同理
    "地平线", "耐科装备", "凯美特气", "中国重汽", "拓普", "潍柴动力",
    "思源电气", "TCL中环", "中环", "长江电力", "药明", "兆易",
    "海优新材", "钧达股份", "帝科股份", "聚辰股份", "澜起科技",
    "屹唐股份", "中科飞测", "鹏鼎控股", "重汽", "日盈电子",
    "协创数据", "行云科技", "利通电子", "杰创智能", "亿田智能",
    "盛视科技", "华达科技", "浙江金洲管道科技",
]

# 高风险表述（方向性判断 / 收益承诺）
RISKY_PHRASES = {
    "相对底部特征": "阶段性特征",
    "接近阶段性尾声": "存在阶段性企稳信号",
    "拐点来临": "出现关键变化",
    "重点关注": "我在关注",
    "推荐关注": "我记录了",
    "布局": "研究",
    "买点": "观察点",
    "卖点": "观察点",
    "目标价": "定价区间",
    "增持": "看好",
    "减持": "谨慎",
    "买入": "持有",
    "卖出": "调整",
    "低估值的配置空间": "处于历史偏低区间",
    "性价比凸显": "相对合理",
    "确定性": "可验证性",
    "景气度": "景气情况",
}

# 截图遮蔽用的方向性关键词表（比 RISKY_PHRASES 更全、更严格）
#
# 为什么不复用 RISKY_PHRASES：
#   RISKY_PHRASES 是**词→词的降级替换**（"布局"→"研究"），用于正文，
#   替换后语义还在，正文读者看到的是弱化表述。
#   截图不一样 —— 图片里的文字平台要 OCR 识别，弱化表述同样会被判违规，
#   所以截图必须**整句隐去**，不能只换词。
#
# 收录标准：只收**构成"行情判断 / 操作建议"语义**的词。
# 纯描述性的宏观词（预期/主导/传导/持续/延续…）刻意不收 ——
# 收了会把整段宏观速览糊掉，图片就失去了产品演示价值。
#   例：「海外市场对利率路径及科技估值持续保持谨慎」是事实描述，不是荐股；
#      「A股处于震荡筑底阶段，建议维持防御型哑铃配置」是典型荐股，必须隐去。
RISKY_KEYWORDS = [
    # 行情位置 / 阶段判断
    "筑底", "见底", "见顶", "底部", "顶部", "拐点", "反弹", "企稳",
    "震荡", "上行", "下行", "走强", "走弱", "抗跌", "补涨", "回撤",
    "轮动", "主线", "赛道", "分化", "高低切",
    # 配置 / 操作建议
    "配置", "布局", "转向", "切换", "防御", "哑铃", "超配", "低配",
    "减配", "加仓", "减仓", "择时", "性价比", "低估值", "高估值",
    "估值优势", "配置空间", "筹码", "景气", "催化", "重点关注",
    "推荐", "看好", "增持", "减持", "买入", "卖出", "目标价",
    "抄底", "逃顶", "空间", "弹性", "低位主题", "高弹性",
    "优先防御", "维持防御",
]

# 署名判断的正则（机构名 + 判断动词）
# 例："华鑫策略认为…"、"XX证券指出…" —— 出现即隐去整句
# 注意：署名本身就是"第三方判断"的背书，比句子本身更敏感。
ATTRIBUTION_RE = re.compile(
    r"[一-龥]{2,4}(?:证券|策略|研究|研究院|投顾|资管|基金|团队|资本)"
    r"[一-龥]{0,6}?(?:认为|指出|表示|判断|预计|建议|提示)"
)


def _abstract_company(name: str, idx: int = 0) -> str:
    """把具体公司名抽象成行业级表述。"""
    labels = ["头部标的", "行业龙头", "细分领域代表公司", "该板块龙头"]
    return labels[idx % len(labels)]


def sanitize_text(text: str, *, keep_direction: bool = False) -> str:
    """
    对自由文本做脱敏。

    keep_direction=False（默认，小红书用）：
        移除股票代码、公司名、方向性判断 —— 只留事实与方法论。
    keep_direction=True（站内/日报存档用）：
        保留方向性表述，但仍移除股票代码。
    """
    if not text:
        return ""

    s = text

    # 1) 股票代码（无论哪种模式都移除）
    s = RE_STOCK_CODE.sub("", s)
    s = re.sub(r"[（(]\s*[，,]\s*[）)]", "", s)   # 清空代码留下的空括号
    s = re.sub(r"\s{2,}", " ", s)

    # 2) 公司名 → 行业级表述（先做，避免"重点关注XX公司"只替换了一半）
    if not keep_direction:
        seen = {}

        def repl_known(m):
            name = m.group(0)
            if name not in seen:
                seen[name] = len(seen)
            return _abstract_company(name, seen[name])

        # 先精确替换已知公司（长名优先，避免"拓荆"被截成"拓"）
        for name in sorted(set(KNOWN_COMPANIES) | set(BRAND_COMPANIES),
                           key=len, reverse=True):
            if name in s:
                s = re.sub(re.escape(name), repl_known, s)

        seen2 = {}

        def repl_generic(m):
            name = m.group(0)
            # 通用限定词（如"原始材料""行业材料"）不是公司名，保持原样
            if not is_company_like(name):
                return name
            if name not in seen2:
                seen2[name] = len(seen2)
            return _abstract_company(name, seen2[name])

        s = COMPANY_SUFFIX.sub(repl_generic, s)

    # 3) 方向性表述
    if not keep_direction:
        for bad, good in RISKY_PHRASES.items():
            s = s.replace(bad, good)

    # 4) 清理多余标点
    s = re.sub(r"[，,]\s*[，,]", "，", s)
    s = re.sub(r"[。.]\s*[。.]", "。", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_stock_mapping(mapping: dict | None) -> dict:
    """把 stockMapping 里的个股映射彻底清掉，只留行业标签。"""
    if not mapping:
        return {}
    out = {}
    for k, v in mapping.items():
        if k == "mentioned_stocks":
            out["industry_tags"] = sorted({
                t for t in (v or []) if isinstance(t, str)
            }) or []
        else:
            out[k] = v
    return out


def sanitize_intelligence(item: dict, idx: int = 0) -> dict:
    """
    情报精选（知识星球）单条脱敏。

    小红书端只保留：
      - 分类标签（行业/内容类型）
      - 一句话总结的**方法论部分**
      - 日期（用于"每日整理"的真实感）
    绝不保留：标题原文、原文链接、个股映射、作者、星球名。
    """
    return {
        "category_l1": item.get("categoryL1", ""),
        "category_l2": sanitize_text(item.get("categoryL2", "")),
        "sw_industry_tag": [t for t in (item.get("swIndustryTag") or []) if isinstance(t, str)],
        "summary": sanitize_text(item.get("summary", "")),
        "post_date": item.get("postDate", ""),
        "stock_mapping": sanitize_stock_mapping(item.get("stockMapping")),
        "_raw_title": item.get("title", ""),   # 仅供站内使用，不进正文
    }


def sanitize_research(item: dict, idx: int = 0) -> dict:
    """
    机构研报单条脱敏。

    小红书端只保留：
      - 分类（NEWS / RESEARCH / TRADING / COMPANY / CONCEPT）—— 这是"研报有哪几类"的教学素材
      - 核心观点的**结构描述**（不引用结论本身）
    绝不保留：文档名、原始观点、目标价、评级。
    """
    name = item.get("documentName") or item.get("title") or ""
    core = item.get("coreView") or item.get("summary") or item.get("coreOpinion") or ""
    return {
        "category": item.get("categoryL1") or item.get("category") or "",
        "category_l2": sanitize_text(item.get("categoryL2", "")),
        # 只保留研报"讨论了什么类型的问题"，不保留结论
        "topic_hint": _topic_hint(name, core),
        "collected_at": item.get("createdAt", "")[:10],
        "_raw_name": name,                     # 仅供站内
    }


def _topic_hint(name: str, core: str) -> str:
    """从研报标题/摘要里提取"话题类型"，而不是具体结论。"""
    cat = (name + " " + str(core)).lower()
    buckets = [
        ("行业配置与景气跟踪", ["行业", "板块", "配置", "景气", "周度", "月度"]),
        ("个股基本面拆解", ["公司", "中报", "年报", "季报", "业绩", "收入", "利润"]),
        ("资金与情绪面", ["龙虎榜", "资金", "情绪", "拥挤", "热度"]),
        ("产业链与技术演进", ["产业链", "技术", "机器人", "算力", "具身", "AI", "人形"]),
        ("宏观与政策跟踪", ["宏观", "政策", "央行", "利率", "通胀"]),
    ]
    for label, kws in buckets:
        if any(k.lower() in cat for k in kws):
            return label
    return "综合研究"


def sanitize_dashboard(dash: dict) -> dict:
    """
    每日概览脱敏。

    每个 section 保留：
      - section_title（板块名，安全）
      - key_points 脱敏后（关键：把"重点关注XX公司"改成"我在看XX方向"）
      - confidence_level（样本量透明度 —— 这是反误导的关键信号）
      - source_count
    不保留：section_content 原文（里面含大量公司名与方向性判断）
    """
    out = {
        "report_date": dash.get("reportDate", ""),
        "frequency": dash.get("frequency", "daily"),
        "data_window": [dash.get("dataWindowStart", "")[:10], dash.get("dataWindowEnd", "")[:10]],
        "sections": [],
    }
    for sec in dash.get("sections", []) or []:
        kps = [sanitize_text(k) for k in (sec.get("key_points") or [])]
        out["sections"].append({
            "section_key": sec.get("section_key", ""),
            "section_title": sec.get("section_title", ""),
            "key_points": kps,
            "confidence_level": sec.get("confidence_level", ""),
            "source_count": sec.get("source_count", 0),
            "source_titles_count": len(sec.get("supporting_source_titles") or []),
            "has_research": len(sec.get("source_research_ids") or []) > 0,
        })
    return out


def brand_line() -> str:
    """
    品牌署名句 —— 唯一允许出现品牌名的地方。

    规则：只做"事实陈述"（我用什么工具），绝不做"引导动作"（去搜/去看/扫码）。
    依据《交易导流违规管理细则》3.2 第 5 项：禁止"以明示或暗示方式诱导用户至
    私域或站外三方平台"。"工具署名"属陈述，"去搜XX"属诱导。
    """
    return "我这套流程跑在自己的效率投研工具 Axiom 上"


def forbidden_check(text: str, extra_names: set | None = None) -> list[str]:
    """
    发布前最后一道：检查正文里有没有漏网的站外标识 / 标的。

    与 compliance.py 互补 —— compliance 管通用红线，这里管"本项目特有"的：
    站点域名、品牌名、股票代码、具体公司名。

    extra_names:
        动态补充的公司名黑名单。AXIOM 每天出现的公司名会变
        （今天叫"奥普特"，明天可能是"今世缘"），
        KNOWN_COMPANIES 是静态列表，覆盖不全。
        → 调用方应把**当日 AXIOM 数据里真实出现的公司名**传进来。
    """
    issues = []
    low = text.lower()

    for dom in ["efficientinvest", "efficientinvest.cn", "axiom.cn"]:
        if dom in low:
            issues.append(f"站外域名/品牌: {dom}")

    if re.search(r"https?://", text):
        issues.append("正文含 URL")

    if RE_STOCK_CODE.search(text):
        issues.append("正文含 6 位股票代码")

    # 已知公司名（精确匹配，比正则可靠）+ 动态名单
    # ⚠️ 必须包含 BRAND_COMPANIES —— 海外品牌名没有中文公司后缀，
    #    靠 COMPANY_SUFFIX 正则抓不到（实测漏过"英伟达""台积电""昇腾云"）。
    pool = (set(KNOWN_COMPANIES) | set(BRAND_COMPANIES)
            | set(extra_names or []))
    hit_known = [n for n in pool if n and n in text]
    if hit_known:
        issues.append(f"正文含具体公司名: {hit_known[:3]}")

    # 疑似公司名（正则 + 排除普通词组与误报模式）
    # 误报模式：前导字是"有/很/太/该/本/各/某"时，多半是普通短语
    #   如「有很少的原始材料」「本行业」「各公司」
    # "它"/"的" 同样不可能是公司名首字，但常出现在
    #   「它的原始材料」「的原始信息」这类指代短语开头 → 一并排除
    NEG_PREFIX = ("有", "很", "太", "该", "本", "各", "某", "这", "那", "少", "多",
                  "什", "哪", "更", "最", "都", "也", "还", "就", "不", "没", "要",
                  "它", "的")
    for m in COMPANY_SUFFIX.finditer(text):
        w = m.group(0)
        if len(w) < 3:
            continue
        if w[0] in NEG_PREFIX:
            continue
        # 通用限定词（原始材料/电子材料/行业材料…）不是公司名
        # ⚠️ 也包含时间/序数前缀（今天传媒/昨日半导体）→ 不是公司名
        if not is_company_like(w):
            continue
        issues.append(f"正文含疑似具体公司名: {w}")
        break
    for w in ["知识星球", "zsxq", "星球", "扫码", "私信我拿", "加群", "主页链接"]:
        if w in text:
            issues.append(f"高风险导流/二次分发词: {w}")

    return issues


if __name__ == "__main__":
    print("=== 脱敏自测 ===\n")
    samples = [
        "【国盛计算机臻选】26Q3持仓分析：机构筹码仍处低位，估值性价比凸显",
        "保险板块利润承压时，应优先关注低投资收益对利润和偿付能力的约束，涉及新华保险和中国人寿",
        "英杰电气半导体和电子材料收入增速持续扩大，月平均订单量处于历史高位，中微半导体设备、拓荆科技",
        "帮我看看 603606.SH 今天的分时走势，重点看尾盘那几",
        "本轮市场调整已出现相对底部特征，拐点来临，重点关注新能源",
    ]
    for s in samples:
        out = sanitize_text(s)
        print(f"原: {s}\n脱: {out}\n")
    print("=== 发布前检查 ===")
    for s in ["我这套流程跑在自己的效率投研工具 Axiom 上",
              "我用的工具叫 efficientinvest",
              "今天看了下计算机板块的估值情况",
              "扫码进群领资料"]:
        print(f"  {s!r}\n    → {forbidden_check(s)}")
