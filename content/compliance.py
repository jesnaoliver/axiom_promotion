"""
合规检查器 —— 发布前的强制闸门。

依据：
  1. 《小红书社区金融生态公约》（2026-09-15 发布并试行）
     - 鼓励四条：专业做科普 / 分享真实理财经历 / 合规表达提示风险 / 标注 AI 生成
     - 禁止八类：恶意唱衰 / 恶意炒作市场走势 / 非法荐股 / 非法金融活动
                / 不实信息 / 误导性信息 / 违规导流 / 虚假人设
  2. 《小红书社区规范 2.0》AI 治理规则（2026-08-07 升级）
     - AI 生成内容必须主动标识
  3. 《AI 生成合成内容标识办法》
  4. 《交易导流违规管理细则》《危及交易安全违规管理细则（修订征求意见稿）》
  5. 知识星球《持续深化财经内容生态治理公告》（2026-09-22）

设计：任何一条 ERROR 级违规 → 直接阻断发布，不允许绕过。
"""

import re
from typing import Any

# ---------------------------------------------------------------- 违禁词库

# 【非法荐股 / 预测未来走势】
BANNED_SIGNAL = [
    "明日买什么", "今天买什么", "买什么", "卖什么", "明日推荐", "今日推荐",
    "推荐个股", "荐股", "可以上车", "现在上车", "马上下单", "抄底", "逃顶",
    "目标价", "止损位", "止盈位", "买点", "卖点", "入场点", "减仓点",
    "加仓点", "必涨", "必跌", "稳赚", "包赚", "躺赚", "抄作业",
    "明日行情", "预测涨跌", "涨跌预测", "明日剧本", "低吸", "打板",
    "明日操作", "操作建议", "个股诊断", "免费诊股", "诊股",
    "TOP50", "今日涨停", "明日涨停", "涨停股", "主力抢筹", "隔夜排名",
]

# 【误导性信息 / 收益承诺】
BANNED_PROMISE = [
    "保本", "保息", "稳赚不赔", "低风险高收益", "低风险高回报",
    "稳赚", "躺赢", "无风险", "零风险", "包票", "内幕消息",
    "内部消息", "独家渠道", "保证收益", "稳定盈利", "轻松赚钱",
    "月入", "年化收益", "收益率高达", "胜率90", "胜率 90",
    "一年翻倍", "稳定盈利", "闭眼买", "无脑买",
]

# 【违规导流 —— 站外引流 / 联系方式 / 暗号】
BANNED_REDIRECT = [
    "加微信", "加v信", "微信号", "公众号", "加群", "进群", "私我拿",
    "私信领取", "评论区领取", "暗号", "扣1", "扣 1", "回复1领取",
    "扫码", "二维码", "加星", "vx", "v信", "薇信",
    "知识星球", "戳链接", "链接见", "淘宝", "闲鱼", "外部链接",
    "点击链接", "主页链接", "简介链接", "看主页简介", "评论区置顶有",
    "百度网盘", "夸克", "阿里云盘",
]

# 【虚假人设 / 资质伪装】
BANNED_PERSONA = [
    "私募", "基金经理", "首席分析师", "持牌", "证券从业", "内部人士",
    "机构首席", "操盘手", "带单", "代客理财", "稳赚大师", "股神",
]

# 【恶意唱衰 / 恶意炒作】
BANNED_HYPE = [
    "崩盘", "血亏", "满仓干", "梭哈", "史诗级", "史诗级牛市", "狂飙",
    "暴涨", "暴跌", "必涨股", "史诗级行情",
]

# 联系方式正则（覆盖谐音变体与数字串）
RE_CONTACT = [
    re.compile(r"1[3-9]\d{9}"),                 # 手机号
    re.compile(r"[wW][\s:：]?[xX]"),           # wx / wx:
    re.compile(r"[vV][\s:：]?\d{5,}"),          # v + 数字
    re.compile(r"🍑🔍|🥝🔍|🍑\s*🔍"),          # 桃子+放大镜
    # 站外域名（含连字符主机名，如 xiaohongshu-abc.com / abc.top）
    re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z0-9][a-zA-Z0-9\-]{2,}\."
               r"(com|cn|top|xyz|net|vip|cc|io|me|club|shop|store)(?![a-zA-Z0-9])"),
    re.compile(r"公[-zZ]众\s*号?\s*[:：]?\s*[^\s，。！？]{2,}"),
]

# 股票代码（6 位数字）—— 出现具体代码极易被判定为荐股
RE_STOCK_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")

# 量化绩效数字（必须有区间口径配套）
RE_PERF = re.compile(
    r"(胜率|收益率|夏普|最大回撤|年化|年化收益|收益曲线|净值)\s*[:：]?\s*"
    r"[-+]?\d+(?:\.\d+)?\s*%?"
)

ERROR = "ERROR"
WARN = "WARN"

RULES = [
    (ERROR, "非法荐股/预测走势", BANNED_SIGNAL),
    (ERROR, "收益承诺/误导性信息", BANNED_PROMISE),
    (ERROR, "违规导流站外", BANNED_REDIRECT),
    (ERROR, "虚假人设/资质伪装", BANNED_PERSONA),
    (ERROR, "恶意炒作市场", BANNED_HYPE),
]


class ComplianceError(Exception):
    pass


def _scan(text: str, words: list[str]) -> list[str]:
    hits = []
    for w in words:
        if w and w in text:
            hits.append(w)
    return hits


# 否定/能力边界声明的上下文标记
# 命中这些词的句子，是在声明"我不做这件事"，属于合规资产而非违规
_NEGATION_MARKERS = [
    "不做", "不预测", "不推荐", "不给", "不谈", "不涉及", "不出现",
    "不要", "不做任何", "不作为", "无法", "不会", "不能", "不是",
    "避免", "拒绝", "不能做", "别当", "严禁", "禁止", "不靠", "不靠它",
]


def _in_negation_context(text: str, word: str) -> bool:
    """
    判断某个敏感词是否出现在"否定/边界声明"语境中。

    场景：淘股吧方法论帖里写"它不做选股、不预测涨跌"，
    这是**能力边界声明**，是合规资产，不能被当成违规。
    但"我推荐某票"里的"推荐"是真违规。

    做法：取该词所在句子，若句中含否定标记 → 视为否定语境。
    """
    if not word:
        return False
    # 逐句切分（中文标点 + 换行）
    import re as _re
    sentences = _re.split(r"[。！？\n；;]", text)
    for s in sentences:
        if word in s:
            if any(m in s for m in _NEGATION_MARKERS):
                return True
    return False


def check_note(note: dict[str, Any], platform: str = "xhs") -> dict:
    """
    检查一条笔记。返回：
      {"passed": bool, "violations": [...], "warnings": [...], "checked_at": ...}

    platform 参数说明：
      · "xhs"（默认）小红书：Markdown 标记会被原样显示 → 规则10b 生效
      · "douyin" 抖音文章：同样不渲染 Markdown → 规则10b 生效
      · "tgb" 淘股吧论坛：纯文本编辑器，**不渲染 Markdown**；
        但方法论内容天然要讲"如何避免预测走势"，
        规则1 的 BANNED_SIGNAL 需要放宽（避免误杀"避免预测走势"这类正文）
    """
    title = note.get("title", "") or ""
    body = note.get("body", "") or ""
    tags = note.get("tags", []) or []
    full = f"{title}\n{body}\n{' '.join(tags)}"

    violations: list[dict] = []
    warnings: list[dict] = []

    # --- 规则 1~5：违禁词 ---
    for level, category, words in RULES:
        hits = _scan(full, words)
        if platform == "tgb" and category == "非法荐股/预测走势":
            # 淘股吧方法论文必然要讨论"如何避免预测走势"这类内容，
            # 关键词会出现在"我不预测涨跌""它不做选股"这类否定句里。
            # → 只保留出现在**否定/边界声明上下文**中的命中则不计违规。
            hits = [h for h in hits
                    if not _in_negation_context(full, h)]
        if hits:
            violations.append({
                "level": level,
                "category": category,
                "hits": hits[:8],
                "advice": f"删除或改写：{'、'.join(hits[:8])}",
            })

    # --- 规则 6：联系方式 / 二维码 / 域名 ---
    contact_hits = []
    for rx in RE_CONTACT:
        m = rx.search(full)
        if m:
            contact_hits.append(m.group(0))
    if contact_hits:
        violations.append({
            "level": ERROR,
            "category": "违规导流（联系方式）",
            "hits": contact_hits[:5],
            "advice": "笔记正文/图片/评论/私信一律不得出现任何联系方式或链接",
        })

    # --- 规则 7：股票代码 ---
    codes = RE_STOCK_CODE.findall(full)
    if codes:
        violations.append({
            "level": ERROR,
            "category": "非法荐股（具体股票代码）",
            "hits": codes[:5],
            "advice": "具体标的必须匿名化（如「某消费龙头」），且不得给出买卖判断",
        })

    # --- 规则 8：绩效数字必须带口径（公约第 6 类硬要求） ---
    perf = RE_PERF.findall(full)
    if perf:
        has_caveat = any(k in full for k in
                          ["模拟", "历史", "区间", "样本", "不含", "非实盘", "不代表未来", "口径", "回测"])
        if not has_caveat:
            violations.append({
                "level": ERROR,
                "category": "误导性信息（绩效数字缺口径）",
                "hits": perf[:5],
                "advice": "出现胜率/收益/回撤等数字时，必须同屏说明【时间区间】【样本笔数】"
                          "【成本假设】【模拟盘非实盘】四要素",
            })

    # --- 规则 8b：未填的真实素材槽位 ---
    # 正文里仍残留占位符说明该槽位没被真实素材填充。
    # 若直接发布，读者会看到 "数据区间：[[区间]]" 这种半成品，属于不实/误导信息。
    # 兼容两种写法：[[...]]（正文模板实际使用）与 {{...}}（人工草稿常见）。
    unfilled = re.findall(r"\[\[(.+?)\]\]", body) + re.findall(r"\{\{(.+?)\}\}", body)
    if unfilled:
        violations.append({
            "level": ERROR,
            "category": "真实素材槽位未填写",
            "hits": unfilled[:5],
            "advice": "正文仍有未填的占位符。请在 content/facts/<选题id>.json "
                      "补齐真实素材后重新生成，AI 不得编造任何数字",
        })

    # --- 规则 9：AI 标注声明 ---
    if not note.get("ai_declared", True):
        violations.append({
            "level": ERROR,
            "category": "AI 生成内容未标识",
            "hits": ["未勾选【笔记含 AI 合成内容】"],
            "advice": "平台 2026-02 起要求 AI 参与生成的内容必须主动标识，未标识将被限制分发",
        })

    # --- 规则 10：金融内容必须做风险提示 ---
    finance_triggers = ["复盘", "回测", "策略", "交易", "持仓", "收益", "止损", "量化", "投资"]
    if any(t in full for t in finance_triggers):
        has_disclaimer = any(k in full for k in
                              ["不构成投资建议", "不构成任何投资建议", "不作为投资建议",
                               "仅供学习", "非投资建议", "不构成任何",
                               "风险自担", "盈亏自负"])
        if not has_disclaimer:
            warnings.append({
                "level": WARN,
                "category": "缺少风险提示",
                "advice": "公约鼓励并要求金融内容做好风险提示，建议在正文末尾加"
                          "「本文仅为个人记录与方法分享，不构成投资建议」",
            })

    # --- 规则 10b：正文残留 Markdown 标记 ---
    # 小红书不渲染 Markdown，`**加粗**` 会原样显示成字面星号，
    # 读者看到的是「我后来加了一条硬规则：**任何一段结论…**」。
    # 2026-11-09 复现：11-04 清理过，11-06 新增的 sample 模板又带回来了。
    # 根治办法不是再手工删一次，而是把它变成硬闸门 —— 漏了就发不出去。
    #
    # ⚠️ 淘股吧例外：论坛编辑器是富文本，但正文里**用 Markdown 标记会被当纯文本显示**，
    #    同样要拦。不过淘股吧方法论文里我们会用【小标题】这种中文括号标注，
    #    那是可接受的（不匹配下面的正则）。
    md_hits = []
    if platform != "tgb":
        for rx in (r"\*\*", r"(?<!\w)#{1,6}\s", r"(?<!\w)>\s", r"```", r"\[[^\]]+\]\([^)]+\)"):
            m = re.search(rx, body)
            if m:
                md_hits.append(m.group(0).strip())
    if md_hits:
        violations.append({
            "level": ERROR,
            "category": "正文残留 Markdown 标记",
            "hits": md_hits[:5],
            "advice": "小红书不渲染 Markdown，标记会以字面星号显示。"
                      "请去掉 **粗体** / # 标题 / > 引用 / ```代码块 / [链接](url)",
        })

    # --- 规则 11：极限词（广告法） ---
    extreme = [w for w in ["最好", "最强", "第一名", "100%", "彻底根治", "永久有效", "国家级", "唯一"] if w in full]
    if extreme:
        warnings.append({
            "level": WARN,
            "category": "极限词",
            "advice": f"疑似广告法极限词：{'、'.join(extreme)}，建议替换",
        })

    # --- 规则 12：标题长度与关键词前置 ---
    if len(title) > 20:
        warnings.append({
            "level": WARN,
            "category": "标题超长",
            "advice": f"标题 {len(title)} 字，建议 ≤20 字（小红书标题上限含标点）",
        })

    # --- 规则 13：正文长度 ---
    body_len = len(re.sub(r"\s", "", body))
    if body_len < 100:
        warnings.append({
            "level": WARN,
            "category": "正文过短",
            "advice": f"正文仅 {body_len} 字，建议 300-800 字（干货类 ≥600 字推荐量更高）",
        })
    if body_len > 1000:
        warnings.append({
            "level": WARN,
            "category": "正文超长",
            "advice": f"正文 {body_len} 字，普通图文笔记上限 1000 字，请精简",
        })

    # --- 规则 14：标签数量 ---
    if not (3 <= len(tags) <= 10):
        warnings.append({
            "level": WARN,
            "category": "标签数量异常",
            "advice": f"当前 {len(tags)} 个标签，建议 5-8 个（1 大核心 + 3-5 长尾 + 1-2 相关）",
        })

    # --- 规则 15：图片数量 ---
    images = note.get("images", []) or []
    if images and not (6 <= len(images) <= 9):
        warnings.append({
            "level": WARN,
            "category": "配图数量偏离最优区间",
            "advice": f"当前 {len(images)} 张，最优 6-9 张（1-18 张为平台上限）",
        })

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
        "title": title,
        "checked_fields": {"title_len": len(title), "body_len": body_len, "tag_count": len(tags),
                           "image_count": len(images)},
    }


def assert_compliant(note: dict[str, Any]) -> None:
    r = check_note(note)
    if not r["passed"]:
        lines = ["发布被合规闸门拦截："]
        for v in r["violations"]:
            lines.append(f"  [{v['level']}] {v['category']}：{'、'.join(map(str, v['hits']))}")
            lines.append(f"      → {v['advice']}")
        raise ComplianceError("\n".join(lines))
