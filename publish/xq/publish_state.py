"""
雪球「当日发布状态」权威查询 —— 发帖前的去重守卫。

【为什么必须查服务端，而不是查本地日志】
2026-11-19 实测踩坑：
  · 本地日志 `logs/20261119_xq_hot.json` 里 `published: false`，
    但**账号主页上明明有一篇 13:17 发的帖子**（id=417589417）。
  · 原因：13:17 那次发布成功后，17:12 又跑了一次 dry-run，
    **dry-run 把同一份日志覆盖了**（`_save()` 无条件写同一个文件名）
    → 「成功发布事实」被抹掉 → 当日去重守卫失效 → 会重复发帖（第十章）。

→ 所以：**当日是否已发、是否同一话题，必须以服务端 timeline 为准。**

【⚠️ 参数陷阱：`count=` 会返回假阴性】
`GET /v4/statuses/user_timeline.json?user_id=...&page=1&count=30`
  → HTTP 400，body = {"error_code":"10022","error_description":"用户未登录"}
  → **这是参数不被支持，不是真的掉登录**（页面文案、cookie 都在）

`GET /v4/statuses/user_timeline.json?user_id=...&page=1&size=30`
  → HTTP 200，正常返回 statuses

⚠️ 实测 2026-11-19：`count=30` → 400 假阴性；`size=30` → 200 正常。
   （原 cleanup_xq_posts.py 用 count=30 → 它的 `_still_exists()` 恒为 False
     → 会误报「已删除」。已修。）

【时效口径】
用 `created_at`（毫秒时间戳）换算本地日期，而不是字符串前缀。
"""

import re
import time
from datetime import datetime

USER_ID = "2569689091"


def fetch_user_timeline(session, max_pages: int = 3) -> list[dict]:
    """
    拉取账号已发帖列表（服务端权威）。

    返回 [{id, created_at(ms), time(YYYY-MM-DD HH:MM), text, tags}, ...]
    """
    session.open()
    time.sleep(3)

    all_posts: list[dict] = []
    for page_no in range(1, max_pages + 1):
        r = session.page.evaluate(
            """async (args) => {
            const [uid, pg] = args;
            try {
              const r = await fetch(
                `/v4/statuses/user_timeline.json?user_id=${uid}&page=${pg}&size=30`,
                {credentials:'include'});
              const t = await r.text();
              let j = null;
              try { j = JSON.parse(t); } catch(e) {}
              return {status:r.status, j:j, raw:t.slice(0,200)};
            } catch(e) { return {err:String(e).slice(0,140)}; }
          }""", [USER_ID, page_no])

        if r.get("status") != 200:
            print(f"   ⚠️ timeline page{page_no} HTTP {r.get('status')} "
                  f":: {str(r.get('raw') or r.get('j'))[:120]}", flush=True)
            break

        sts = (r.get("j") or {}).get("statuses") or []
        if not sts:
            break
        for x in sts:
            desc = x.get("description") or ""
            plain = re.sub(r"<br\s*/?>", "\n", desc)
            plain = re.sub(r"<[^>]+>", "", plain).replace("&nbsp;", " ").strip()
            ts = x.get("created_at") or 0
            all_posts.append({
                "id": x.get("id"),
                "created_at": ts,
                "time": (datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
                         if ts else ""),
                "date": (datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                         if ts else ""),
                "text": plain,
                "tags": re.findall(r"#([^#\s]{1,20})#", plain),
            })
        if len(sts) < 30:
            break
        time.sleep(1.2)

    return all_posts


def posts_on_day(posts: list[dict], day: str) -> list[dict]:
    """筛出某个自然日（YYYY-MM-DD）发布的帖子。"""
    return [p for p in posts if p.get("date") == day]


def published_topics(posts: list[dict]) -> set[str]:
    """
    提取历史帖子里出现过的「板块/话题」标记（雪球第十章去重用）。

    两个来源：
      1. 正文里的 #话题标签#
      2. 正文第一行里的板块词（标题是「XX这个方向…」→ 取 XX）
    """
    out: set[str] = set()
    for p in posts:
        for t in p.get("tags") or []:
            out.add(t)
        first = (p.get("text") or "").split("\n")[0]
        m = re.match(r"^(.{2,12}?)(?:这个方向|，)", first)
        if m:
            out.add(m.group(1))
    return out


def daily_state(posts: list[dict], day: str | None = None) -> dict:
    """
    当日发布状态（服务端权威）。

    返回 {
      "day": "2026-11-19",
      "count": 1,
      "posts": [...],
      "published_topics": {...},
    }
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    today_posts = posts_on_day(posts, day)
    return {
        "day": day,
        "count": len(today_posts),
        "posts": today_posts,
        "published_topics": published_topics(posts),
        "all_topics": published_topics(posts),
    }
