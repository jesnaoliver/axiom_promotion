"""
Axiom 情报精选 —— 按板块检索 + 核心观点分级提取。

【接口实测结论（2026-11-17）】
- 列表：`GET /v1/intelligence?page=N&pageSize=10&sortBy=postDate&sortOrder=DESC`
  ⚠️ **pageSize 上限是 10**，给 200 会返回 0 条
  ⚠️ **没有任何服务端搜索参数**（keyword/search/title/sector/industry/plate/category 全部无效，
     返回 0 条）→ 必须**拉全量后客户端筛选**
- 全量：约 600 条（60 页）
- 详情：`GET /v1/intelligence/{id}` —— 返回更多字段（含 coreView 完整版）

【记录结构（关键）】
{
  "id", "title", "summary",
  "postDate": "2026-11-16",
  "categoryL1": "NEWS",  "categoryL2": "商业航天-火箭发射",
  "swIndustryTag": ["国防军工"],          ← 申万行业标签 = 板块
  "stockMapping": {"mentioned_stocks": [...]},
  "coreView": {                            ← ★ 核心观点
      "content":    "...核心观点句...",
      "premises":  ["前提1", "前提2", ...],  ← 事实层
      "conclusion":"...结论...",
      "deduction_formula": "F3",
      "supporting_mid_ids": [...]
  },
  "originalTextRaw": "## 标题\n**时间**: ...\n![图片](...)"
}

【🔴 分级模型：2026-11-19 重构（用户实测反例驱动）】
─────────────────────────────────────────────────────────
旧模型只有 SAFE / CAUTION / BLOCK 三档，把**大量正常内容误杀**了。
实测反例（11月17日复盘笔记，用户截图）：
  content   "资金调仓与产业基本面扩张并行，市场主线正由泛化交易转向有订单、运行量或渗透率验证的结构性增长领域"
  premise 1 "指数收盘涨跌互现、创业板指跌幅最大且全市场成交缩量，但尾盘多只宽基ETF集中显著放量…"
  premise 2 "晶圆厂收入与产能利用率上升、同期全球半导体设备销售增长预期明确，叠加昇腾云在具身智能企业和高校实现高覆盖…"
  premise 3 "特高压招标总额、交流与直流细分规模均显著增长，2027年直流项目预期加速落地，且跨区外送电量已突破万亿度…"
  conclusion "应转向有订单、运行量或渗透率验证的结构性增长主线"
→ 旧规则把这条**整条弃用**（categoryL2 含"涨停"→ 记录级拦截），
  并且 premise 1 因含"跌幅"被句子级拦掉 —— 全是误杀。

新模型按「**这句话在做什么**」分四档，而不是按「有没有关键词」：
  ✅ FACT      客观事实陈述 → 可直接引用
               （已发生的数据/事件/产业指标；指数层面涨跌也算事实）
  🔵 METHOD   方法与判据 → 改写语气后可用
               （"筛选标准是订单/运行量/渗透率"这类可验证口径）
  ⚠️ DIRECTION 方向性判断 → 不用（但可作为"我的看法"的素材由我重写）
  ⛔ BLOCK    个股/荐股/行情播报 → 一律不用
               （具体标的+买卖指向、目标价、收益承诺、个股涨跌、涨停板复盘）

【关键判别：指数层面行情 vs 个股层面行情】
  "创业板指跌幅最大"      → FACT（市场结构描述，不是荐股）
  "大港股份涨停"          → BLOCK（个股行情）
  "逾4300只个股下跌"      → FACT（统计事实）
  → 判据：**行情词 + 是否指向可识别的具体标的**
  → 指数/板块/全市场主体 → 放行；个股主体 → 拦截

【最终成文的安全结构】
  ① 话题背景（雪球热门话题的公开描述，不带个股）
  ② 事实层：coreView.premises 中被判定为 FACT 的子句
  ③ 判据层：coreView.conclusion 中被判定为 METHOD 的句（改写为第一人称）
  ④ 我的看法（我自己写的推理 + 能力边界事实陈述）
  ⑤ 风险提示
→ 给方法与事实，不给方向结论与标的。
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from source.axiom_api import AxiomClient  # noqa: E402

ROOT = Path(__file__).parent.parent
CACHE_FILE = ROOT / "assets" / "_axiom_intel_cache.json"

# ------------------------------------------------------------------
# 分级规则
# ------------------------------------------------------------------

# ⛔ 整条弃用：命中即跳过
#
# ⚠️ 2026-11-19 修正：**不再用 categoryL1 匹配中文词**
#    categoryL1 是英文枚举（NEWS/RESEARCH/TRADING/COMPANY/INDUSTRY/MACRO/CONCEPT），
#    中文词永远匹配不上 → 旧规则形同虚设。
#    现在按 categoryL2 + title + content 三个维度判。
BLOCK_CATEGORY_L2 = [
    ("涨停板", "涨停板复盘=盘面播报"),
    ("龙虎榜", "龙虎榜=游资盘口"),
    ("资金龙虎榜", "游资盘口"),
    ("涨停股", "涨停股复盘"),
    ("买卖点", "买卖建议"),
    ("荐股", "荐股"),
    # ⚠️ 2026-11-19 新增：券商研报的「推荐」子类本身就是研报动作
    #   实测 "券商研报-电子行业推荐"（【广发电子】推荐量增升级&新品方向）
    #   它的前提全是研报推荐语（"推荐范围集中于…"）→ 引用价值低且带荐股味
    #   ⚠️ 只拦「推荐」，**不拦「点评/策略」** ——
    #   实测「券商研报-行业点评」有 5 条、「券商研报-公司点评」有 8 条，
    #   全拦会让可用情报骤减。
    ("行业推荐", "研报推荐类"),
    ("公司推荐", "研报推荐类"),
    # ⚠️ 2026-11-19 新增：**公司纪要/调研**类
    #   实测 "拓普集团 董事长、总裁交流总结"（categoryL2=公司交流纪要）
    #   它的前提全部用"公司/国内目标产能"指代同一家公司 ——
    #   **字面上没有公司名，躲过了个股名检查，但实质就是在讲一只票**。
    #   → 整条弃用。
    ("公司交流纪要", "公司纪要=单只股票内容"),
    ("公司调研", "公司调研=单只股票内容"),
    ("公司业绩", "公司业绩=单只股票内容"),
    ("企业纪要", "企业纪要=单只股票内容"),
    ("调研要点", "调研纪要=单只股票内容"),
    ("调研反馈", "调研反馈=单只股票内容"),
    ("董事长", "高管交流=单只股票内容"),
]

# ⛔ 记录级：标题命中即整条弃用
BLOCK_TITLE = [
    r"(涨停|跌停|连板|炸板|封板).{0,8}(复盘|梯队|雁阵|图|一览|名单)",
    r"(目标价|买入评级|增持评级|强烈推荐|首推|金股)",
    r"(关注|做多|做空|买入|卖出).{0,8}(标的|弹性|机会|\d+x)",
    r"^\s*(超跌|低估值|翻倍)(的)?\w{0,6}(股|标的)",
]

# ⛔ 行情播报词（**只在指向个股/细分板块时才拦截**）
#
# ⚠️ 2026-11-19 补全：原先漏了"下跌/上涨/回落/普跌"等常用说法 ——
#   实测 "逾4300只个股下跌" 因为没命中任何行情词，
#   掉到兜底分支 → 被误判为 block（而它其实是市场统计事实）。
QUOTE_WORDS = (r"(涨停|跌停|连板|封板|炸板|涨幅|跌幅|拉升|拉升|回落|反弹|"
               r"上涨|下跌|普涨|普跌|走强|走弱|领涨|领跌|创新高|创新低|"
               r"拉升|回落|反弹|走平)")

# ✅ 市场/指数层面主体 → 行情词放行
#
# ⚠️ 注意：**不能把"个股"两个字直接当主体**
#   "某某个股涨停" 含"个股" → 会被误判成市场层面 → 放行个股行情（危险）
# → 个股统计必须带数字或涨跌谓词。
MARKET_SUBJECTS = (
    r"(指数|创业板指|沪指|深成指|上证|科创板|北证|科创50|沪深300|中证\d+|大盘|"
    r"全市场|两市|成交额|成交量|宽基ETF|ETF|板块|行业|题材|概念|"
    r"逾\d+只个股|\d+只个股|个股(?:涨跌|下跌|上涨|普跌|普涨|同步))"
)

# ✅ 指数/全市场主体（行情词放行用）
#
# ⚠️ 2026-11-19 拆分：原先 MARKET_SUBJECTS 里混了"板块/行业/题材/概念"，
#   结果细分板块的领涨也被放行。
#   实测："光刻胶细分在半导体材料板块反弹时领涨" → 应拦
#         "创业板指跌幅最大且全市场成交缩量"   → 应放行（用户实测反例）
# → 拆成两个集合，各管各的。
INDEX_SUBJECTS = (
    r"(指数|创业板指|沪指|深成指|上证|科创板|北证|科创50|沪深300|中证\d+|"
    r"大盘|全市场|两市|成交额|成交量|宽基ETF|ETF|"
    r"逾\d+只个股|\d+只个股|个股(?:涨跌|下跌|上涨|普跌|普涨|同步))"
)

# ⛔ 细分板块/个股主体（行情词拦截用）
SUB_SECTOR_SUBJECTS = (
    r"(板块|细分|概念|行业|题材|个股|龙头|产业链|材料|设备|芯片|封装|"
    r"模组|产线|白电|黑电|家电|汽车|光伏|储能|锂电|军工|医药|食品|白酒)"
)

# ⛔ 荐股/操作指向
TRADE_DIRECTION_PAT = "|".join([
    r"(买入|卖出|建仓|清仓|加仓|减仓|上车|离场|抄底|逃顶|追高)",
    r"(配置重心|重点配置|超配|低配|增配|减配)",
    r"(优先(选择|关注|配置))",
    r"(目标价|止损价|止盈价|评级|胜率|收益率|年化|回撤)",
    r"(上涨空间|弹性空间|\d+\s*倍\s*(空间|弹性))",
    r"(看好|推荐关注|建议关注|应关注|值得关注)",
    r"(标的|个股)",
    r"(龙头股|妖股|白马股|翻倍股)",
    r"(规避|回避)",
    r"(机会)",
])

# ⚠️ 品牌/公司名黑名单（即使不在 stockMapping 里也不能进正文）
#
# 2026-11-19 实测：stockMapping 只覆盖 A 股上市主体，
# 但 Axiom 的 premises 里会出现**海外品牌/产品名**（昇腾云、openPangu、Optimus…），
# 这些是公司名不是板块词 → 必须拦。
BRAND_NAMES = [
    "昇腾云", "昇腾", "openPangu", "Pangu", "Optimus", "英伟达", "NVIDIA",
    "台积电", "SK海力士", "海力士", "美光", "英特尔", "AMD", "三星",
    "寒武纪", "海光", "阿里", "阿里巴巴", "腾讯", "字节", "百度", "京东",
    "小米", "华为", "特斯拉", "苹果", "微软", "谷歌", "亚马逊", "Meta",
    "OpenAI", "Anthropic", "特斯拉", "百度", "联想", "中芯", "拓普",
    "长电", "通富", "华天", "兆易", "北方华创", "中微", "拓荆", "紫光",
    "中科曙光", "浪潮", "工业富联", "东山", "沪电", "生益", "胜宏",
    "大港股份", "襄阳轴承", "华能蒙电", "美利云", "华工科技", "安泰集团",
    "长江电力", "宁德", "比亚迪", "隆基", "阳光电源", "药明", "恒瑞",
]

# 🔵 METHOD 特征：讲"怎么判断/怎么筛"，不讲"买什么"
METHOD_PAT = (
    r"(验证|筛选|标准|口径|依据|条件|维度|检验|确认|锚定|可证伪|前提)"
)

# ⛔ 明显是操作指引（即使同时含 METHOD 词也要拦）
METHOD_BLOCKER = (
    r"(优先(选择|关注|配置)|配置重心|建仓|买入|卖出|加仓|减仓|超配|低配|"
    r"增配|减配|布局|抄底|标的|龙头|妖股|看好|推荐|规避|回避|机会|"
    r"目标价|评级|收益率|胜率|回撤)"
)

# ⚠️ DIRECTION 特征：祈使/建议句 —— "应该往哪走"
DIRECTIVE_PAT = (
    r"(应|宜|须|需|应当|应该).{0,10}(转向|聚焦|关注|区分|选择|配置|布局|"
    r"回避|规避|持有|减仓|加仓)"
)

# ✅ FACT 特征：客观描述谓词 —— "发生了什么"
DESCRIPTIVE_PAT = (
    r"(增长|下降|上升|回升|回落|扩大|收窄|保持|持平|达到|突破|同比|环比|"
    r"披露|公布|落地|推进|完成|中标|招标|产能|利用率|渗透率|收入|利润|营收|"
    r"出货|订单|交付|缩量|放量|集中|覆盖|扩张|传导|转化|兑现|印证|同步|"
    r"撤离|流出|流入|韧性|平稳|疲软|回升|回暖|高个位数|低个位数|"
    r"亿元|万亿|亿度|亿千瓦|万台|万平方米|百分点)"
)


def _rows_of(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("items", "list", "rows", "records", "data", "content"):
            v = res.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("items", "list", "rows", "records"):
                    v2 = v.get(k2)
                    if isinstance(v2, list):
                        return v2
        for v in res.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def fetch_all(client: AxiomClient | None = None, max_pages: int = 60,
              use_cache: bool = True, max_age_hours: float = 6.0) -> list[dict]:
    """
    拉取情报精选全量（pageSize 上限 10，需分页）。

    ⚠️ 2026-11-19 修正：**缓存必须有时效**
    实测：缓存文件一旦存在就永远被复用 → 11-17 建的缓存到 11-19 仍停在 11-16 的数据，
    导致"用户在界面上搜到的最新情报"和"系统引用的情报"根本不是同一条。
    → 现在超过 max_age_hours 就强制重新拉取。
    """
    if use_cache and CACHE_FILE.exists():
        try:
            age_h = (datetime.now().timestamp()
                     - CACHE_FILE.stat().st_mtime) / 3600.0
            if age_h <= max_age_hours:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            else:
                print(f"⚠️ Axiom 缓存已过期（{age_h:.1f} 小时 > {max_age_hours} 小时），"
                      f"重新拉取…", flush=True)
        except Exception:
            pass

    client = client or AxiomClient()
    if not client.ensure_login():
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        res = client._request(
            "GET",
            f"/v1/intelligence?page={page}&pageSize=10"
            f"&sortBy=postDate&sortOrder=DESC")
        batch = _rows_of(res)
        if not batch:
            break
        new = 0
        for r in batch:
            rid = str(r.get("id"))
            if rid and rid not in seen:
                seen.add(rid)
                rows.append(r)
                new += 1
        if new == 0 and page > 3:
            break
        time.sleep(0.05)

    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return rows


def _score_match(row: dict, keywords: list[str]) -> int:
    """一条情报对关键词的匹配强度。"""
    tags = set(str(t) for t in (row.get("swIndustryTag") or []))
    l2 = str(row.get("categoryL2") or "")
    l1 = str(row.get("categoryL1") or "")
    title = str(row.get("title") or "")
    summary = str(row.get("summary") or "")
    cv = row.get("coreView") or {}
    content = str(cv.get("content") or "")
    premises = " ".join(str(p) for p in (cv.get("premises") or []))

    score = 0
    for kw in keywords:
        if not kw:
            continue
        if kw in tags:
            score += 10
        if kw in l2:
            score += 6
        if kw in l1:
            score += 3
        if kw in title:
            score += 5
        if kw in summary:
            score += 4
        if kw in content:
            score += 3
        if kw in premises:
            score += 3
    return score


def _has_coreview(row: dict) -> bool:
    cv = row.get("coreView") or {}
    return bool(cv.get("premises") or cv.get("content"))


# 「公司名：观点」形态的判定 —— 冒号前那个词是板块/通用词 → 不是公司名
#
# ⚠️ 白名单要覆盖两类：
#   ① 板块/概念词（"面板""免税""面板"…）→ 来自 sector_map 的板块词表
#   ② 通用叙事词（"数据显示""美国股市走势""瑞银""牧业更新"…）
#      → 这些不是公司名，误杀会砍掉可用情报
_GENERIC_TITLE_PREFIX = {
    # 通用叙事开头
    "数据显示", "美国股市", "美股盘面", "一位", "东方", "东方财富", "瑞银",
    "摩根士丹利", "高盛", "南华早报", "早报", "晚报", "盘前", "盘后",
    "复盘", "综述", "回顾", "周报", "月报", "日报", "快讯", "要闻",
    "舆情热度", "事件", "热点", "机会", "策略", "配置", "行业", "市场",
    "机构", "外资", "内资", "公募", "私募", "险资", "社保", "养老金",
    # 板块/领域词（冒号前）
    "面板", "免税", "国产算力", "半导体观点", "牧业", "内蒙储能",
    "西部", "北美", "欧洲", "国内", "海外", "全球",
}


def _looks_like_board_word(name: str) -> bool:
    """这个 2-6 字的中文词是板块/通用词（而非公司名）吗？"""
    if not name:
        return True
    if name in _GENERIC_TITLE_PREFIX:
        return True
    # 板块词表命中（半导体/机器人/免税/面板…）
    try:
        from source.sector_map import SECTOR_KEYWORDS, TAG_DISPLAY

        for kw, _ in SECTOR_KEYWORDS:
            if kw in name or name in kw:
                return True
        if name in TAG_DISPLAY or name in set(TAG_DISPLAY.values()):
            return True
    except Exception:
        pass
    # 含「板块/行业/产业/指数/市场」等通用后缀 → 不是公司名
    if re.search(r"(板块|行业|产业|指数|市场|概念|主题|赛道|领域|板块)$", name):
        return True
    # 「XX数据更新」「XX舆情热度」「XX策略」这类 → 通用
    if re.search(r"(数据|舆情|热度|策略|观点|早报|复盘|综述|点评|分析|跟踪|日报|周报)$",
                 name):
        return True
    return False


def record_blocked(row: dict) -> str | None:
    """
    记录级过滤：整条弃用的返回原因，可用返回 None。

    ⚠️ 2026-11-19 修正：
    旧版拿中文词（"股票交易""涨停"…）去匹配 categoryL1/categoryL2，
    而 categoryL1 实际是英文枚举 → 规则完全失效，
    同时 "市场复盘-强势板块与涨停股" 这种**优质复盘笔记**被误杀。
    → 现在只拦「确实就是盘面播报/荐股」的记录。
    """
    l2 = str(row.get("categoryL2") or "")
    title = str(row.get("title") or "")
    l1 = str(row.get("categoryL1") or "")

    # ⚠️ 2026-11-19 新增：**categoryL1 == "COMPANY" → 整条弃用**
    #   实测漏网样本：
    #     "粤芯半导体：不是第二个中芯…"  (COMPANY/半导体公司分析)
    #     "# 双环传动更新-26.11"          (COMPANY/公司更新-机器人业务进展)
    #   这两条 categoryL2 不含任何封杀词 → 旧规则全放行，
    #   但它们**讲的是单只票**（标题即公司名）→ 引用即等于荐股。
    #   → COMPANY 这个一级分类本身就是「单只股票内容」的标记。
    if l1.upper() == "COMPANY":
        return f"categoryL1=COMPANY（单只股票内容）：{title[:30]}"

    # ⚠️ 纯图片帖子（"图片帖子_19"）—— 有 coreView 但正文是图片，
    #    事实句是系统自动生成的描述，引用价值低且容易跑题
    #    实测 600 条里有 23 条这种 → 直接弃用。
    if re.match(r"^图片帖子", title):
        return "纯图片帖（无正文文本）"

    # ⚠️ 2026-11-19 新增：标题过于笼统（"行业要闻"）
    #   实测「机器人」话题选中了 title="行业要闻" 的记录 ——
    #   它的 coreView 是把多个不相关主题拼在一起（"商业航天与存储芯片事件…"），
    #   引用它等于发一篇拼盘。→ 弃用。
    if title.strip() in ("行业要闻", "行业新闻", "市场要闻", "快讯", "早报",
                         "每日简报", "综合", "其他"):
        return "标题过于笼统（多主题拼盘）"

    # ⚠️ 2026-11-20 新增：占位/无意义标题
    #   实测 "算力芯片" 选中了 title="#出处未知#" 的记录
    #   → 无来源标注的内容不适合作为"公开信息整理"的事实来源
    if re.match(r"^#.{0,8}未知", title) or title.strip() in ("#出处未知#",
                                                             "出处未知", "未知"):
        return "标题为占位符（出处未知）"

    # ⚠️ 2026-11-20 新增：社交/无意义标题
    #   实测候选池里混着 "上午好，打分派点感谢您的支持！"
    #        "📰 高盛中国收盘综述"（无实质内容）
    #        "📑过去 12 小时的一些讨论点"
    #   → 这些不是情报，是社区发言/碎片汇总 → 弃用
    if re.match(r"^(上午好|下午好|晚上好|大家好|感谢|打卡|签到|早安|晚安)", title):
        return "社交发言（非情报内容）"
    if re.search(r"(感谢.{0,6}支持|打分派点|求票|互关|转发抽奖)", title):
        return "社区互动内容（非情报）"
    if re.match(r"^📰\s*\S", title) or title.strip().startswith("📑"):
        return "标题为图标+泛化描述"

    # ⚠️ 2026-11-19 新增：**标题里列了 3+ 个板块的多主题拼盘**
    #   实测 "11月18日复盘笔记：算力租赁/华为算力/半导体/PCB/家电/6G/CPO/汽车整车/冰雪经济等"
    #   → 它是全市场综述，不是任何单一板块的情报
    #   → 引用它写"CPO 帖"会变成"讲全市场的事" → 弃用
    #   判据：标题里用 / 分隔的板块词 ≥ 3 个
    parts = [p for p in re.split(r"[/、,，]", title) if p.strip()]
    if len(parts) >= 4:
        return f"多主题拼盘（标题含 {len(parts)} 个板块）"

    for kw, why in BLOCK_CATEGORY_L2:
        if kw in l2:
            return f"二级分类含「{kw}」：{why}"

    # ⚠️ 2026-11-19 补强：标题里带公司名 → 这条讲的是单只票
    #   实测 "中鼎股份观点更新" —— stockMapping 是空的，
    #   靠 stockMapping 检查完全躲得过去。
    #   （多板块复盘笔记的豁免见下方 is_market_recap 分支）

    # ⚠️ 2026-11-19 新增：**「公司名：观点」标题形态**
    #   实测漏网："重庆啤酒：高分红有持续性，27年关注大宗成本端及销量变化"
    #   （categoryL1=NEWS / categoryL2=未分类 → 前面的规则全放行）
    #   但「冒号前那个词」就是公司名 → 整条讲的是单只票。
    #   判据：冒号前 2-6 个中文，且**不在板块/通用词表**里。
    m = re.match(r"^([一-龥]{2,6})[：:]", title)
    if m:
        name = m.group(1)
        if not _looks_like_board_word(name):
            return f"标题是「公司名：观点」形态（「{name}」）：单只股票内容"

    # ⚠️ 2026-11-19 新增：**多板块复盘笔记豁免**
    #   实测 5 条「X月X日复盘笔记：板块A/板块B/…」全部被"标题含公司名"误杀
    #   —— 标题里列的是**板块名**（华为算力/AI漫剧/AI制药…），不是单只股票。
    #   这些是**多板块横截面**，正是"同一个板块有没有互相印证"最好的素材。
    #   → 命中复盘笔记形态时，跳过公司名检查（其余规则照常）。
    is_market_recap = bool(re.search(r"复盘笔记|市场复盘|板块复盘|每日复盘", title))
    if not is_market_recap:
        comp = title_has_company(title)
        if comp:
            return f"标题含公司名「{comp}」：单只股票内容"

    for pat in BLOCK_TITLE:
        if re.search(pat, title):
            return f"标题命中行情/荐股模式：{title[:30]}"

    # coreView.content 本身就是荐股/操作指引 → 整条弃用
    cv = row.get("coreView") or {}
    content = str(cv.get("content") or "")
    if content and re.search(METHOD_BLOCKER, content) and not re.search(
            r"(验证|筛选|标准|口径)", content):
        return f"核心观点本身是操作指引：{content[:40]}"

    return None


def find_for_sector(rows: list[dict], keywords: list[str],
                    need_coreview: bool = True,
                    skip_blocked: bool = True) -> list[dict]:
    """
    按板块关键词筛选情报，按「匹配强度 + 日期」排序。

    返回带 _score 的记录列表（已过滤掉没有 coreView 的，
    因为用户流程要求"点击最新一条查看，里面有核心观点"）。
    """
    scored = []
    for r in rows:
        if need_coreview and not _has_coreview(r):
            continue
        if skip_blocked:
            why = record_blocked(r)
            if why:
                continue
        s = _score_match(r, keywords)
        if s > 0:
            r = dict(r)
            r["_score"] = s
            scored.append(r)

    # 日期倒序（同日按分数倒序）
    scored.sort(key=lambda r: (str(r.get("postDate") or ""), r["_score"]),
                reverse=True)
    return scored


def sector_relevance(row: dict, sector_name: str,
                     keywords: list[str] | None = None) -> int:
    """
    这条情报**与板块的实际相关度**（0-100）。

    ⚠️ 为什么需要（2026-11-19 实测）：
    按「日期倒序」取最新一条，会拿到**全市场复盘**这种大杂烩，
    它的 coreView 事实全是指数/其他板块的 → 正文跑题。
    真正该用的是「家电出口数据跟踪」这种**纯板块情报**。

    ⚠️ 2026-11-19 修正（**概念词 vs 申万标签必须分开计权**）：
    实测「机器人」话题选中了「【申万家电】11月以来超额显著」——
    原因：`concept_keywords()` 返回的检索词里，**申万一级标签**
    （电子/机械设备）与概念词（机器人/轴承/减速器）**混在一起**，
    而「电子」在任何一篇电子/家电研报正文里都会出现 → 40 分满分 → 跑题。

    → 现在分两档：
      · **概念词**（板块显示名 + 话题概念词）→ 内容命中 40 分（权重最高）
      · **申万标签**（电子/机械设备这类粗粒度行业名）
        → 内容命中只给 10 分，且**不进入**内容匹配
        （只在 swIndustryTag 里命中时给 10 分）
      → 概念词一个都没命中正文 → 相关度封顶 10 分 → 达不到 40 阈值 → 弃用

    评分维度（概念词）：
      · 标题含概念词           → 45 分（主题就是它）
      · 核心观点/摘要含        → 35 分
      · 二级分类含             → 25 分
      · **前提句里顺带提一句**  → 12 分（只是提及，不是主题）
      · 申万标签含             → 10 分
    """
    if not row:
        return 0
    from source.sector_map import TAG_DISPLAY

    # 申万一级标签词表（粗粒度行业名）
    sw_tags = set(TAG_DISPLAY.keys()) | set(TAG_DISPLAY.values())

    all_kws = [sector_name] + list(keywords or [])
    all_kws = [k for k in all_kws if k]
    if not all_kws:
        return 0

    # 概念词 = 不是申万标签的词；板块显示名本身永远是概念词
    # ⚠️ 必须**去重** —— 否则同一个词在 [sector_name] + keywords 里出现两次，
    #    分数直接翻倍（实测 rel(机器人) 被算成 80）。
    concept_kws = []
    for k in all_kws:
        if (k == sector_name or k not in sw_tags) and k not in concept_kws:
            concept_kws.append(k)
    sw_kws = []
    for k in all_kws:
        if k != sector_name and k in sw_tags and k not in sw_kws:
            sw_kws.append(k)

    tags = [str(t) for t in (row.get("swIndustryTag") or [])]
    l2 = str(row.get("categoryL2") or "")
    title = str(row.get("title") or "")
    cv = row.get("coreView") or {}
    content = str(cv.get("content") or "")
    summary = str(row.get("summary") or "")
    premises = " ".join(str(p) for p in (cv.get("premises") or []))
    # ⚠️ 前提要**单独拆出来** ——
    #   实测「机器人」话题选中了【申万家电】那条：
    #   它的前提里有一句"家电企业…向机器人、液冷和半导体等泛科技领域迁移"
    #   → 前提顺带提一句 = 40 分 → 抢在真正的机器人情报前面 → 跑题。
    #   → 前提命中只给 12 分（光是"提及"，不是"主题"）
    content_blob = " ".join([title, content, summary, premises])

    score = 0
    concept_hit = False
    for kw in concept_kws:
        # ★ 标题/核心观点/摘要命中 → 主题就是它（最高权重）
        if kw in title:
            score += 45
            concept_hit = True
        elif kw in content or kw in summary:
            score += 35
            concept_hit = True
        elif kw in l2:
            score += 25
            concept_hit = True
        elif kw in premises:
            # ⚠️ 只在前提里**顺带提一句** → 弱命中
            score += 12
            concept_hit = True
        elif kw in tags:
            score += 10
            concept_hit = True

    # 申万标签只做「沾边」参考，不参与内容匹配（否则任何电子/家电研报都满分）
    for kw in sw_kws:
        if kw in tags:
            score += 10
        elif kw in l2:
            score += 5

    # ⚠️ 概念词一个都没命中 → 这条根本不是讲这个板块的 → 封顶 10 分
    if not concept_hit:
        score = min(score, 10)

    return min(score, 100)


def pick_for_sector(rows: list[dict], sector_name: str,
                    keywords: list[str] | None = None,
                    max_days_back: int = 5) -> dict | None:
    """
    为板块挑**最贴切**的一条情报（而非单纯最新）。

    策略：
      1. 只看最近 max_days_back 天的记录
      2. 按 sector_relevance 排序取最高
      3. 若最高分低于阈值（40），说明最近没有真正相关的 → 返回 None，
         让调用方换一个话题（而不是硬拿一条跑题的）

    ⚠️ 这直接对应用户流程第 5 步「点最新一条查看」——
       但"最新"必须同时**相关**，否则正文会变成全市场综述。
    """
    # 单条不够 → 合并同板块的多条情报，凑足事实
    merged = merge_for_sector(rows, sector_name, keywords, max_days_back)
    return merged


def _pick_one(rows: list[dict], sector_name: str,
              keywords: list[str] | None = None,
              max_days_back: int = 5) -> dict | None:
    """挑单条最贴切的情报。"""
    cand = find_for_sector(rows, keywords or [sector_name])
    if not cand:
        return None

    dates = sorted({str(r.get("postDate") or "") for r in cand}, reverse=True)
    if not dates:
        return None
    newest = dates[0]
    # 只保留与最新日期相差在 max_days_back 天内的
    keep = [r for r in cand
            if _days_between(str(r.get("postDate") or ""), newest) <= max_days_back]
    if not keep:
        keep = cand[:1]

    scored = []
    for r in keep:
        rel = sector_relevance(r, sector_name, keywords)
        if rel > 0:
            scored.append((rel, str(r.get("postDate") or ""), r))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_rel, _, best = scored[0]
    # ⚠️ 2026-11-20：阈值 40 → 45
    #   实测「煤炭」话题：储能类情报靠"电力设备"这个申万标签拿到 10 分，
    #   加上其它字段凑到 40 → 被选中，但正文会讲储能不是煤炭。
    #   → 提高门槛，让"只靠标签沾边"的过不来。
    if best_rel < 45:
        return None
    best = dict(best)
    best["_relevance"] = best_rel
    return best


def merge_for_sector(rows: list[dict], sector_name: str,
                     keywords: list[str] | None = None,
                     max_days_back: int = 5,
                     max_records: int = 4) -> dict | None:
    """
    合并**同板块的多条**情报，把各自的事实凑成一篇更完整的正文。

    ⚠️ 为什么需要（2026-11-19 实测）：
    单条情报的核心观点常常只有 1-2 条事实可用
    （实测"先进封装"选中的那条只剩 1 条），
    帖子正文单薄 → 雪球侧也更容易被判"内容过短"。
    → 合并同板块近期的 2-4 条，取它们**去重后的事实**。

    ⚠️ 合并必须**同细分领域**，否则会跑题：
    实测"先进封装"合并"电子布电话会议""行业要闻"后，
    混进了"电子布供给约束""商业航天与存储芯片"—— 讲的是别的板块。
    → 用 `subtopic_signature` 判断是否同一细分话题。

    合成的记录结构与单条一致，额外带 `_merged_from`。
    """
    cand = find_for_sector(rows, keywords or [sector_name])
    if not cand:
        return None

    dates = sorted({str(r.get("postDate") or "") for r in cand}, reverse=True)
    newest = dates[0]
    keep = [r for r in cand
            if _days_between(str(r.get("postDate") or ""), newest) <= max_days_back]

    # 相关度排序（阈值与 _pick_one 保持一致：45）
    scored = []
    for r in keep:
        rel = sector_relevance(r, sector_name, keywords)
        if rel >= 45:
            scored.append((rel, str(r.get("postDate") or ""), r))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    # ⚠️ 只合并**同一细分领域**的记录
    primary_sig = subtopic_signature(scored[0][2])
    picked = [(rel, dt, r) for rel, dt, r in scored
              if subtopic_signature(r) == primary_sig]
    if len(picked) < 2:
        picked = scored[:max_records]
    picked = picked[:max_records]

    base = dict(picked[0][2])
    merged_facts: list[str] = []
    merged_methods: list[str] = []
    seen = set()

    for _, _, r in picked:
        g = grade_coreview(r)
        for f in g["facts"]:
            key = re.sub(r"[\s，,。.、；;：:]", "", f)[:40]
            if key and key not in seen:
                seen.add(key)
                merged_facts.append(f)
        for m in g["methods"]:
            key = re.sub(r"[\s，,。.、；;：:]", "", m)[:40]
            if key and key not in seen:
                seen.add(key)
                merged_methods.append(m)

    # 只用单条就够时直接返回单条（保持"点最新一条"的语义）
    if len(picked) == 1:
        return base

    base["_merged_from"] = [str(r.get("title"))[:40] for _, _, r in picked]
    base["_merged_relevance"] = [rel for rel, _, _ in picked]
    # 把合并后的事实挂到 coreView 上（下游按 facts 读取）
    cv = dict(base.get("coreView") or {})
    cv["_merged_facts"] = merged_facts
    cv["_merged_methods"] = merged_methods
    base["coreView"] = cv
    base["_relevance"] = picked[0][0]
    return base


def subtopic_signature(row: dict) -> str:
    """
    记录所属的**细分领域**签名（用于判断两条情报是不是在讲同一件事）。

    取 categoryL2 里的细分词（如"光刻机与光刻胶产业链""行业要闻-科技动态"）。
    ⚠️ 用 L2 的**第一段细分名**而不是整个 L2 ——
       "市场复盘-强势板块与涨停股" 和 "市场复盘-板块与个股行情" 都属于"市场复盘"，
       应该能合并；而"券商研报-电子行业点评" 和 "行业数据跟踪-家电出口" 不能。
    """
    l2 = str(row.get("categoryL2") or "")
    if not l2:
        return ""
    head = l2.split("-")[0].strip()
    return head


def sector_fact_coverage(row: dict, sector_name: str,
                          keywords: list[str] | None = None) -> int:
    """
    这条情报的**可用事实**里，有几条是真正讲这个板块的。

    ⚠️ 为什么需要（2026-11-19 实测）：
    「机器人」话题选中「11月19日复盘笔记」—— 它是**多板块横截面**，
    13 条事实里属于机器人的是 **0 条**：
      "山西炼焦煤停产规模持续累积"（煤炭）
      "核心城市二手房价格保持正增长"（房地产）
      "海外变压器交付排产延长至2028年"（电网设备）
    → 正文会变成"用机器人的话题讲煤炭/房地产/电网" → 严重跑题。
    → 选情报时就要看**事实层面的板块覆盖**，而不是整篇文档的相关度。
    """
    if not row:
        return 0
    from source.sector_map import INDUSTRY_NOUNS

    terms = {k for k in [sector_name] + list(keywords or []) if k}
    # 目标板块的行业名词（本身属于目标板块的名词不算"别的行业"）
    own_nouns = {n for n, o in INDUSTRY_NOUNS.items()
                 if o == sector_name or n in terms}

    cnt = 0
    for f in grade_coreview(row).get("facts") or []:
        # 命中目标概念词 → 直接算
        if any(t in f for t in terms):
            cnt += 1
            continue
        # 命中属于目标板块的行业名词 → 算
        if any(n in f for n in own_nouns):
            cnt += 1
            continue
        # 命中别的行业名词 → 不算
        if any(n in f for n in INDUSTRY_NOUNS):
            continue
        # 什么都不命中 → 全市场层面，不算
    return cnt


def pick_for_sector(rows: list[dict], sector_name: str,
                    keywords: list[str] | None = None,
                    max_days_back: int = 5,
                    min_facts: int = 2) -> dict | None:
    """
    为板块挑情报：**优先单条，事实不够才合并多条**。

    策略：
      1. 单条最贴切的 → 若其可用事实数 >= min_facts，直接用
      2. 否则合并同板块近期的 2-4 条，取去重后的事实
      3. 若最近没有真正相关的（相关度 < 40）→ 返回 None，换一个话题

    ⚠️ 对应用户流程第 5 步「点最新一条查看」——
       但"最新"必须同时**相关**，否则正文会变成全市场综述。

    ⚠️ 2026-11-19 三次修正：**按「事实层面的板块覆盖」选情报**
    实测「机器人」选中「11月19日复盘笔记」—— 整篇相关度满分，
    但 13 条事实里属于机器人的是 0 条（其余是煤炭/房地产/电网）。
    → 加一层：候选情报必须**有事实真的落在目标板块**，
      覆盖为 0 的情报直接降级 → 换一个板块/话题。
    """
    best = _pick_one(rows, sector_name, keywords, max_days_back)
    if best:
        g = grade_coreview(best)
        cov = sector_fact_coverage(best, sector_name, keywords)
        if len(g["facts"]) >= min_facts and cov >= 1:
            best["_fact_coverage"] = cov
            return best
    # 单条事实不足 → 合并多条
    merged = merge_for_sector(rows, sector_name, keywords, max_days_back)
    if merged:
        g = grade_coreview(merged)
        cov = sector_fact_coverage(merged, sector_name, keywords)
        if len(g["facts"]) >= min_facts and cov >= 1:
            merged["_fact_coverage"] = cov
            return merged
        # 合并后仍不足 → 至少返回合并结果（比单条多）
        if cov >= 1:
            merged["_fact_coverage"] = cov
            return merged
        # ⚠️ 2026-11-20 修正：合并结果覆盖也是 0 → **返回 None**
        #   实测「机器人」最终选中了【中信制造】ATLAS 那条：
        #   它的 facts=0、cov=0（标题里带"人形机器人"但内容讲芯片架构），
        #   旧逻辑 `return merged if len(g["facts"]) > 0 else best`
        #   → 两条都不合格却还是返回了 best → 正文无事实可用。
        #   → 宁可不发这个板块，也不要发一篇没有事实的帖子。
        if len(g["facts"]) > 0:
            return merged
        return None
    # ⚠️ 同理：单条与合并都不合格 → None（换话题）
    if best:
        g = grade_coreview(best)
        if len(g["facts"]) > 0 and sector_fact_coverage(
                best, sector_name, keywords) >= 1:
            best["_fact_coverage"] = sector_fact_coverage(
                best, sector_name, keywords)
            return best
    return None


def pick_with_coverage(rows: list[dict], sector_name: str,
                       keywords: list[str] | None = None,
                       max_days_back: int = 5,
                       min_facts: int = 2,
                       min_coverage: int = 1) -> dict | None:
    """
    严格版选情报：**必须**有 ≥min_coverage 条事实真的落在目标板块。

    pick_for_sector 是"宽松"版（找不到好的一定会返回次优的），
    本函数是"严格"版 —— 找不到就返回 None，让调用方换话题。
    → 用于发帖：宁可换话题，也不要拿一篇讲煤炭的复盘去写机器人。
    """
    best = _pick_one(rows, sector_name, keywords, max_days_back)
    if best:
        g = grade_coreview(best)
        cov = sector_fact_coverage(best, sector_name, keywords)
        if len(g["facts"]) >= min_facts and cov >= min_coverage:
            best["_fact_coverage"] = cov
            return best

    merged = merge_for_sector(rows, sector_name, keywords, max_days_back)
    if merged:
        g = grade_coreview(merged)
        cov = sector_fact_coverage(merged, sector_name, keywords)
        if len(g["facts"]) >= min_facts and cov >= min_coverage:
            merged["_fact_coverage"] = cov
            return merged

    # ⚠️ 兜底：在全部候选里找覆盖最高的（哪怕只有 1 条）
    cand = find_for_sector(rows, list(keywords or [sector_name]))
    best_cov, best_row = 0, None
    for r in cand:
        cov = sector_fact_coverage(r, sector_name, keywords)
        if cov > best_cov:
            best_cov, best_row = cov, r
    if best_row is not None and best_cov >= min_coverage:
        # ⚠️ 2026-11-20 修正：兜底也**必须检查事实条数**
        #   实测「PCB」话题兜底选中了【申万家电】那条：
        #   它的前提里有"…布局向机器人、液冷和半导体等泛科技领域迁移"
        #   → 含"电子"→ 覆盖数达标 → 但可用事实是 0 条
        #   → 正文会变成"讲 PCB 但没有一条 PCB 事实"的空帖。
        #   → 事实数不够 → 宁可返回 None（换话题）。
        g = grade_coreview(best_row)
        if len(g["facts"]) >= min_facts:
            best_row = dict(best_row)
            best_row["_fact_coverage"] = best_cov
            return best_row
    return None


def _days_between(d1: str, d2: str) -> int:
    """两个 YYYY-MM-DD 相差几天（解析失败返回 999）。"""
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return abs((a - b).days)
    except Exception:
        return 999


def pick_latest(rows: list[dict], keywords: list[str],
                need_coreview: bool = True) -> dict | None:
    """取匹配到的最新一条。"""
    cand = find_for_sector(rows, keywords, need_coreview)
    return cand[0] if cand else None


# ------------------------------------------------------------------
# 句子级分级
# ------------------------------------------------------------------

# 子句切分符（保留标点信息用不到，直接切）
CLAUSE_SPLIT = re.compile(r"[，；;。]|(?<=[）)])")


def split_clauses(text: str, min_len: int = 6) -> list[str]:
    """
    把长句拆成子句 —— 这是本次重构的关键。

    旧版按整句判定，导致：
      "指数收盘涨跌互现、创业板指跌幅最大且全市场成交缩量，但尾盘多只宽基ETF集中显著放量"
    整句因为一个"跌幅"被废掉，而里面"全市场成交缩量""宽基ETF放量"都是有价值的事实。
    → 拆开后逐句判定，能救回大部分有价值内容。
    """
    if not text:
        return []
    parts = [p.strip() for p in CLAUSE_SPLIT.split(str(text)) if p and p.strip()]
    out = []
    for p in parts:
        if len(p) >= min_len:
            out.append(p)
        elif out and p:
            # 太短的片段并回上一句
            out[-1] = out[-1] + p
    return out


def mentioned_stock_names(row: dict) -> list[str]:
    """
    取本条情报关联的个股名（stockMapping.mentioned_stocks）。

    ⚠️ 2026-11-19 补强：stockMapping **覆盖不全**
    实测 "中鼎股份观点更新" 这条，stockMapping 是空的，
    但标题里的"中鼎股份"就是个股名 → 必须从**标题**里也提取。
    → 标题里出现「XX股份/XX科技/XX集团」等公司形态词 → 整条弃用。
    """
    out = []
    sm = row.get("stockMapping") or {}
    for s in (sm.get("mentioned_stocks") or []):
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict):
            n = s.get("name")
            if n:
                out.append(str(n))
    return [n for n in out if n]


def title_has_company(title: str) -> str | None:
    """
    标题里是否含具体公司名（"中鼎股份观点更新" → "中鼎股份"）。
    返回公司名或 None。
    """
    if not title:
        return None
    # 先看已知黑名单
    from source.sanitize import BRAND_COMPANIES, KNOWN_COMPANIES
    for n in sorted(set(BRAND_COMPANIES) | set(KNOWN_COMPANIES), key=len,
                     reverse=True):
        if n in title:
            return n
    # 股票代码（任意括号 + 6 位数字，或带 .SH/.SZ 后缀）
    #   实测 "花旗——思源电气（002028.SZ）——拜访上海总部"
    #   和 "大摩——潍柴动力（000338）——管理层交流纪要"
    if re.search(r"[（(]\s*\d{6}(?:\.(?:SH|SZ|BJ|HK))?\s*[)）]", title):
        return "标题含股票代码"
    if re.search(r"\b\d{6}\.(?:SH|SZ|BJ|HK)\b", title, re.IGNORECASE):
        return "标题含股票代码"
    # 公司形态词
    # ⚠️ 2026-11-19 补强：H股/美股后缀
    #   "中国重汽H推荐" —— "重汽"后面跟的是英文字母 H，
    #   中文公司后缀正则抓不到 → 漏放。
    m = re.search(r"[一-龥]{2,6}[A-Z]?(?:股份|集团|控股|科技|电子|电器|通信|"
                  r"证券|银行|保险|药业|生物|能源|电力|装备|机电|材料|"
                  r"重工|精密|光电|智能|软件|传媒|环保|食品|乳业|动力|"
                  r"重汽|重机|电气|石化|矿业|建设|交运|航空|港口)", title)
    if m:
        return m.group(0)
    # 纯中文公司名 + 单个字母后缀（"中国重汽H"）
    m1 = re.search(r"[一-龥]{3,8}[A-Z](?![a-zA-Z])", title)
    if m1 and m1.group(0) not in ("A股", "H股", "AI", "IPO", "ETF", "LPR",
                                  "GDP", "CPI", "PPI", "PMI", "REITs"):
        return m1.group(0)
    # ⚠️ 外文缩写 + 中文（"TCL中环"）
    #   没有中文公司后缀 → 上面的正则抓不到
    #   判据：标题里出现连续大写英文缩写（≥2 个大写字母）紧跟中文
    m2 = re.search(r"[A-Z]{2,}[一-龥]{2,4}", title)
    if m2:
        return m2.group(0)
    # ⚠️ 「XX交流要点/调研纪要」类标题 → 必然是单只股票
    #   实测 "🌀【天风汽车】地平线机器人交流要点梳理-1118"
    #   "地平线机器人" 里 "地平线" 是公司名但没有中文后缀 → 正则抓不到
    if re.search(r"(交流要点|交流纪要|调研纪要|调研要点|纪要|调研反馈|"
                 r"专家交流|电话会|路演|接待)", title):
        return "标题含交流/调研纪要类动作"
    return None


def classify_clause(clause: str, stock_names: list[str] | None = None) -> str:
    """
    单个子句分级 → "fact" | "method" | "direction" | "block"

    【判定顺序（2026-11-19 重排）】
    旧版把"含方向性关键词"一律降级，导致
      "资金调仓与产业基本面扩张并行"   → 被判 direction（其实这是两个事实并列）
      "晶圆厂收入与产能利用率上升"     → 被判 direction（其实是事实）
    → 新版改为**先看是不是客观事实**，再看是不是祈使句，最后才看方法论。

    1. 股票代码 / 本条关联个股名 → block
    2. 行情词：市场主体 → fact；个股主体 → block
    3. 祈使/建议句（应…转向/宜…区分）→ direction
    4. 客观事实谓词（增长/突破/披露/招标…）→ fact
    5. 方法词（验证/筛选/标准/口径…）→ method
    6. 兜底 → direction
    """
    if not clause:
        return "block"
    t = clause.strip()

    # 1) 股票代码 → 直接封杀
    if re.search(r"\b\d{6}(?:\.(?:SH|SZ|BJ|HK))?\b", t):
        return "block"

    # 2) 命中本条情报关联的个股名 → 封杀（最精确的个股判定）
    for n in (stock_names or []):
        if n and n in t:
            return "block"

    # 2b) 命中品牌/公司名黑名单 → 封杀
    #     （英伟达/台积电/昇腾云… 这些没有中文公司后缀，
    #      必须显式拦截，否则会漏进正文）
    for n in BRAND_NAMES:
        if n and n in t:
            return "block"

    # 3) 行情词：区分指数层面 vs 个股/细分板块层面
    if re.search(QUOTE_WORDS, t):
        # 3a) **指数/全市场层面 → 放行**（是市场结构事实）
        #     "指数收盘涨跌互现、创业板指跌幅最大且全市场成交缩量" ← 用户实测的反例
        #     —— 这句是合法的市场结构描述，不是荐股。
        #     "逾4300只个股下跌" 也是市场统计事实 → 放行
        if re.search(INDEX_SUBJECTS, t):
            if re.search(r"(买入|卖出|加仓|减仓|抄底|推荐|标的|优先)", t):
                return "block"
            return "fact"
        #     "光刻胶细分在半导体材料板块反弹时领涨" ← 实测漏放
        if re.search(SUB_SECTOR_SUBJECTS, t):
            return "block"
        # 3c) 其余（个股主体）→ 拦截
        return "block"

    # 4) 祈使/建议句 → direction（这是"应该往哪走"，不是事实）
    #    ⚠️ 必须排在事实判定**之前** ——
    #    "应转向有订单、运行量或渗透率验证的结构性增长主线" 里
    #    同时有"验证"和"应转向"，它本质是方法而非事实，但方向性强 → direction。
    if re.search(DIRECTIVE_PAT, t):
        if re.search(METHOD_BLOCKER, t):
            return "block"
        return "direction"

    # 4b) **"转向/聚焦…主线"型方向句** → direction
    #     ⚠️ 不含"应/宜"的祈使词，但同样是方向判断
    #     实测 "转向数据、大脑、国产算力及上游兑现主线" → 应判 direction
    if re.search(r"(转向|聚焦|切换|轮动到|主线)", t):
        return "direction"

    # 4c) **"关注…"型操作指向** → direction
    #     实测 "关注国产设备放量" —— "关注"是操作暗示，不是事实陈述
    if re.search(r"(关注|聚焦于|重点看)", t):
        return "direction"

    # 5) 操作指向 / 荐股 → block
    if re.search(TRADE_DIRECTION_PAT, t):
        return "block"

    # 5b) **预测/推断类** → direction
    #     ⚠️ 必须排在事实判定**之前** ——
    #     "后续超额收益将更多由泛科技与出海兑现驱动" 含"收益""驱动"，
    #     但它是在**预测未来**，不是陈述已发生的事实。
    #
    #     ⚠️ 但不能一刀切 —— 以下三类仍是事实：
    #       · 带时间锚点的既定安排："2027年直流项目预期加速落地"
    #       · 已发生的行为："…拓展海外增长空间"（拓展是已发生的行为）
    #       · 数量化的价格描述："价格上行并存"（描述已发生的价格变化）
    if re.search(r"(后续|未来|预计|预期|有望|或将|将会|展望|空间|弹性|"
                 r"超额收益|更多由|进一步提升|"
                 r"将(上升|下降|增长|回落|扩大|收紧|改善|走强|走弱|提升))", t):
        # 5b-1) 已发生的行为动词（拓展/推进/布局/落地/达成…）→ 事实
        if re.search(r"(已|正在|持续|陆续|纷纷).{0,6}"
                     r"(拓展|推进|落地|达成|完成|实现|提升|增长)", t):
            pass
        # 5b-2) 带年份/时间锚点 + 具体动作 → 既定安排，算事实
        #     ⚠️ 但如果句子里还有"预计/预计推动/将"等预测词 →
        #     时间锚点只是**预测的对象**，不是既定安排 → 仍算 direction
        #     实测 "高煤价环境预计推动2027年长协签约全面落地"（预测）
        #           vs "2027年直流项目预期加速落地"（既定安排）
        elif re.search(r"\d{4}年|\d{4}-\d{2}|明年|今年内|年底前", t) \
                and not re.search(r"(预计|预计推动|将|将会|有望|或将|"
                                  r"预期将|估计)", t):
            pass
        # 5b-3) 已披露/已实现的量化描述 → 事实
        #     ⚠️ 必须排除"将/预计/预计推动"这类**未来时**
        #     实测 "但市场煤价波动将上升"（预测）被误判为事实
        elif re.search(r"(已实现|已披露|已落地|已完成|已公布|实际|实测|"
                       r"实现同比|增长|下降|上升|达到|突破)", t) \
                and not re.search(r"(将|预计|预期|有望|将会|即将)", t):
            pass
        else:
            return "direction"

    # 6) 客观事实谓词 → fact
    #    ⚠️ 这一条要**早于** METHOD 判定：
    #    "家电出口在10月单月和1至10月累计口径下均实现同比增长"
    #    含"口径"（方法词）但它是事实 → 必须是 fact。
    if re.search(DESCRIPTIVE_PAT, t):
        return "fact"

    # 7) 方法与判据 → method
    if re.search(METHOD_PAT, t) and not re.search(METHOD_BLOCKER, t):
        return "method"

    # 8) 含市场主体 + 描述性动词 → fact
    if re.search(MARKET_SUBJECTS, t) and re.search(
            r"(增长|下降|上升|回升|回落|扩大|收窄|保持|达到|突破|同比|环比|"
            r"数据|指标|规模|占比|数量|总额|收入|产能|利用率|招标|渗透率|"
            r"集中|分散|覆盖|落地|实现|推进|完成|披露|公布)", t):
        return "fact"

    # 9) 纯客观陈述（含具体数字）→ fact
    if re.search(r"\d", t) and re.search(
            r"(增长|下降|上升|扩大|达到|突破|同比|环比|为|达|占|规模|收入|"
            r"产能|利用率|招标|渗透率|数量|总额|预期|实现|覆盖)", t):
        return "fact"

    # 10) 兜底：含方向性动词 → direction
    if re.search(r"(转向|聚焦|主线|重心|由.{1,12}转向|结构|轮动|承接|撤离|"
                 r"走强|走弱|景气|分化|重估)", t):
        return "direction"

    return "direction"


def grade_coreview(row: dict) -> dict:
    """
    把 coreView 分级拆解（2026-11-19 重构）。

    返回：
      "facts":     ✅ 可直接写入正文的事实子句
      "methods":   🔵 方法/判据句（需改写语气后写入）
      "directions":⚠️ 方向性判断（默认不用）
      "blocked":   ⛔ 不可用的表述
      "safe_facts":✅ 兼容旧字段 = facts + 改写后的 methods
      "caution":   ⚠️ 兼容旧字段 = directions
      "summary"/"title"/"postDate"/"sector"/"score"/...
    """
    cv = row.get("coreView") or {}
    stock_names = mentioned_stock_names(row)

    facts: list[str] = []
    methods: list[str] = []
    directions: list[str] = []
    blocked: list[str] = []

    def put(text: str):
        """对一段文本分句并归档。"""
        for cl in split_clauses(text):
            k = classify_clause(cl, stock_names)
            if k == "fact":
                facts.append(cl)
            elif k == "method":
                methods.append(cl)
            elif k == "method_blocked":
                blocked.append(cl)
            else:
                directions.append(cl)

    # coreView.content —— 核心观点句
    content = str(cv.get("content") or "").strip()
    if content:
        put(content)

    # coreView.premises —— 推理前提（最有价值的事实层）
    for p in (cv.get("premises") or []):
        put(str(p).strip())

    # coreView.conclusion —— 结论句
    conclusion = str(cv.get("conclusion") or "").strip()
    if conclusion:
        put(conclusion)

    # ⚠️ 合并模式下：多条情报的事实已经预先汇总在这里
    #   （见 merge_for_sector）
    for f in (cv.get("_merged_facts") or []):
        put(str(f).strip())
    for m in (cv.get("_merged_methods") or []):
        put(str(m).strip())

    # summary —— ⚠️ 常常与 content 完全相同 → 去重
    summary = str(row.get("summary") or "").strip()
    if summary and summary != content:
        put(summary)

    # 去重（保留首次出现）
    def dedup(items):
        seen = set()
        out = []
        for x in items:
            key = re.sub(r"[\s，,。.、；;：:]", "", x)[:45]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(x)
        return out

    facts = dedup(facts)
    methods = dedup(methods)
    directions = dedup(directions)
    blocked = dedup(blocked)

    return {
        "title": str(row.get("title") or ""),
        "postDate": str(row.get("postDate") or ""),
        "sector": list(row.get("swIndustryTag") or []),
        "categoryL1": str(row.get("categoryL1") or ""),
        "categoryL2": str(row.get("categoryL2") or ""),
        "source_kind": str(row.get("categoryL2") or ""),
        "facts": facts,
        "methods": methods,
        "directions": directions,
        "blocked": blocked,
        # 兼容旧字段
        "safe_facts": facts,
        "caution": directions,
        "summary": summary,
        "score": row.get("_score", 0),
    }


def fetch_detail(client: AxiomClient, row_id: str) -> dict:
    """取单条情报详情（含更完整的 coreView）。"""
    res = client._request("GET", f"/v1/intelligence/{row_id}")
    if isinstance(res, dict):
        return res.get("data", res)
    return {}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--sector", default="家电")
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    from source.sector_map import concept_keywords, infer_sectors

    rows = fetch_all()
    print(f"已加载 {len(rows)} 条情报")

    kws = concept_keywords(infer_sectors(args.sector), args.sector)
    print(f"板块「{args.sector}」检索词: {kws}\n")

    cand = find_for_sector(rows, kws)
    print(f"命中 {len(cand)} 条，前 {args.limit} 条：\n")
    for r in cand[:args.limit]:
        g = grade_coreview(r)
        print(f"  [{g['postDate']}] {g['title'][:46]}")
        print(f"     {g['categoryL1']}/{g['categoryL2']} | 标签={g['sector']} 分数={g['score']}")
        print(f"     ✅事实 {len(g['facts'])} | 🔵方法 {len(g['methods'])} "
              f"| ⚠️方向 {len(g['directions'])} | ⛔封杀 {len(g['blocked'])}")
        for f in g["facts"][:3]:
            print(f"        · {f[:76]}")
        for m in g["methods"][:2]:
            print(f"        ◆ {m[:76]}")
        for b in g["blocked"][:2]:
            print(f"        ⛔ {b[:76]}")
        print()
