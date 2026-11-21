"""
雪球热门话题抓取。

【实测结构（2026-11-17）】
容器：div.HashtagHotList_board_3Oq
条目：a[href^="/hashtag/"]  —— 10 条，文本即话题原文
示例：
  1 上证指数开盘跌破3600点
  2 半导体设备大幅拉升，东方嘉盛涨停
  6 家电板块反复活跃，长虹美菱2连板

【重要：话题里含个股名】
"半导体设备大幅拉升，东方嘉盛涨停" 里 "东方嘉盛" 是个股名。
→ 话题原文可以写进 **热点记录.xlsx**（内部台账，用户明确要求）
→ 但**雪球正文里不能出现个股名** —— 由 content/xq_compliance.py 拦截。
   发帖正文只引用「板块 + 话题描述」，个股名一律剔除。
"""

import re
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent

HOT_CONTAINER_HINTS = [
    "HashtagHotList_board",
    "HashtagHotList",
]



def _parse_hot_topics_from_text(page) -> list[dict]:
    """
    从页面**文本**解析热门话题（DOM 解析的兜底）。

    ⚠️⚠️ **实测（Edge，登录态）**：
    - 文本里有完整的 10 条热门话题
    - 但 `a[href^="/hashtag/"]` **只有页脚那 2 个**（#我给雪球提建议# / #防诈骗举报专区#）
    - `HashtagHotList` 容器**根本没渲染成 div**
    → 登录态下 Edge 首页的话题区是**纯文本渲染**，DOM 定位拿不到！
    （Chrome 游客态反而能拿到 DOM）

    文本格式（稳定）：
        热门话题
        1\t
        家电板块拉升，长虹美菱3连板
        (空行)
        2\t
        英伟达营收首破千亿，指引再创新高
        ...
    """
    try:
        body = page.inner_text("body")
    except Exception:
        return []

    i = body.find("热门话题")
    if i < 0:
        return []
    seg = body[i + 4: i + 4 + 800]
    # ⚠️ 必须截断 —— 否则会混进后面的「热股榜/热门基金」列表
    for stop in ("热股榜", "热门基金", "热门股票", "热门视频", "热门资讯"):
        j = seg.find(stop)
        if j > 0:
            seg = seg[:j]
            break

    # 以「序号 + 制表符」切分
    parts = re.split(r"\n\s*(\d{1,2})\t\n", seg)
    out = []
    # parts = [前导文本, '1', 话题1, '2', 话题2, ...]
    for k in range(1, len(parts) - 1, 2):
        try:
            rank = int(parts[k])
        except ValueError:
            continue
        txt = parts[k + 1].strip().split("\n")[0].strip()
        if not txt or len(txt) > 60:
            continue
        out.append({"rank": rank, "text": txt})
    # 只要前 10 条（热门话题固定 10 条）
    out = [o for o in out if 1 <= o["rank"] <= 10]
    return out

def fetch_hot_topics(session) -> list[dict]:
    """
    抓取雪球首页「热门话题」列表。

    返回 [{rank, text, href, sectors, keywords}, ...]
    """
    session.open()
    time.sleep(7)
    # ⚠️ 热门话题是**懒加载**的：无头模式下不渲染（实测 headless=True 抓到 0 条，
    #    headless=False 能抓到 10 条）→ 滚一下触发渲染
    #    Edge 比 Chrome 慢，多滚几次、多等一会儿
    for _ in range(3):
        try:
            session.page.mouse.wheel(0, 500)
            time.sleep(1.0)
            session.page.mouse.wheel(0, -500)
            time.sleep(0.8)
        except Exception:
            break

    items = session.page.evaluate(r"""
        () => {
            const out = [];
            // ⚠️ 不能靠"标题文本 == '热门话题'"定位 ——
            //    容器 div 的 textContent 包含全部子项文本，永远不等于 '热门话题'。
            // ✅ 直接按 class 前缀定位（实测稳定）：
            //    HashtagHotList_board_3Oq  ← 主容器
            //    HashtagHotList_tabs__panel_2fJ ← 标签页面板
            let container = null;
            const boards = [...document.querySelectorAll('div')]
                .filter(d => (d.className||'').toString().includes('HashtagHotList'));
            for (const b of boards) {
                const n = b.querySelectorAll('a[href^="/hashtag/"]').length;
                if (n >= 5) { container = b; break; }
            }
            if (!container) {
                // 兜底：任意含 hashtag 链接的公共祖先
                const all = [...document.querySelectorAll('a[href^="/hashtag/"]')];
                if (all.length) {
                    let p = all[0].parentElement;
                    for (let i = 0; i < 5 && p; i++) {
                        if (p.querySelectorAll('a[href^="/hashtag/"]').length >= 5) {
                            container = p; break;
                        }
                        p = p.parentElement;
                    }
                }
            }
            if (!container) return out;

            const seen = new Set();
            container.querySelectorAll('a[href^="/hashtag/"]').forEach((a, i) => {
                const t = (a.textContent||'').trim();
                const h = a.getAttribute('href')||'';
                if (!t || seen.has(t)) return;
                seen.add(t);
                out.push({idx: i, text: t, href: h});
            });
            return out;
        }
    """)

    if not items:
        # 重试一次：重新加载 + 再滚
        try:
            session.open()
            time.sleep(6)
            for _ in range(2):
                session.page.mouse.wheel(0, 500)
                time.sleep(1.0)
                session.page.mouse.wheel(0, -500)
                time.sleep(0.8)
            items = session.page.evaluate(r"""
                () => {
                    const out = [];
                    const boards = [...document.querySelectorAll('div')]
                        .filter(d => (d.className||'').toString().includes('HashtagHotList'));
                    let container = null;
                    for (const b of boards) {
                        if (b.querySelectorAll('a[href^="/hashtag/"]').length >= 5) {
                            container = b; break;
                        }
                    }
                    if (!container) return out;
                    const seen = new Set();
                    container.querySelectorAll('a[href^="/hashtag/"]').forEach((a, i) => {
                        const t = (a.textContent||'').trim();
                        if (!t || seen.has(t)) return;
                        seen.add(t);
                        out.push({idx: i, text: t, href: a.getAttribute('href')||''});
                    });
                    return out;
                }
            """)
        except Exception:
            pass

    if not items:
        # ⚠️ DOM 拿不到（Edge 登录态下话题区是纯文本渲染）→ 从文本解析
        items = _parse_hot_topics_from_text(session.page)

    # 按文本里的序号排序（页面显示 1..10）
    def _rank(item):
        m = __import__("re").match(r"^(\d{1,2})", item["text"])
        return int(m.group(1)) if m else item["idx"] + 1

    from source.sector_map import concept_keywords, infer_sectors

    result = []
    for idx, it in enumerate(items):
        text = it.get("text", "")
        if not text:
            continue
        # 去掉前缀序号（DOM 路径里序号可能在文本里）
        clean = re.sub(r"^\d{1,2}\s*", "", text).strip()
        if not clean:
            continue
        rank = it.get("rank") or (idx + 1)
        sectors = infer_sectors(clean)
        result.append({
            "rank": int(rank),
            "text": clean,
            "raw": text,
            "href": it.get("href", ""),
            "sectors": sectors,
            "keywords": concept_keywords(sectors, clean),
            "has_sector": bool(sectors),
        })

    result.sort(key=lambda x: x["rank"])
    return result


def fetch_hot_stocks(session) -> list[str]:
    """
    抓首页「热股榜」的股票名（当日热门个股名单）。

    用途：**交叉验证**热门话题里的个股名。
    话题说"半导体设备大幅拉升，东方嘉盛涨停"，热股榜里若有"东方嘉盛"，
    就能确定它是个股名 → 正文里必须剔除。
    这比正则可靠得多。
    """
    session.open()
    time.sleep(5)
    try:
        session.page.mouse.wheel(0, 400)
        time.sleep(1.2)
        session.page.mouse.wheel(0, -400)
        time.sleep(0.8)
    except Exception:
        pass

    names = session.page.evaluate(r"""
        () => {
            const boards = [...document.querySelectorAll('div')]
                .filter(d => (d.className||'').toString().includes('StockHotList'));
            let box = null;
            for (const b of boards) {
                if (b.querySelectorAll('a[href^="/S/"]').length >= 3) { box = b; break; }
            }
            if (!box) {
                const a = document.querySelector('a[href^="/S/"]');
                if (a) { let p = a.parentElement;
                    for (let i=0;i<5&&p;i++){ if(p.querySelectorAll('a[href^="/S/"]').length>=3){box=p;break;} p=p.parentElement; } }
            }
            if (!box) return [];
            const out = [];
            const seen = new Set();
            box.querySelectorAll('a[href^="/S/"]').forEach(a => {
                const t = (a.textContent||'').trim();
                if (!t || seen.has(t) || t.length > 8) return;
                // ⚠️ 排除指数与基金（"上证指数""XX ETF联接A""XX混合"都不是热门个股）
                if (/指数|ETF|LOF|混合|联接|债|FOF|QDII|指数A$/.test(t)) return;
                seen.add(t); out.push(t);
            });
            return out;
        }
    """)
    return names


def pick_topics_with_sector(topics: list[dict], limit: int = 3) -> list[dict]:
    """挑出有板块归属的话题（大盘类话题无板块，跳过）。"""
    out = [t for t in topics if t.get("has_sector")]
    return out[:limit]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from publish.xq.session import XqSession

    # ⚠️ 必须有头模式：headless=True 抓不到热门话题（雪球懒加载 + 反爬）
    with XqSession(headless=False, prefer_cdp=True) as s:
        # ⚠️ 热门话题是**游客可见**的（首页右侧），不依赖登录态
        topics = fetch_hot_topics(s)
        stocks = fetch_hot_stocks(s)
        print(f"热股榜个股: {stocks}\n")
        if not topics:
            print("❌ 未抓到热门话题（页面结构可能已变）")
            raise SystemExit(1)
        print(f"共 {len(topics)} 条热门话题：\n")
        for t in topics:
            flag = "★" if t["has_sector"] else " "
            print(f" {flag} {t['rank']:>2}. {t['text']}")
            if t["sectors"]:
                print(f"        板块: {'/'.join(t['sectors'])}")
                print(f"        检索词: {t['keywords']}")
        print("\n★ = 有板块归属（可用于配对 Axiom 情报）")
