"""
雪球热点发帖主流程 —— 把用户要求的六步串成一条链。

【用户要求的流程】
1. 登录雪球 → 抓首页「热门话题」
2. 从话题里选涉及板块的（如今天有半导体、家电）
3. 把「日期 + 话题 + 板块」写进 热点记录.xlsx
4. 把板块拿到 Axiom「情报精选」搜索
5. 点最新一条 → 里面有「核心观点」
6. 把核心观点带到雪球发长文，再结合这些观点补充看法 → 发帖

【实现】
  步骤1-2  publish/xq/hotspots.py     fetch_hot_topics / fetch_hot_stocks
  步骤3    source/hotspot_log.py       append_rows
  步骤4-5  source/axiom_intel.py      fetch_all / find_for_sector / grade_coreview
  步骤6    source/gen_xq_hot.py       build_hot_post（合规分级 + 去重 + 去公司名）
           publish/xq/publisher.py     publish_post

【每一步的失败处理】
- 热门话题抓不到 → 报告（页面结构变了）
- 无板块话题 → 跳过（大盘类话题无法配对情报）
- Axiom 无匹配 → 换一个话题继续
- 合规不过 → **不发布**，写 blocked 日志

【不做的事】
- 不建组合（雪球组合默认公开、不可删）
- 不发任何站外信息
- 不在正文出现个股名
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import env_loader  # noqa: E402
from source.axiom_intel import (  # noqa: E402
    fetch_all, find_for_sector, pick_for_sector, pick_with_coverage,
)
from source.gen_xq_hot import build_hot_post, check_posts  # noqa: E402
from source.hotspot_log import append_rows, merge_pending  # noqa: E402
from source.sector_map import infer_sectors, sector_display  # noqa: E402

ROOT = Path(__file__).parent.parent


def _topic_already_published(sector: str, topic_text: str,
                             used_topics: set[str]) -> bool:
    """
    判断「这个话题/板块」是不是历史已经发过的（雪球第十章）。

    判据（任一命中即算重复）：
      1. 板块显示名（"先进封装"）出现在历史已发帖的话题标记里
      2. 话题原文里的板块词出现在历史已发帖的话题标记里

    ⚠️ 历史标记来自服务端 timeline：
       · 正文里的 #话题标签#（如 #先进封装#）
       · 正文首行的板块名（如"先进封装这个方向，今天…" → "先进封装"）
    """
    if not used_topics:
        return False
    if sector and sector in used_topics:
        return True
    for kw in (topic_text or "").split():
        k = kw.strip("，,。.、；;：:")
        if len(k) >= 2 and k in used_topics:
            return True
    return False


def _published_state(session) -> dict:
    """
    服务端权威的「已发布状态」（雪球第九/十章去重的唯一可信来源）。

    ⚠️ 2026-11-19 实测踩坑：
       本地日志里的 published 字段**不可信** —— 一次成功的发布会被
       之后的 dry-run 覆盖（两者写同一个文件名）→ 「今日已发」事实丢失
       → 守卫失效 → 重复发帖（触犯第十章）。
       → 一律以服务端 timeline 为准。
    """
    from publish.xq.publish_state import daily_state, fetch_user_timeline

    try:
        posts = fetch_user_timeline(session)
    except Exception as e:
        print(f"  ⚠️ 查询已发布状态失败: {str(e)[:80]}", flush=True)
        return {"day": datetime.now().strftime("%Y-%m-%d"), "count": 0,
                "posts": [], "published_topics": set(), "unavailable": True}

    st = daily_state(posts)
    print(f"  服务端核查：今日({st['day']})已发 {st['count']} 篇", flush=True)
    for p in st["posts"]:
        print(f"    · id={p['id']} [{p['time']}] {p['text'].split(chr(10))[0][:40]}",
              flush=True)
    return st


def run(publish: bool = True, limit: int = 1, log_dir: Path | None = None) -> dict:
    """
    主流程。

    publish=False 时只生成不发布（dry-run）。
    """
    from publish.xq.session import XqSession, port_alive
    from publish.xq.hotspots import fetch_hot_stocks, fetch_hot_topics

    log_dir = log_dir or (ROOT / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    report = {"date": today, "steps": [], "posts": [], "published": False}

    # ---------- 步骤 1-2：抓热门话题 ----------
    print("=" * 62)
    print("步骤 1-2｜抓取雪球热门话题")
    print("=" * 62, flush=True)

    with XqSession(headless=False, prefer_cdp=True) as session:
        # ---------- 步骤 0：登录（人工完成滑块，登录态由常驻浏览器持有）----------
        # ⚠️⚠️ 滑块（极验拼图）**程序化通过率很低**（实测多次重试常失败），
        #    而且它是平台风控手段 —— 这一环交给人做一次。
        #    之后所有流程通过 CDP 接管同一个常驻浏览器，登录态一直有效。
        print("=" * 62)
        print("步骤 0｜登录雪球")
        print("=" * 62, flush=True)

        if not session.is_logged_in():
            print("  当前未登录。请在浏览器窗口里完成登录：", flush=True)
            print("    ① 点「登 录」", flush=True)
            print("    ② 拖动滑块完成拼图验证", flush=True)
            print("  （滑块只过一次；登录态会保存在常驻浏览器里，后续无需再登）\n",
                  flush=True)
            ok_login = session.wait_for_login(timeout=1800)
        else:
            ok_login = True
            print("  ✅ 已是登录态", flush=True)

        if not ok_login:
            report["steps"].append({"step": "login", "ok": False,
                                    "error": "雪球登录未完成"})
            print("❌ 登录未完成，终止", flush=True)
            _save(log_dir, today, report)
            return report
        print("  ✅ 已登录\n", flush=True)

        # ---------- 步骤 0.5：当日去重守卫（服务端权威）----------
        # 雪球第九章禁止「批量发布雷同低质信息」，第十章禁止「重复发布」
        # → 一天只发一篇。判据必须是**服务端 timeline**，
        #   不能只看本地日志（dry-run 会覆盖发布记录，见 _published_state 注释）。
        pub_state = _published_state(session)
        if pub_state.get("unavailable"):
            print("  ⚠️ 无法核查服务端发布状态 → 中止（不盲发）", flush=True)
            report["steps"].append({"step": "dedup", "ok": False,
                                    "error": "服务端发布状态不可读，中止以免重复发帖"})
            _save(log_dir, today, report)
            return report

        if pub_state["count"] >= 1:
            already = pub_state["posts"][0]
            print(f"  ⏭️  今日已发布 {pub_state['count']} 篇"
                  f"（id={already['id']}，{already['time']}）", flush=True)
            print("     按「每天最多 1 篇」跳过本次发布（第九/十章）", flush=True)
            report["steps"].append({
                "step": "dedup", "ok": True, "skipped": True,
                "reason": "今日已有发布记录（第九章/第十章），不重复发布",
                "today_count": pub_state["count"],
                "existing_post_ids": [p["id"] for p in pub_state["posts"]],
            })
            report["published"] = False
            report["ok"] = True
            report["skipped"] = True
            report["skip_reason"] = "今日已发布（第九章/第十章）"
            report["existing_post_ids"] = [p["id"] for p in pub_state["posts"]]
            report["existing_post_url"] = (
                f"https://xueqiu.com/2569689091/{pub_state['posts'][0]['id']}"
                if pub_state["posts"] else "")
            _save(log_dir, today, report)
            return report

        # 历史已发话题（第十章：禁止同一话题换标题重发）
        used_topics = set(pub_state.get("published_topics") or set())
        if used_topics:
            print(f"  历史已发话题标记: {'/'.join(sorted(used_topics)[:8])}", flush=True)

        topics = fetch_hot_topics(session)
        hot_stocks = fetch_hot_stocks(session)

        if not topics:
            report["steps"].append({"step": "hot_topics", "ok": False,
                                    "error": "未抓到热门话题（页面结构可能已变）"})
            print("❌ 未抓到热门话题", flush=True)
            _save(log_dir, today, report)
            return report

        print(f"  热门话题 {len(topics)} 条 | 热股榜 {len(hot_stocks)} 只\n", flush=True)
        for t in topics:
            flag = "★" if t["has_sector"] else " "
            print(f"  {flag} {t['rank']:>2}. {t['text']}", flush=True)
            if t["sectors"]:
                print(f"        板块: {'/'.join(t['sectors'][:3])}", flush=True)

        # ---------- 步骤 3：写热点记录表 ----------
        print(f"\n{'=' * 62}")
        print("步骤 3｜写入 热点记录.xlsx")
        print("=" * 62, flush=True)

        # 先尝试把之前因文件占用而进备用文件的记录合回主表
        mp = merge_pending()
        if mp["merged"]:
            print(f"  （已自动合并 {mp['merged']} 条待写记录回主表）", flush=True)
        elif mp["files"] and not mp["ok"]:
            print(f"  ⚠️ 表格仍被占用，{len(mp['files'])} 个待合并文件保留中", flush=True)

        rows = []
        for t in topics:
            if not t["has_sector"]:
                continue
            secs = infer_sectors(t["text"])
            rows.append({
                "date": today,
                "topic": t["text"],
                "sector": sector_display(secs, t["text"]),
            })
        wr = append_rows(rows)
        print(f"  新增 {wr['added']} 条，跳过重复 {wr['skipped']} 条", flush=True)
        print(f"  → {wr['path']}", flush=True)
        if wr.get("fallback"):
            print(f"  ⚠️ 表格被 WPS/Excel 占用，无法直接写入", flush=True)
            print(f"     已写入备用文件：{wr['fallback']}", flush=True)
            print(f"     请关闭表格后，把备用文件内容合并进 热点记录.xlsx", flush=True)
        report["steps"].append({"step": "write_xlsx", "ok": True, **wr})

        # ---------- 步骤 4-5：找 Axiom 情报 ----------
        print(f"\n{'=' * 62}")
        print("步骤 4-5｜Axiom 情报精选：按板块找最新一条")
        print("=" * 62, flush=True)

        intel_rows = fetch_all()
        if not intel_rows:
            report["steps"].append({"step": "axiom_intel", "ok": False,
                                    "error": "Axiom 情报拉取失败"})
            print("❌ Axiom 情报拉取失败", flush=True)
            _save(log_dir, today, report)
            return report
        print(f"  已加载 {len(intel_rows)} 条情报\n", flush=True)

        # ---------- 步骤 6：成文 ----------
        print(f"{'=' * 62}")
        print("步骤 6｜生成雪球长文（核心观点 + 我的看法）")
        print("=" * 62, flush=True)

        posts = []
        # 今日热门话题涉及的**概念词**（板块显示名）
        # ⚠️ 必须用显示名（"半导体"/"先进封装"/"家电"…）而**不能**用申万标签 ——
        #    实测"半导体设备"和"先进封装"的申万标签都是 电子+机械设备，
        #    用标签排除会误伤同族板块（半导体帖里合法的"封装设备"会被剔掉）。
        all_sector_words = set()
        for t in topics:
            secs = infer_sectors(t["text"])
            if secs:
                all_sector_words.add(sector_display(secs, t["text"]))

        used_sectors: set[str] = set()
        used_intel: set[str] = set()
        for t in topics:
            if len(posts) >= limit:
                break
            if not t["has_sector"]:
                continue
            kws = t.get("keywords") or []
            if not kws:
                continue
            secs = infer_sectors(t["text"])
            sector = sector_display(secs, t["text"])
            # ⚠️ 同一板块不重复发（今日热门话题池里半导体系占 3 条，
            #    不去重会连发三篇同一板块）
            if sector in used_sectors:
                print(f"  {t['text'][:26]} → 与前一篇同板块（{sector}），跳过",
                      flush=True)
                continue
            # ⚠️ 雪球第十章：禁止「同一话题换标题重发」。
            #    判据 = 板块显示名 或 话题原文 与历史已发帖重合。
            #    （先进封装 → 昨天发过"先进封装" → 今天再发同一板块就是换标题重发）
            if _topic_already_published(sector, t.get("text", ""), used_topics):
                print(f"  {t['text'][:26]} → 与历史已发帖同一话题（{sector}），"
                      f"跳过（第十章）", flush=True)
                continue
            # ⚠️ 用 pick_with_coverage（严格）：不仅要相关度高，
            #    还必须有事实真的落在目标板块。
            #    实测「机器人」选中「11月19日复盘笔记」—— 整篇相关度满分，
            #    但 13 条事实里属于机器人的 0 条（其余是煤炭/房地产/电网）
            #    → 用它写机器人帖 = 拿别人的行业凑数。
            row = pick_with_coverage(intel_rows, sector, kws)
            if not row:
                print(f"  {t['text'][:26]} → Axiom 无高相关情报，换一个话题", flush=True)
                continue
            # ⚠️ 2026-11-20：**同一篇情报不重复用**
            #   实测「算力芯片」和「半导体」两个话题选中了同一条研报 →
            #   两篇帖子的事实几乎一样 → 内容雷同。
            intel_key = str(row.get("id") or row.get("title") or "")
            if intel_key and intel_key in used_intel:
                print(f"  {t['text'][:26]} → 与前一篇同一情报，跳过", flush=True)
                continue
            # 带上其他板块词（排除跑题事实）
            t = dict(t)
            t["other_sectors"] = [s for s in all_sector_words if s != sector]
            post = build_hot_post(t, row, hot_stocks, len(posts))
            posts.append(post)
            used_sectors.add(sector)
            used_intel.add(intel_key)
            m = post["_meta"]
            print(f"  ✅ 话题: {m['topic']}", flush=True)
            print(f"     板块: {m['sector']} | 情报: {m['intel_title'][:36]}", flush=True)
            print(f"     情报日期: {m['intel_date']} | 相关度: {row.get('_relevance')} "
                  f"| 引用事实 {m['facts_used']} 条", flush=True)
            print(f"     标题: {post['title']}", flush=True)
            print(f"     字数: {len(post['body'])} | 标签: {post['tags']}", flush=True)

        if not posts:
            report["steps"].append({"step": "compose", "ok": False,
                                    "error": "没有可用话题-情报配对"})
            print("\n❌ 没有可用的话题-情报配对", flush=True)
            _save(log_dir, today, report)
            return report

        # ---------- 合规闸门 ----------
        posts = check_posts(posts)
        blocked = [p for p in posts if p["_issues"]]
        ok_posts = [p for p in posts if not p["_issues"]]
        for p in blocked:
            print(f"\n  ⛔ 合规拦截: {p['title']}", flush=True)
            for i in p["_issues"]:
                print(f"     {i}", flush=True)

        report["posts"] = [{k: v for k, v in p.items() if not k.startswith("_")}
                            | {"_meta": p.get("_meta"), "_issues": p["_issues"]}
                            for p in posts]

        if not ok_posts:
            report["steps"].append({"step": "compliance", "ok": False,
                                    "error": "全部帖子被合规闸门拦截"})
            _save(log_dir, today, report)
            return report

        # ---------- 发布 ----------
        if not publish:
            print("\n（dry-run，未发布）", flush=True)
            for p in ok_posts:
                d = ROOT / "content" / "for_xq_hot"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{p['id']}.json").write_text(
                    json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
            report["steps"].append({"step": "dry_run", "ok": True,
                                    "count": len(ok_posts)})
            report["published"] = False
            report["ok"] = True
            report["compliance_passed"] = True
            report["title"] = ok_posts[0]["title"] if ok_posts else ""
            report["topic_id"] = ok_posts[0]["id"] if ok_posts else ""
            _save(log_dir, today, report)
            return report

        from publish.xq.publisher import publish_post
        results = []
        for p in ok_posts:
            print(f"\n  ▶ 发布: {p['title']}", flush=True)
            res = publish_post(p, session=session)
            for line in res.get("log", []):
                print(f"     {line}", flush=True)
            results.append(res)
            if res.get("ok"):
                report["published"] = True
            # 一篇失败就停，避免连续触发风控
            if not res.get("ok"):
                break

        report["steps"].append({"step": "publish", "ok": report["published"],
                                "results": results})
        # 字段一致性：ok = 流程是否走通（与 dry-run 语义一致）
        report["ok"] = report["published"]
        report["compliance_passed"] = True
        report["title"] = ok_posts[0]["title"] if ok_posts else ""
        report["topic_id"] = ok_posts[0]["id"] if ok_posts else ""
        if report["published"]:
            pid = None
            for r in results:
                if r.get("post_id"):
                    pid = r["post_id"]
                    break
            report["post_id"] = pid
            report["post_url"] = (f"https://xueqiu.com/2569689091/{pid}"
                                  if pid else "")
        _save(log_dir, today, report)
        return report


def _load_env() -> dict:
    """读凭证（统一走 config.env_loader，支持本地 .env 与资产库缓存）。"""
    return env_loader.load_env()


def save_report(log_dir: Path, filename: str, report: dict) -> Path:
    """
    合并写日志（**发布事实不可逆**）。

    ⚠️ 2026-11-19 修正（**发布记录被 dry-run 覆盖**的致命缺陷）：
       原实现无条件写同一个文件名 → 一次成功发布（published=true）会被
       之后的 dry-run（published=false）整个覆盖 → 「今日已发」事实丢失
       → 当日去重守卫失效 → 重复发帖（触犯雪球第十章）。
       （实测 2026-11-19 13:17 发了帖，17:12 dry-run 后日志里 published=false，
         而服务端明明有那篇帖子 id=417589417。）

    → 规则：**published 一旦为 true，任何后续运行都不能把它改回 false**，
       且 post_id / post_url 永远保留。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / filename
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    merged = dict(old)
    merged.update(report)

    if old.get("published") is True:
        merged["published"] = True
        merged["post_id"] = old.get("post_id") or merged.get("post_id")
        merged["post_url"] = old.get("post_url") or merged.get("post_url")
        merged.setdefault("published_at_first_run", old.get("date"))

    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str),
                 encoding="utf-8")
    return p


def _save(log_dir: Path, today: str, report: dict) -> None:
    p = save_report(log_dir, f"{today}_xq_hot.json", report)
    print(f"\n日志: {p}", flush=True)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    lim = 1
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            lim = int(sys.argv[i + 1])
    run(publish=not dry, limit=lim)
