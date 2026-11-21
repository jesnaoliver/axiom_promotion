"""
抖音专属合规闸门。

【为什么必须有独立的一份，而不是复用小红书的 compliance.py】

抖音的违规面和小红书**不完全重合**，有三类是抖音独有或更严的：

1. **AI 工具类违规**
   2025-03-10 抖音安全中心《打击"非法荐股"等违法证券活动》公告的**三个案例全部是 AI 工具场景**：
   - 用 AI 工具分析某股票有上升潜力，并给出止盈/止损价位
   - 宣称"炒股4个月赚230万""12天盈利32%"
   - "AI炒股神器"课程引流 + 通过昵称/头像/签名/评论引导脱离抖音
   我们的产品形态（AI + 行情 + 回测）**天然就在这个射程内**，
   所以必须有一道专门针对"AI 荐股语义"的检测。

2. **AI 批量起号**
   2026-10-28《关于治理恶意营销号滥用AI的公告》五类行为，
   第五类即"借助AI工具低成本复制内容批量起号变现"，
   近一个月处置违规短视频 113.5 万条、账号 50.7 万个。
   → 需要检测"内容高度同质化"这个特征。

3. **模糊话术引导私信**
   抖音电商《【诱导第三方】处置细则》明确：使用模糊话术引导消费者私信沟通
   本身即构成"诱导第三方"，**即使没说任何联系方式**。
   小红书版没有这条。

另外抖音严打**变体规避**：2026-09-16 公告明确点名违规账号使用
"藏头诗、藏尾诗"等变体规避审核。
→ 检测层要能识别这类变体（首字/尾字拼接的敏感词）。

【与小红书版的分层关系】
  第一层  content/compliance.py        通用红线（两平台都适用）
  第二层  content/douyin_compliance.py 本文件，抖音专属加严
  第三层  source/sanitize.py           数据层脱敏（把标的剥离）
  第四层  source/sanitize.forbidden_check  项目专属（域名/品牌/代码）
任一层不过 → 中止发布。
"""

import re

# ---------------------------------------------------------------------------
# 抖音独有的 AI 荐股语义
# ---------------------------------------------------------------------------

# 直接的 AI 荐股话术
AI_RECOMMEND_PATTERNS = [
    (r"AI\s*(选股|荐股|荐股器|选股器|炒股神器|炒股助手)", "AI荐股/选股话术"),
    (r"智能选股|自动选股|量化选股", "智能选股话术"),
    (r"AI\s*抓.{0,4}(妖股|涨停|牛股)", "AI抓妖股"),
    (r"(智能体|机器人|助手).{0,8}(告诉你|推荐|建议).{0,6}(买|卖|入场|离场)", "智能体给出买卖建议"),
    (r"(帮我|让它|用它).{0,6}(分析|看).{0,10}(走势|行情).{0,12}(涨|跌|买点|卖点)", "让AI判断涨跌"),
]

# 收益/胜率承诺（抖音红线更明确）
RETURN_PATTERNS = [
    (r"\d+\s*天.{0,4}(盈利|收益|赚)", "N天收益"),
    (r"(盈利|收益|赚)\s*\d+\s*%", "收益率数字"),
    (r"胜率\s*\d+", "胜率数字"),
    (r"年化\s*\d+", "年化收益"),
    (r"(翻倍|暴涨|涨停).{0,6}(可|能|稳)", "收益承诺"),
    (r"(稳定|持续)(盈利|收益|赚钱)", "稳定盈利承诺"),
    (r"(保证|保底|稳赚|包赚|无风险).{0,6}(收益|盈利|赚)", "保本承诺"),
    (r"(回测|实盘|模拟).{0,6}(收益率|胜率).{0,8}\d", "回测业绩展示"),
]

# 晒收益截图（抖音 2026-09-16 明确列为伪装话术）
PROFIT_SCREENSHOT = [
    (r"(晒|展示|见图|附上).{0,6}(收益|盈利|利润|战绩|战果)", "晒收益"),
    (r"(我的|账户).{0,4}(收益|盈利|赚了)", "个人收益展示"),
    (r"(累计|今年|本月).{0,4}(赚|盈利)\s*\d", "累计收益"),
]

# 变体规避：藏头/藏尾（把敏感字拆开藏）
HIDDEN_WORD = re.compile(r"[\u4e00-\u9fa5]{0,2}(?:推|荐|买|卖|涨|跌|赚|亏|股|票)"
                          r"[\u4e00-\u9fa5]{0,2}")

# 模糊话术引导私信（抖音特有：小红书没有这条）
VAGUE_DM_PATTERNS = [
    (r"(私信|扣1|扣个1|滴我|dd我|私我).{0,8}(领取|拿|要|发|了解|咨询)", "模糊话术引导私信"),
    (r"(评论区|留言|公屏).{0,8}(领取|拿|发|私)", "评论区引导私拿"),
    (r"(懂的都懂|懂的来|有需要的).{0,10}(私信|留言)", "暗示式引导"),
    (r"(详情|完整版|更多).{0,8}(看主页|见主页|主页有)", "引导看主页"),
]

# 站外导流（抖音有个签+评论区+私信全场景 AI 风控，比小红书更严）
TRAFFIC_PATTERNS = [
    (r"https?://|www\.", "URL"),
    (r"\b[a-z0-9-]+\.(cn|com|net|org|cc|xyz|top|vip)\b", "域名"),
    (r"(微信|weixin|wx|威信|VX|威信|green|绿泡泡|扣扣|QQ)\s*[:：]?\s*[a-zA-Z0-9_-]{4,}", "联系方式"),
    (r"(公众号|小程序|某平台|某站|其他平台).{0,6}(搜|搜索|看|找)", "引导去其他平台"),
    (r"扫码|二维码|扫一扫", "二维码引导"),
    (r"(私域|站外|加群|进群|拉群).{0,6}(聊|交流|领取|咨询)", "私域引导"),
]

# 知识星球/研报二次分发
# 注意：讲"怎么读研报/怎么分类/研报的法规要求"是**方法论科普**，完全安全。
# 只有**转述研报内容本身**才违规。所以正则必须精确区分。
SECONDARY_DIST = [
    (r"知识星球|zsxq|付费社群|星球成员", "付费社群二次分发"),
    # 转述研报内容：研报 + 具体的结论性内容
    (r"(研报|券商研报|投行研报|研究报告).{0,4}(原文|全文)", "研报原文"),
    (r"(研报|券商研报).{0,4}(摘要|要点).{0,20}(上涨|下跌|买入|增持|目标价|评级|预测)", "研报观点转述"),
    (r"(转述|摘录|搬运|分享).{0,6}(研报|研报内容)", "研报转述"),
    (r"(目标价|评级|盈利预测|上调评级|下调评级)", "研报评级/目标价"),
    (r"(研报|券商).{0,6}(建议|推荐).{0,6}(买入|增持|配置)", "研报投资建议"),
]

# AI 批量起号（2026-10-28 公告第五类）
BATCH_CONTENT = [
    (r"关注我.{0,6}(每日|每天).{0,6}(更新|分享|发布)", "每日更新模板话术"),
    (r"(第\s*\d+\s*天|day\s*\d+).{0,10}(复盘|系统|日志)", "连载编号模板"),
]


def check_douyin(note: dict) -> dict:
    """
    抖音合规检查。

    note 需含：title / body / tags / images（images 为可选）
    返回与 compliance.check_note 同构的 dict，便于上层统一处理。
    """
    title = note.get("title", "") or ""
    body = note.get("body", "") or ""
    tags = " ".join(note.get("tags", []) or [])
    images = note.get("images", []) or []
    text = f"{title}\n{body}\n{tags}"

    violations = []
    warnings = []

    def hit(cat, pattern_desc, matched, where="body"):
        violations.append({
            "category": cat,
            "hits": [matched],
            "where": where,
            "platform": "douyin",
        })

    # ---- 1. AI 荐股语义 ----
    for pat, desc in AI_RECOMMEND_PATTERNS:
        m = re.search(pat, text)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 2. 收益/胜率承诺 ----
    for pat, desc in RETURN_PATTERNS:
        m = re.search(pat, text)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 3. 晒收益截图 ----
    for pat, desc in PROFIT_SCREENSHOT:
        m = re.search(pat, text)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 4. 模糊话术引导私信（抖音特有）----
    for pat, desc in VAGUE_DM_PATTERNS:
        m = re.search(pat, text)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 5. 站外导流 ----
    for pat, desc in TRAFFIC_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 6. 二次分发 ----
    for pat, desc in SECONDARY_DIST:
        m = re.search(pat, text)
        if m:
            hit(desc, pat, m.group(0))

    # ---- 7. AI 批量起号特征 ----
    for pat, desc in BATCH_CONTENT:
        m = re.search(pat, text)
        if m:
            warnings.append({
                "category": desc,
                "detail": "抖音2026-10-28公告严打AI批量起号，注意内容差异化",
                "platform": "douyin",
            })

    # ---- 8. 变体规避检测（藏头诗/藏尾诗）----
    # 抖音 2026-09-16 公告明确点名违规账号用藏头诗、藏尾诗规避审核。
    #
    # 关键设计：**检测行为特征，不检测具体词**。
    # 因为藏头诗能拼出的敏感词是无限的（"推荐不要乱买"/"荐股是骗人的"…），
    # 词表永远补不全。
    # 行为特征才是稳定的：
    #   ① 多行短句（每行 2-8 字）
    #   ② 行内字符间距大（"推 荐 不 要 乱 买"这种拆字写法）
    #   ③ 首字或尾字连读能读出一个完整语义
    # 命中 ①+② 或 ①+③ → 疑似藏头/藏尾，拦截。
    #
    # ⚠️ 实测误伤（2026-11-09）：正文里的**数据清单**会被当成藏头诗。
    #   典型正文片段（合法，来自 AXIOM 真实分类计数）：
    #       · NEWS 4 条
    #       · MACRO 1 条
    #       · IDEA 1 条
    #   这三行每行 2-8 字 → 命中①；行内有空格 → 命中② → 被误判成"藏头诗"。
    #   真实拆字藏头的特征是**汉字之间**插空格（"推 荐 不 要"），
    #   而数据清单的空格是**汉字与数字/英文之间**的分隔（"· NEWS 4 条"）。
    #   → 用「汉字间空格」判定拆字，并排除列表行，误伤消除、检测力不变。
    LIST_MARKER = ("·", "•", "-", "*", ">", "|", "—")
    _CJK = r"[\u4e00-\u9fff]"

    def _is_list_line(l: str) -> bool:
        """列表/项目符号行 —— 数据清单，不是诗句。"""
        s = l.lstrip()
        if not s:
            return False
        if s[0] in LIST_MARKER:
            return True
        # "1. " / "1) " / "① " 这类编号
        return bool(re.match(r"^(\d+[.、)]|\d+\s|[①-⑳])", s))

    def _split_cjk(l: str) -> int:
        """汉字之间被空格拆开的次数 —— 真正的"拆字"写法特征。"""
        return len(re.findall(_CJK + r"[ \t]+" + _CJK, l))

    lines = [l.strip() for l in (body or "").split("\n") if l.strip()]
    if len(lines) >= 3:
        # 只把**非列表行**当诗句候选
        poem_lines = [l for l in lines if not _is_list_line(l)]
        short_lines = [l for l in poem_lines if 2 <= len(l.replace(" ", "")) <= 8]
        # 拆字：汉字之间有空格（而不是"汉字 数字 英文"的分隔空格）
        spaced = [l for l in poem_lines if _split_cjk(l) >= 2]
        heads = "".join(l.replace(" ", "")[0] for l in poem_lines if l.replace(" ", ""))
        tails = "".join(l.replace(" ", "")[-1] for l in poem_lines if l.replace(" ", ""))
        # 首尾连读要能构成有意义的短语（≥4字且含常见动词/否定词）
        semantic_markers = ("买", "卖", "荐", "推", "涨", "跌", "赚", "亏", "股",
                            "票", "仓", "位", "骗", "别", "快", "上")
        head_ok = len(heads) >= 4 and sum(heads.count(c) for c in semantic_markers) >= 2
        tail_ok = len(tails) >= 4 and sum(tails.count(c) for c in semantic_markers) >= 2

        if len(short_lines) >= 3 and (spaced or head_ok or tail_ok):
            violations.append({
                "category": "藏头/藏尾诗变体规避审核",
                "hits": [f"疑似拆字藏头/藏尾：首字连读「{heads[:10]}」 尾字连读「{tails[:10]}」"],
                "where": "body",
                "platform": "douyin",
            })

    # ---- 9. 结构性检查 ----
    title_len = len(title)
    if title_len > 30:
        violations.append({
            "category": "标题超长",
            "hits": [f"{title_len}字，抖音文章标题上限30字"],
            "where": "title",
            "platform": "douyin",
        })
    if title_len < 5:
        warnings.append({
            "category": "标题过短",
            "detail": f"仅{title_len}字，抖音信息流推荐权重受影响",
            "platform": "douyin",
        })

    body_len = len(body)
    if body_len > 8000:
        violations.append({
            "category": "正文超出文章上限",
            "hits": [f"{body_len}字，抖音文章上限8000字"],
            "where": "body",
            "platform": "douyin",
        })
    if body_len < 300:
        warnings.append({
            "category": "正文过短",
            "detail": f"仅{body_len}字，抖音文章不低于300字",
            "platform": "douyin",
        })

    tag_count = len(note.get("tags", []) or [])
    if tag_count > 5:
        warnings.append({
            "category": "话题数量偏多",
            "detail": f"{tag_count}个，建议3-5个",
            "platform": "douyin",
        })

    if not images:
        warnings.append({
            "category": "缺少配图",
            "detail": "抖音文章强烈建议配头图+封面",
            "platform": "douyin",
        })
    elif len(images) > 30:
        violations.append({
            "category": "配图超上限",
            "hits": [f"{len(images)}张，抖音文章上限30张"],
            "where": "images",
            "platform": "douyin",
        })

    # ---- 10. AI 标识提醒（不阻断，但必须提示）----
    warnings.append({
        "category": "需勾选AI内容标识",
        "detail": "抖音《内容标识使用规范》要求AI生成内容勾选「含有AI生成内容」，"
                  "网信办6类必选标签为发布必经环节",
        "platform": "douyin",
    })

    # ---- 11. 金融免责提示 ----
    if "不构成投资建议" not in body and "不构成任何投资建议" not in body:
        warnings.append({
            "category": "缺少投资风险提示",
            "detail": "抖音《财经行业公约》2.3 要求金融内容显著位置提示风险",
            "platform": "douyin",
        })

    return {
        "platform": "douyin",
        "passed": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
        "checked_fields": {
            "title_len": title_len,
            "body_len": body_len,
            "tag_count": tag_count,
            "image_count": len(images),
        },
    }


def check_title_style(title: str) -> list[str]:
    """
    抖音标题风格检查（返回建议，不是违规）。

    抖音与小红书的标题逻辑不同：
      小红书：偏搜索（把关键词放前面，用户主动搜）
      抖音：偏推荐流（"问题 + 损失"更能勾住，前10字权重最高）
    """
    advice = []
    t = title.strip()

    if len(t) > 20:
        advice.append(f"标题{len(t)}字，建议压到20字内（抖音信息流只显示前10字左右）")

    # 结论式开头（"我/如何/怎样"）在抖音偏弱
    if re.match(r"^(我|如何|怎样|为什么|分享)", t):
        advice.append("抖音标题偏「问题+损失」，比「我做了什么」更抓人")

    # 缺少具体性
    if not re.search(r"[0-9一二三四五六七八九十]|%|倍|天|次|个|步", t):
        advice.append("建议加入具体数字（几天/几步/多少次），抖音标题带数字点击率更高")

    return advice


if __name__ == "__main__":
    print("=" * 66)
    print("抖音合规闸门自测")
    print("=" * 66)

    cases = [
        ("正常方法论",
         {"title": "看不到样本量的结论，我一律不采信",
          "body": "我给自己的投研流程接了个日报模块。它每段都标了置信度。"
                  "样本不足时会明确说「不下结论」。这套机制我用了几个月，"
                  "最大的变化是我终于知道自己哪天判断是有底的。以上只是我自己的"
                  "记录和方法分享，不构成任何投资建议，市场有风险，决策请自己判断。",
          "tags": ["AI工具", "交易复盘"], "images": ["a.jpg"]}),

        ("AI荐股话术",
         {"title": "我用AI选股助手抓到了大妖股",
          "body": "我的AI选股神器12天盈利32%，胜率85%，这个工具真的能帮你翻倍。",
          "tags": ["AI工具"], "images": ["a.jpg"]}),

        ("晒收益",
         {"title": "分享我今年的战绩",
          "body": "附上我的收益截图，今年累计赚了30万，太香了。",
          "tags": [], "images": ["a.jpg"]}),

        ("模糊私信话术",
         {"title": "完整版工具包",
          "body": "需要的私信我，评论区扣1我把资料发你。",
          "tags": [], "images": ["a.jpg"]}),

        ("站外导流",
         {"title": "我的交易系统",
          "body": "详情看主页，也可以加微信 efficientinvest 交流。",
          "tags": [], "images": ["a.jpg"]}),

        ("星球二次分发",
         {"title": "知识星球精选",
          "body": "今天星球里的研报原文，券商研报的目标价和盈利预测都在这。",
          "tags": [], "images": ["a.jpg"]}),

        ("藏头诗",
         {"title": "系统搭建日志",
          "body": "推 荐 不 要 乱 买\n荐 股 是 骗 人 的\n买 入 前 先 看 仓 位",
          "tags": [], "images": ["a.jpg"]}),
    ]

    for name, note in cases:
        r = check_douyin(note)
        mark = "✅ 通过" if r["passed"] else "⛔ 拦截"
        print(f"\n{mark}  {name}")
        for v in r["violations"]:
            print(f"      ⛔ {v['category']}: {v['hits']}")
        for w in r["warnings"]:
            print(f"      ⚠️  {w['category']}")
        if r["passed"]:
            for a in check_title_style(note["title"]):
                print(f"      💡 {a}")

    print("\n" + "=" * 66)
