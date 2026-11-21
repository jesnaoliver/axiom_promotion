"""
回归验证：确认改版后 xq_hot 链路的**内容生成段**仍然正常。

不发布。只跑：
  抓热门话题 → 写热点记录.xlsx → Axiom 情报 → 成文 → 合规闸门
并顺带验证：
  · _topic_already_published 的去重判据（第十章）
  · published_topics 的抽取结果
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from publish.xq.session import XqSession  # noqa: E402
from publish.xq.hotspots import fetch_hot_stocks, fetch_hot_topics  # noqa: E402
from publish.xq.publish_state import daily_state, fetch_user_timeline  # noqa: E402
from source.axiom_intel import fetch_all, pick_for_sector  # noqa: E402
from source.build_xq_hot_post import _topic_already_published  # noqa: E402
from source.gen_xq_hot import build_hot_post, check_posts  # noqa: E402
from source.hotspot_log import append_rows, merge_pending  # noqa: E402
from source.sector_map import infer_sectors, sector_display  # noqa: E402

with XqSession(headless=False, prefer_cdp=True) as s:
    print("=== A. 登录 + 服务端状态 ===", flush=True)
    print(f"  登录: {'✅' if s.is_logged_in() else '❌'}", flush=True)
    posts = fetch_user_timeline(s)
    st = daily_state(posts)
    print(f"  今日({st['day']})已发 {st['count']} 篇", flush=True)
    print(f"  历史话题标记: {sorted(st['published_topics'])}", flush=True)

    print("\n=== B. 第十章去重判据单测 ===", flush=True)
    used = st["published_topics"]
    cases = [
        ("先进封装", "先进封装概念拉升，大港股份涨停", True, "同板块+同话题"),
        ("半导体", "半导体设备板块走强，耐科装备涨停", False, "不同话题"),
        ("机器人", "机器人概念拉升，襄阳轴承涨停", False, "不同话题"),
    ]
    for sector, text, expect, why in cases:
        got = _topic_already_published(sector, text, used)
        flag = "✅" if got == expect else "❌"
        print(f"  {flag} {sector} / {text[:22]} → {got} (期望 {expect}｜{why})",
              flush=True)

    print("\n=== C. 抓热门话题 ===", flush=True)
    topics = fetch_hot_topics(s)
    stocks = fetch_hot_stocks(s)
    print(f"  话题 {len(topics)} 条 | 热股榜 {len(stocks)} 只", flush=True)
    for t in topics:
        flag = "★" if t["has_sector"] else " "
        print(f"  {flag} {t['rank']:>2}. {t['text']}", flush=True)

    print("\n=== D. 写热点记录.xlsx ===", flush=True)
    mp = merge_pending()
    print(f"  待合并文件合并: {mp}", flush=True)
    rows = [{"date": time.strftime("%Y%m%d"), "topic": t["text"],
             "sector": sector_display(infer_sectors(t["text"]), t["text"])}
            for t in topics if t["has_sector"]]
    wr = append_rows(rows)
    print(f"  新增 {wr['added']} / 跳过 {wr['skipped']} → {wr['path']}", flush=True)

    print("\n=== E. Axiom 情报 ===", flush=True)
    intel = fetch_all()
    print(f"  情报 {len(intel)} 条", flush=True)

    print("\n=== F. 成文 + 合规 ===", flush=True)
    all_secs = {sector_display(infer_sectors(t["text"]), t["text"])
                for t in topics if t["has_sector"]}
    made = []
    for t in topics:
        if not t["has_sector"] or not t.get("keywords"):
            continue
        secs = infer_sectors(t["text"])
        sector = sector_display(secs, t["text"])
        if sector in {m["_meta"]["sector"] for m in made}:
            continue
        if _topic_already_published(sector, t["text"], used):
            print(f"  ⏭️ {t['text'][:26]} → 历史已发同话题，跳过（第十章）", flush=True)
            continue
        row = pick_for_sector(intel, sector, t["keywords"])
        if not row:
            print(f"  ⏭️ {t['text'][:26]} → Axiom 无高相关情报", flush=True)
            continue
        tt = dict(t)
        tt["other_sectors"] = [x for x in all_secs if x != sector]
        p = build_hot_post(tt, row, stocks, len(made))
        made.append(p)
        m = p["_meta"]
        print(f"  ✅ {m['sector']} | 情报: {m['intel_title'][:34]} | "
              f"事实 {m['facts_used']} 条(封杀{m['facts_blocked']}/方向{m['facts_caution']})",
              flush=True)
        print(f"     标题: {p['title']}", flush=True)

    posts_checked = check_posts(made)
    for p in posts_checked:
        issues = p["_issues"]
        print(f"  合规 {'PASS' if not issues else 'BLOCK'}: {p['title'][:30]} "
              f"{issues if issues else ''}", flush=True)
    print(f"\n生成 {len(made)} 篇 | 全部通过: {all(not p['_issues'] for p in posts_checked)}",
          flush=True)
