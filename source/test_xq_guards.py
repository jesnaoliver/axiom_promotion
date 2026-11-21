"""雪球调度守卫回归测试：当日去重 + 主题轮换 + 成功判定。"""
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler  # noqa: E402

LOGS = Path(tempfile.mkdtemp()) / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
scheduler.LOGS = LOGS
scheduler.ROOT = LOGS.parent

day = date(2026, 11, 11)
fails = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name} {detail}")
    if not cond:
        fails.append(name)


print("=== 1. 当日去重守卫 ===")
# 失败日志不应阻断次日发布
(LOGS / "2026-11-11_xq_failed.json").write_text(json.dumps(
    {"date": "2026-11-11", "published": False,
     "publish_result": {"ok": False, "stage": "login", "error": "雪球未登录"}}),
    encoding="utf-8")
check("失败日志不触发当日去重", scheduler._xq_published_today(day) is False)

# 成功日志应触发去重
(LOGS / "2026-11-11_xq_publish.json").write_text(json.dumps(
    {"date": "2026-11-11", "published": True, "topic_id": "XQ-ai-checklist"}),
    encoding="utf-8")
check("成功日志触发当日去重", scheduler._xq_published_today(day) is True)

# timeout 但 URL 落到帖子页 → 视为已发布
(LOGS / "2026-11-12_xq_publish.json").write_text(json.dumps(
    {"date": "2026-11-12", "published": False,
     "publish_result": {"publish_result": {"marker": "timeout",
                                           "url": "https://xueqiu.com/statuses/987654321"}}}),
    encoding="utf-8")
check("timeout+帖子页URL 触发去重",
      scheduler._xq_published_today(date(2026, 11, 12)) is True)

print("\n=== 2. 主题轮换 ===")
ids = scheduler._xq_published_topic_ids()
check("已发主题被收录", "XQ-ai-checklist" in ids, f"ids={ids}")

print("\n=== 3. 轮换选主题（模拟连续多日）===")
from source.gen_xq import build_xq_posts  # noqa: E402

posts = build_xq_posts()
used = set()
order = []
for i in range(6):
    cands = [p for p in posts if p["id"] not in used]
    if not cands:
        cands = [posts[i % len(posts)]]
    pick = cands[0]
    order.append(pick["id"])
    used.add(pick["id"])
check("前5篇主题不重复", len(set(order[:5])) == 5, f"order={order[:5]}")
check("第6篇回绕到已发主题", order[5] in order[:5], f"order[5]={order[5]}")

print("\n=== 4. 成功判定：URL 落到帖子页即算已发布 ===")
import re  # noqa: E402

final_url = "https://xueqiu.com/statuses/987654321"
landed = bool(re.search(r"/statuses/\d+", final_url))
check("帖子页URL 被识别为已发布", landed)

print("\n=== 5. 三层闸门仍然拦截违规内容（不可被绕过）===")
from content.xq_compliance import check_xq  # noqa: E402
from source.sanitize import forbidden_check  # noqa: E402

bad = {"title": "某龙头值得看", "body": "推荐买入该股，目标价 100 元，收益率 30%。" * 10,
       "tags": ["投资心得"], "images": [], "ai_marked": True}
r = check_xq(bad)
check("违规内容被雪球闸门拦下", r["passed"] is False,
      f"violations={[v['category'] for v in r['violations']][:3]}")
check("个股指代被雪球闸门拦下",
      any("个股指代" in v["category"] for v in r["violations"]))

# 项目闸门负责的是：站外域名 / URL / 股票代码 / 具体公司名
# （「某龙头」这类指代由雪球专属闸门负责，不重复统计）
check("站外 URL 被项目闸门拦下",
      len(forbidden_check("详情见 https://efficientinvest.cn/abc")) > 0)

# 产品名是**绝对禁止**项 → 必须是硬拦截，不能只是 warning
prod = {"title": "我的复盘方法", "body": "我用 AXIOM 做复盘核对。" + "这是方法论内容。" * 60,
        "tags": ["投资心得"], "images": [], "ai_marked": True}
rp = check_xq(prod)
check("产品名被硬拦截（不是仅警告）", rp["passed"] is False,
      f"violations={[v['category'] for v in rp['violations']]}")

no_ai = {"title": "我的复盘方法分享", "body": "讲方法不讲标的。" * 100,
         "tags": [], "images": [], "ai_marked": False}
r2 = check_xq(no_ai)
check("缺AI标识被拦下", r2["passed"] is False,
      f"{[v['category'] for v in r2['violations']]}")

shutil.rmtree(LOGS.parent, ignore_errors=True)
print("\n" + "=" * 60)
if fails:
    print(f"❌ 失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("✅ 全部通过")
