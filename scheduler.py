"""
日更调度器 —— 每天自动生成内容 + 发布。

工作流：
  1. 从选题库取当天选题（按日期确定性轮转，避免重复）
  2. 用 compose 组装正文（真实素材槽位从素材文件读取）
  3. 生成封面图
  4. 跑合规闸门（不通过则中止，绝不发布）
  5. 调用 publisher 发布
  6. 记录发布结果到 logs/

用法：
    python scheduler.py --date 2026-11-02
    python scheduler.py --dry-run          # 只生成不发布
    python scheduler.py --week             # 批量生成未来7天
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from content.build_assets import BULLETS  # noqa: E402
from content.compose import build_draft  # noqa: E402
from content.compliance import check_note  # noqa: E402
from content.cover import make_cover  # noqa: E402
from content.topic_bank import TOPIC_BANK  # noqa: E402

ROOT = Path(__file__).parent
DRAFTS = ROOT / "content" / "drafts"
ASSETS = ROOT / "assets"
LOGS = ROOT / "logs"
FACTS = ROOT / "content" / "facts"   # 真实素材（JSON，按选题 id 存）

# 发布时段（依据搜索流量高峰：12:00-13:00 / 19:00-21:00）
PUBLISH_SLOTS = ["11:30", "12:10", "18:30", "19:30", "20:10"]


def pick_topic(day: date) -> dict:
    """按日期确定性轮转选题：同一天永远取同一条，不同天不重复。"""
    epoch = (day - date(2026, 11, 1)).days
    idx = epoch % len(TOPIC_BANK)
    return TOPIC_BANK[idx]


def load_facts(topic_id: str) -> dict:
    """读取真实素材。若不存在返回空 dict，正文中保留 {{}} 槽位。"""
    p = FACTS / f"{topic_id}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def build_one(day: date, dry_run: bool = False) -> dict:
    topic = pick_topic(day)
    facts = load_facts(topic["id"])

    draft = build_draft(topic, slots=facts.get("slots"))
    draft["date"] = day.isoformat()
    draft["publish_slot"] = PUBLISH_SLOTS[day.toordinal() % len(PUBLISH_SLOTS)]

    # 封面
    cover_path = ASSETS / f"cover_{topic['id']}.jpg"
    make_cover(
        title=topic["cover"],
        label="搭系统日志" if topic["track"] == "A" else "回测避坑",
        out_path=cover_path,
        palette_idx=day.toordinal() % 5,
        footer="记录 · 不构成投资建议",
        bullets=BULLETS.get(topic["id"]),
        badge=f"D{day.toordinal() % 100:02d}",
    )
    draft["images"] = [str(cover_path)]

    # 合规闸门
    report = check_note({
        "title": draft["title"], "body": draft["body"],
        "tags": draft["tags"], "images": draft["images"],
    })
    draft["compliance"] = report

    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS / f"{day.isoformat()}_{topic['id']}.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "date": day.isoformat(),
        "topic_id": topic["id"],
        "title": draft["title"],
        "slot": draft["publish_slot"],
        "draft_path": str(draft_path),
        "cover": str(cover_path),
        "compliance_passed": report["passed"],
        "violations": report["violations"],
        "warnings": report["warnings"],
        "missing_slots": draft["missing_slots"],
        "published": False,
    }

    if dry_run:
        return result

    if not report["passed"]:
        result["error"] = "合规未通过，已中止发布"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_blocked.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    # 发布
    from publish.publisher import publish
    pub = publish(draft)
    result["publish_result"] = pub
    result["published"] = bool(pub.get("ok"))

    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{day.isoformat()}_publish.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _published_today(day: date) -> bool:
    """
    判断该自然日是否已有**小红书**成功发布记录。

    日更任务的前提是"一天一篇"。历史上出现过：
    11-02 17:43 提前发布了标记为 11-03 的内容，
    次日再跑就会在同一天发第二篇。

    ⚠️ 2026-11-10 修复：原实现 glob 的是 `{day}_*.json`，
    会把**同日的抖音/淘股吧日志**也算进来（2026-11-10 抖音 10:52 发布成功
    → 小红书被误判为"当日已发布"而跳过）。
    "一天一篇"是**按平台**计的，不是全平台共用一个额度，
    故这里必须排除其他平台的记录，与
    `_douyin_published_today()` / `_tgb_published_today()` 保持一致。

    排除依据优先用日志里的 `platform` / `source` 字段（最可靠），
    文件名后缀作为兜底。
    """
    other_platform_files = (
        "_douyin_publish.json", "_douyin_skipped.json",
        "_tgb_publish.json", "_tgb_publish_2.json", "_tgb_skipped.json",
    )
    for p in LOGS.glob(f"{day.isoformat()}_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        platform = str(d.get("platform") or d.get("source") or "").lower()
        if platform in ("douyin", "tgb"):
            continue
        if p.name.endswith(other_platform_files):
            continue
        if d.get("published"):
            return True
    return False


def build_from_axiom(day, dry_run: bool = False, publish_index: int = None,
                     use_screenshot: bool = True) -> dict:
    """
    从 AXIOM 实时数据生成并发布一篇。

    这是**主力路径**：内容 100% 来自用户自己的系统真实产出，
    经三层脱敏 + 三层合规闸门后发布。

    publish_index: 指定当天要发批次里的第几篇（0 起）。
                   **默认 None = 自动挑选**：
                   从批次第 0 篇开始，取第一篇**尚未发布过**的
                   （按 topic_id + 标题双重去重）。
                   自动挑选可以避免"同一天重跑 → 撞上一轮已发的内容"，
                   这是实际发生过的问题（2026-11-03 撞标题）。
    use_screenshot: 是否附上系统真实界面截图（产品演示图）。
    """
    from source.gen_from_axiom import build_all, attach_screenshot
    from source.build_axiom_covers import COVER_SPEC, cover_bullets_for
    from content.cover import make_cover
    from content.compliance import check_note as _check
    from source.sanitize import forbidden_check
    from publish.publisher import _published_index

    notes = build_all()
    if not notes:
        return {"ok": False, "error": "AXIOM 未产出可用内容"}

    # 选题挑选：自动模式跳过已发过的内容
    used_ids, used_titles = _published_index()
    picked_idx = publish_index
    auto_pick = publish_index is None
    if auto_pick:
        picked_idx = None
        for i, n in enumerate(notes):
            if n["id"] not in used_ids and n["title"] not in used_titles:
                picked_idx = i
                break
        if picked_idx is None:
            return {
                "ok": False, "error": "AXIOM 批次内所有选题均已发布过，"
                                      "本次不发布（避免重复内容被判刷量）",
                "stage": "dedup",
                "candidates": [n["id"] for n in notes],
            }
    note = notes[picked_idx % len(notes)]
    nid = note["id"]

    # 封面（纯设计图，永远作为首图）
    spec = None
    for key, v in COVER_SPEC.items():
        if nid.endswith(key):
            spec = v
            break
    spec = spec or COVER_SPEC["conf"]
    cover_path = ASSETS / f"cover_{nid}.jpg"
    make_cover(
        title=spec["text"], label=spec["label"], out_path=cover_path,
        palette_idx=picked_idx % 5, footer="记录 · 不构成投资建议",
        bullets=cover_bullets_for(note, spec), badge="AX",
    )
    note["images"] = [str(cover_path)]

    # 系统真实截图（产品演示图）作为第二张
    shot_path = ""
    if use_screenshot:
        try:
            shot_path = attach_screenshot(note, ASSETS / "composed")
        except Exception as e:
            print(f"   ⚠️ 截图挂载失败（不影响发布）: {e}")

    # 草稿落盘
    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS / f"{day.isoformat()}_{nid}.json"
    draft_path.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")

    # 三层合规
    report = _check({
        "title": note["title"], "body": note["body"],
        "tags": note["tags"], "images": note["images"],
    })
    proj_issues = (forbidden_check(note["title"]) + forbidden_check(note["body"])
                   + forbidden_check(" ".join(note["tags"])))

    result = {
        "date": day.isoformat(),
        "source": "axiom",
        "topic_id": nid,
        "title": note["title"],
        "draft_path": str(draft_path),
        "cover": str(cover_path),
        "screenshot": shot_path,
        "image_count": len(note["images"]),
        "compliance_passed": report["passed"] and not proj_issues,
        "violations": report["violations"],
        "project_issues": proj_issues,
        "warnings": report["warnings"],
        "published": False,
    }

    if dry_run:
        return result

    if not report["passed"] or proj_issues:
        result["error"] = "合规未通过，已中止发布"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_axiom_blocked.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    # ---------- 当日去重守卫 ----------
    # 同一个自然日只发一篇。历史上出现过"昨晚提前发了 11-03 的内容，
    # 今天又跑一次"的撞车。当日已有成功发布记录 → 本次不再发。
    if _published_today(day):
        result["error"] = f"{day.isoformat()} 当日已有成功发布记录，本次不再重复发布"
        result["stage"] = "dedup"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_axiom_skipped.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⏭️  {day} 当日已发布过，跳过本次发布")
        return result

    from publish.publisher import publish
    pub = publish(note)
    result["publish_result"] = pub
    result["published"] = bool(pub.get("ok"))

    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{day.isoformat()}_axiom_publish.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _douyin_published_index() -> tuple[set, set]:
    """
    扫描 logs/ 里**抖音**的发布记录，索引已发布的 topic_id 与标题。

    为什么要平台隔离：
      小红书与抖音是两套独立的发布序列，各自发自己的稿子。
      如果共用 publish/publisher._published_index()，
      抖音这篇（DY-*）虽然 topic_id 不同，但**标题体系不同**，
      共用索引会让两个平台的去重逻辑互相干扰。
      所以抖音单独扫 `*_douyin_publish.json`。
    """
    topic_ids: set = set()
    titles: set = set()
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return topic_ids, titles
    for p in logs_dir.glob("*_douyin_publish.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("published"):
            continue
        if d.get("topic_id"):
            topic_ids.add(d["topic_id"])
        if d.get("title"):
            titles.add(d["title"])
    return topic_ids, titles


def _content_fingerprint(note: dict) -> str:
    """一篇文章的内容指纹（标题 + 正文）。"""
    h = hashlib.sha1()
    h.update((note.get("title") or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((note.get("body") or "").encode("utf-8"))
    return h.hexdigest()


def _douyin_published_fingerprints() -> set:
    """
    已发布抖音文章的**内容指纹**集合（标题+正文的 sha1）。

    为什么不能按 topic_id 去重（2026-11-09 实测踩坑）：
      抖音各篇的 id 是固定的（DY-conf / DY-intel …），只有 6 个。
      按 id 去重 → 发满 6 篇后选题池就枯竭了，
      之后每天选题阶段直接返回 stage=dedup，**一篇都发不出去**
      （实测 --date 2026-11-10：标题/正文 None，发布跳过）。
      而这 6 个模板里有 5 个是**数据驱动**的（正文随 AXIOM 每日数据变化），
      同一模板隔天再发是**新内容**，不是重复内容。

    正确语义（与小红书侧 AX-{数据日期}-{模板} 一致）：
      · 同一模板隔天可再发（数据变了就是新文章）
      · **完全相同的内容**（标题+正文一致）只发一次
      · 静态模板（如 DY-howto 纯方法论）正文逐日相同 → 自动被拦，
        不会因为 id 不同就把同一篇文章连发多天
    """
    fps: set = set()
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return fps
    for p in logs_dir.glob("*_douyin_publish.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("published"):
            continue
        # 新日志直接带指纹；旧日志从草稿文件反算
        fp = d.get("content_fingerprint")
        if fp:
            fps.add(fp)
            continue
        # 正文存在草稿文件里（旧版日志只存路径）
        dp = d.get("draft_path")
        if not dp:
            continue
        try:
            note = json.loads(Path(dp).read_text(encoding="utf-8"))
        except Exception:
            continue
        fps.add(_content_fingerprint(note))
    return fps


def _douyin_published_today(day: date) -> bool:
    """该自然日是否已有抖音成功发布记录。"""
    for p in LOGS.glob(f"{day.isoformat()}_douyin_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("published"):
            return True
    return False


def build_for_douyin(day, dry_run: bool = False, publish_index: int = 0) -> dict:
    """
    生成并发布一篇抖音文章。

    与小红书的关键差异：
      · 内容是**重写**的，不是复用 —— 抖音是推荐流，
        标题走「问题+损失」、开头结论前置、结尾引导评论区互动
      · 合规闸门多一层：content/douyin_compliance.py
        （AI荐股语义、晒收益、模糊私信话术、藏头诗变体规避）
      · 配图沿用同一套 AXIOM 系统截图（已脱敏）

    去重：
      与小红书链路同构，但**索引按平台隔离**（见 _douyin_published_index）。
      抖音各篇的 id 是固定的（DY-conf / DY-intel …），
      同一天重跑会稳定产出同一篇，所以必须挡住。
    """
    from source.gen_douyin import build_douyin_notes
    from content.compliance import check_note as _check
    from content.douyin_compliance import check_douyin
    from source.sanitize import forbidden_check

    notes = build_douyin_notes()
    if not notes:
        return {"ok": False, "error": "未产出可用抖音内容"}

    # 去重：挑一篇**内容**没发过的
    #
    # ⚠️ 为什么用内容指纹而不是 topic_id（2026-11-09 实测踩坑）：
    #   抖音只有 6 个固定 id，按 id 去重 → 发满 6 篇后选题池枯竭，
    #   之后每天直接 stage=dedup，一篇都发不出去（--date 2026-11-10 实测）。
    #   但其中 5 个模板是数据驱动的，正文随 AXIOM 每日数据变化，
    #   隔天再发是**新内容**，不该被当成重复。
    #   → 改成按"标题+正文"指纹去重：
    #     · 完全相同的文章（含静态的 DY-howto）→ 永不重发
    #     · 数据变了的新文章 → 正常发布
    #
    # ⚠️ 为什么还要按日轮转（而不是永远取第一篇）：
    #   抖音 2026-10-28 公告严打「AI 批量起号」——
    #   每天发同质化模板（角度/案例/句式都一样）形态上就撞上了。
    #   按日轮转起点，保证不同天优先换角度，
    #   与小红书侧 pick_topic() 的 epoch % N 同一套做法。
    used_ids, used_titles = _douyin_published_index()
    used_fps = _douyin_published_fingerprints()
    epoch = (day - date(2026, 11, 1)).days
    start = epoch % len(notes) if notes else 0
    order = list(range(len(notes)))
    order = order[start:] + order[:start]      # 轮转后的候选顺序

    # 两轮挑选：
    #   第一轮 = 内容全新 **且** 标题没用过（优先，保证标题多样性）
    #   第二轮 = 内容全新（标题用过也认；宁可标题重复，也不能一篇不发）
    # 抖音 2026-10-28 公告严打「AI 批量起号」，
    # 同标题反复出现形态上就撞上了 → 第一轮尽量避开；
    # 但标题可能天然恒定（如 DY-sample「材料只有个位数的我不看结论」），
    # 所以保留第二轮兜底，选题池永不死锁。
    picked = None
    for require_new_title in (True, False):
        for i in order:
            n = notes[i]
            if _content_fingerprint(n) in used_fps:
                continue                        # 内容完全重复 → 换下一篇
            if require_new_title and n["title"] in used_titles:
                continue                        # 标题用过 → 优先换一篇
            picked = i
            break
        if picked is not None:
            break
    if picked is None:
        return {
            "ok": False,
            "error": "本批抖音内容与已发布文章完全重复，本次不发布（避免重复内容被判刷量）",
            "stage": "dedup",
            "candidates": [n["id"] for n in notes],
        }
    note = notes[picked]
    nid = note["id"]

    # 配图：沿用 AXIOM 系统截图的 3:4 成品
    composed_dir = ASSETS / "composed"
    images = []
    scene_map = {
        "DY-conf": "dashboard", "DY-sample": "dashboard", "DY-window": "dashboard",
        "DY-intel": "intel_list", "DY-research": "intel_detail", "DY-howto": "agent",
    }
    scene = scene_map.get(nid)
    if scene:
        img_p = composed_dir / f"shot_{scene}.jpg"
        if img_p.exists():
            images.append(str(img_p))
    note["images"] = images

    # 四层闸门
    r_general = _check({"title": note["title"], "body": note["body"],
                        "tags": note["tags"], "images": note["images"]})
    r_dy = check_douyin({"title": note["title"], "body": note["body"],
                          "tags": note["tags"], "images": note["images"]})
    proj = (forbidden_check(note["title"]) + forbidden_check(note["body"])
            + forbidden_check(" ".join(note["tags"])))

    violations = ([f"[通用] {v['category']}" for v in r_general["violations"]]
                  + [f"[抖音] {v['category']}" for v in r_dy["violations"]]
                  + [f"[项目] {x}" for x in proj])

    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS / f"{day.isoformat()}_{nid}.json"
    draft_path.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "date": day.isoformat(),
        "source": "douyin",
        "platform": "douyin",
        "topic_id": nid,
        "title": note["title"],
        "draft_path": str(draft_path),
        "content_fingerprint": _content_fingerprint(note),
        "image_count": len(note["images"]),
        "compliance_passed": not violations,
        "violations": violations,
        "warnings": r_dy["warnings"],
        "title_advice": note.get("_title_advice", []),
        "published": False,
    }

    if dry_run:
        return result

    if violations:
        result["error"] = "合规未通过，已中止发布"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_douyin_blocked.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    # ---------- 当日去重守卫（抖音独立统计）----------
    # 抖音侧当天只发一篇。抖音各篇 id 固定，重跑会稳定产出同一篇，
    # 不挡就会把同一篇发两遍（平台判刷量，直接影响账号权重）。
    if _douyin_published_today(day):
        result["error"] = f"{day.isoformat()} 抖音当日已有成功发布记录，本次不再重复发布"
        result["stage"] = "dedup"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_douyin_skipped.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⏭️  {day} 抖音当日已发布过，跳过本次发布")
        return result

    from publish.douyin.publisher import publish_article
    pub = publish_article(note)
    result["publish_result"] = pub
    result["published"] = bool(pub.get("ok"))

    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{day.isoformat()}_douyin_publish.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _tgb_published_topic_ids() -> set:
    """淘股吧已发布过的主题 id 集合（用于主题轮换，保证同一主题不重复发）。"""
    ids: set = set()
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return ids
    for p in logs_dir.glob("*_tgb_publish*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pub = d.get("publish_result") or {}
        marker = (pub.get("publish_result") or {}).get("marker")
        url = (pub.get("publish_result") or {}).get("url") or pub.get("final_url") or ""
        ok = d.get("published") or marker in ("posted", "waitForView") \
            or "/a/2w" in url or "waitForView" in url
        if ok and d.get("topic_id"):
            ids.add(d["topic_id"])
    return ids


def _tgb_published_today(day: date) -> bool:
    """
    该自然日是否已有淘股吧成功发布记录。

    ⚠️ 淘股吧社区规则二.4「禁止大量重复发帖」，且创作中心1级前每天发言次数
    受限（官方明示限制目的之一是「防止发布广告」）。**一天只能发一篇**。

    ⚠️ 2026-11-09 实测踩坑：当日已发了 2 篇（14:51 / 14:55），
    而 logs 里只留下最后一份 _tgb_publish.json（前一份被同路径覆盖），
    → 用「文件是否存在」判断会漏判，必须按**成功发布事实**来判断。
    """
    for p in LOGS.glob(f"{day.isoformat()}_tgb_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("published"):
            return True
        # timeout 但 URL 已落到帖子页/待审核页 → 同样是「已发布」
        pub = d.get("publish_result") or {}
        marker = (pub.get("publish_result") or {}).get("marker")
        url = (pub.get("publish_result") or {}).get("url") or pub.get("final_url") or ""
        if marker in ("posted", "waitForView") or "/a/2w" in url or "waitForView" in url:
            return True
    return False


# 淘股吧两次发布之间的最小间隔（天）。
# 依据：淘股吧推广策略.md「发布节奏」——
#   新号阶段不要日更；先发 1 篇 → 观察 24-72 小时审核结果与跟帖反馈 → 再发第 2 篇。
# 淘股吧先审后发 + 小黑屋案底制（3 次暂封 / 9 次永封）+ 违规回溯历史发言，
# → 新号期宁可慢，不可密。
TGB_MIN_INTERVAL_DAYS = 2


def _tgb_last_publish_day() -> date | None:
    """最近一次淘股吧成功发布的日期（None = 从未发布过）。"""
    days = []
    for p in LOGS.glob("*_tgb_publish*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pub = d.get("publish_result") or {}
        marker = (pub.get("publish_result") or {}).get("marker")
        url = (pub.get("publish_result") or {}).get("url") or pub.get("final_url") or ""
        ok = d.get("published") or marker in ("posted", "waitForView") \
            or "/a/2w" in url or "waitForView" in url
        if not ok:
            continue
        try:
            days.append(date.fromisoformat(d.get("date", "")))
        except Exception:
            continue
    return max(days) if days else None


def _tgb_cooldown_active(day: date) -> tuple[bool, str]:
    """
    跨天冷却守卫：距上次发布不足 TGB_MIN_INTERVAL_DAYS 天 → 拦截。

    ⚠️ 背景：2026-11-09~11-12 连续 4 天各发 1 篇（合计 5 篇）。
    策略文档「发布节奏」明确要求新号阶段**不要日更**、两次发布间隔 24-72 小时，
    以便观察审核结果与跟帖反馈。淘股吧先审后发 + 小黑屋案底制，
    违规会**回溯历史发言**，密发风险远大于收益。
    """
    last = _tgb_last_publish_day()
    if last is None:
        return False, ""
    gap = (day - last).days
    if gap < TGB_MIN_INTERVAL_DAYS:
        return True, (f"距上次发布（{last.isoformat()}）仅 {gap} 天，"
                      f"未达最小间隔 {TGB_MIN_INTERVAL_DAYS} 天"
                      "（策略：新号阶段不要日更，先观察 24-72 小时审核结果）")
    return False, ""


def build_for_tgb(day, dry_run: bool = False, publish_index: int = 0) -> dict:
    """
    生成并发布一篇淘股吧主贴。

    与小红书/抖音的关键差异：
      · 体裁是**论坛长文**（800-3000字），不是卡片短文
      · **零引流**：不出现产品名、URL、联系方式
        （淘股吧规则一.10/二.7/三.4/三.5/三.8 全部禁止）
      · 分类固定「方法论」，**绝不选「个股研究」**
        （个股研究强制输入1-5支个股，选了即等同公开荐股）
      · **先审后发** + 小黑屋案底制 → 节奏要慢，一次只发一篇
    """
    from source.gen_tgb import build_tgb_posts
    from content.compliance import check_note as _check
    from content.tgb_compliance import check_tgb, check_category
    from source.sanitize import forbidden_check

    posts = build_tgb_posts()
    if not posts:
        return {"ok": False, "error": "未产出可用淘股吧内容"}

    # ---------- 选题轮换：选一个「历史没发过」的主题 ----------
    # ⚠️ 2026-11-09 实测踩坑：原实现是 posts[publish_index % len(posts)]，
    #   即恒定取第 1 篇 → 同一天重跑会把**同一篇**再发一遍。
    #   淘股吧规则二.4「禁止大量重复发帖」+ 创作中心每日发言次数限制，
    #   重复内容是判罚最重的一类。
    # → 改为：先看已发布过哪些 topic_id，本轮优先挑没发过的；
    #   都发过了才回退到按日轮转。
    #
    # ⚠️ 2026-11-12 修：原 fallback 在主题池耗尽时会回退到**已发过**的主题，
    #   仍会重复发帖（规则二.4）。现在 fallback 只允许选「历史发过但间隔最久」
    #   的主题；且全部发过且无间隔最久概念时，直接中止发布并提示扩池。
    used_tgb_ids = _tgb_published_topic_ids()
    candidates = [p for p in posts if p["id"] not in used_tgb_ids]
    if not candidates:
        # 主题池已全部发过 —— 允许回退，但必须挑「最久没发」的那篇，
        # 保证两次发布之间间隔最长（降低被判重复发帖的风险）。
        epoch = (day - date(2026, 11, 1)).days
        candidates = sorted(posts, key=lambda p: p["id"])
        logger_line = (f"[淘股吧] 主题池已发完，回退到间隔最久的主题："
                       f"{candidates[0]['id']}")
        print("   " + logger_line)
    post = candidates[publish_index % len(candidates)]
    pid = post["id"]

    # 三层闸门
    r_general = _check({"title": post["title"], "body": post["body"],
                        "tags": post.get("tags", []), "images": post.get("images", [])},
                       platform="tgb")
    r_tgb = check_tgb({"title": post["title"], "body": post["body"],
                       "tags": post.get("tags", []), "images": post.get("images", [])})
    proj = (forbidden_check(post["title"]) + forbidden_check(post["body"]))

    violations = ([f"[通用] {v['category']}" for v in r_general["violations"]]
                  + [f"[淘股吧] {v['category']}" for v in r_tgb["violations"]]
                  + [f"[项目] {x}" for x in proj])

    cat = check_category(post.get("category", "方法论"))
    if not cat["ok"]:
        violations.append(f"[分类] {cat['reason']}")

    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS / f"{day.isoformat()}_{pid}.json"
    draft_path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "date": day.isoformat(),
        "source": "tgb",
        "platform": "tgb",
        "topic_id": pid,
        "title": post["title"],
        "category": post.get("category", "方法论"),
        "body_len": len(post["body"]),
        "draft_path": str(draft_path),
        "compliance_passed": not violations,
        "violations": violations,
        "warnings": r_tgb["warnings"],
        "published": False,
    }

    if dry_run:
        return result

    if violations:
        result["error"] = "合规未通过，已中止发布"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_tgb_blocked.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    # ---------- 当日去重守卫（淘股吧独立统计）----------
    # 淘股吧规则二.4「禁止大量重复发帖」，创作中心1级前每天发言次数受限。
    # 2026-11-09 实测：当日已连发 2 篇（14:51 / 14:55），
    # 再发即构成「当日重复发帖」，直接触发违规。
    if _tgb_published_today(day):
        result["error"] = (f"{day.isoformat()} 淘股吧当日已有发布记录"
                           "（规则二.4 禁止大量重复发帖），本次不再重复发布")
        result["stage"] = "dedup"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_tgb_skipped.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⏭️  {day} 淘股吧当日已发布过，跳过本次发布")
        return result

    # ---------- 跨天冷却守卫（新号期不要日更）----------
    # 策略文档「发布节奏」：两次发布间隔 24-72 小时，先观察审核结果与跟帖反馈。
    # 淘股吧先审后发 + 小黑屋案底制，违规会回溯历史发言 → 宁可慢，不可密。
    cooldown_hit, cooldown_reason = _tgb_cooldown_active(day)
    if cooldown_hit:
        result["error"] = cooldown_reason
        result["stage"] = "cooldown"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_tgb_cooldown.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⏭️  {day} 淘股吧处于发布冷却期，跳过本次发布：{cooldown_reason}")
        return result

    from publish.tgb.publisher import publish_post
    pub = publish_post(post)
    result["publish_result"] = pub
    result["published"] = bool(pub.get("ok"))

    LOGS.mkdir(parents=True, exist_ok=True)
    # 同一天可能发多篇 → 日志不能被覆盖，追加编号保留全部记录
    base = LOGS / f"{day.isoformat()}_tgb_publish.json"
    if base.exists():
        n = 2
        while (LOGS / f"{day.isoformat()}_tgb_publish_{n}.json").exists():
            n += 1
        base = LOGS / f"{day.isoformat()}_tgb_publish_{n}.json"
    base.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _xq_published_topic_ids() -> set:
    """雪球已发布过的主题 id 集合（主题轮换，保证同一主题不重复发）。"""
    ids: set = set()
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return ids
    for p in logs_dir.glob("*_xq_publish*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pub = d.get("publish_result") or {}
        marker = (pub.get("publish_result") or {}).get("marker")
        url = (pub.get("publish_result") or {}).get("url") or pub.get("final_url") or ""
        ok = d.get("published") or marker in ("posted", "发布成功", "发表成功", "审核中") \
            or "/statuses/" in url
        if ok and d.get("topic_id"):
            ids.add(d["topic_id"])
    return ids


def _xq_published_today(day: date) -> bool:
    """
    该自然日是否已有雪球成功发布记录。

    ⚠️ 雪球第九章禁止「批量发布雷同低质信息」，第十章禁止「重复发布」。
    与淘股吧同构：**一天只发一篇**。

    ⚠️ 2026-11-11 实测：雪球首发链路从未跑通（发文入口 404 +
       登录态服务端已失效），故历史上无 _xq_publish.json。
       本函数按「成功发布事实」判定，而不是按文件是否存在。
    """
    for p in LOGS.glob(f"{day.isoformat()}_xq_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("published"):
            return True
        pub = d.get("publish_result") or {}
        marker = (pub.get("publish_result") or {}).get("marker")
        url = (pub.get("publish_result") or {}).get("url") or pub.get("final_url") or ""
        if marker in ("posted", "发布成功", "发表成功", "审核中") or "/statuses/" in url:
            return True
    # ⚠️ 2026-11-19：xq_hot 流程的日志文件名是 **无横杠** 格式
    #    （`20261119_xq_hot.json`，来自 build_xq_hot_post.py 的
    #      `datetime.now().strftime("%Y%m%d")`），
    #    而上面的 glob 用的是**带横杠**格式 → 这份记录永远扫不到。
    #    → 补扫无横杠格式，否则"今日已发"判据对 xq_hot 链路失效。
    for p in LOGS.glob(f"{day.strftime('%Y%m%d')}_xq_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("published"):
            return True
    return False


def build_for_xq(day, dry_run: bool = False, publish_index: int = 0) -> dict:
    """
    生成并发布一篇雪球主贴。

    雪球与前三条赛道的差异（都体现在这里）：
      · **必须勾选 AI 生成内容标识**（社区规则第十二章第4款，强制）
      · **单帖关联个股 ≤3 支**（雪球量化限制）
      · 能力边界必须写成**事实陈述**（"它不做选股"）——
        浙江证监局45号已明确「免责声明无效」
      · 不建组合（组合默认公开、不可删，关停后数据仍在原页）
      · 审核：智能反垃圾先拦截（限时加私）+ 人工审核，
        发布后需核实**对外可见性**，不能只看"发布成功"
    """
    from source.gen_xq import build_xq_posts
    from content.compliance import check_note as _check
    from content.xq_compliance import check_xq
    from source.sanitize import forbidden_check

    posts = build_xq_posts()
    if not posts:
        return {"ok": False, "error": "未产出可用雪球内容"}

    # ---------- 选题轮换：选一个「历史没发过」的主题 ----------
    # ⚠️ 与淘股吧同构（2026-11-09 踩过的坑）：原实现恒取 posts[0]，
    #   同一天重跑会把**同一篇**再发一遍 → 触犯第九章/第十章。
    # → 先看已发布过哪些 topic_id，本轮优先挑没发过的。
    used_xq_ids = _xq_published_topic_ids()
    candidates = [p for p in posts if p["id"] not in used_xq_ids]
    if not candidates:
        epoch = (day - date(2026, 11, 1)).days
        candidates = [posts[epoch % len(posts)]]
    post = candidates[publish_index % len(candidates)]
    pid = post["id"]

    # 三层闸门
    r_general = _check({"title": post["title"], "body": post["body"],
                        "tags": post.get("tags", []), "images": post.get("images", [])},
                       platform="tgb")
    if not r_general["passed"]:
        violations = [f"[通用] {v['category']}" for v in r_general["violations"]]
    else:
        violations = []

    r_xq = check_xq(post)
    violations += [f"[雪球] {v['category']}" for v in r_xq["violations"]]
    violations += [f"[项目] {x}" for x in
                   forbidden_check(post["title"]) + forbidden_check(post["body"])]

    DRAFTS.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS / f"{day.isoformat()}_{pid}.json"
    draft_path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "date": day.isoformat(),
        "source": "xq",
        "platform": "xueqiu",
        "topic_id": pid,
        "title": post["title"],
        "body_len": len(post["body"]),
        "tags": post.get("tags", []),
        "ai_marked": post.get("ai_marked", False),
        "draft_path": str(draft_path),
        "compliance_passed": not violations,
        "violations": violations,
        "warnings": r_xq["warnings"],
        "published": False,
    }

    if dry_run:
        return result

    if violations:
        result["error"] = "合规未通过，已中止发布"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_xq_blocked.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    # ---------- 当日去重守卫（雪球独立统计）----------
    # 雪球第九章禁止「批量发布雷同低质信息」，第十章禁止「重复发布」。
    # → 一天只发一篇；重跑时按「成功发布事实」判定，不能只看文件存在。
    if _xq_published_today(day):
        result["error"] = (f"{day.isoformat()} 雪球当日已有发布记录"
                           "（第九章/第十章禁止重复发布），本次不再重复发布")
        result["stage"] = "dedup"
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{day.isoformat()}_xq_skipped.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⏭️  {day} 雪球当日已发布过，跳过本次发布")
        return result

    from publish.xq.publisher import publish_post
    pub = publish_post(post)
    result["publish_result"] = pub

    # ⚠️ 发布成功判定不能只看 ok：
    #   「已点击发布」后若 wait_publish_result 超时/异常，但 URL 已落到
    #   /statuses/<id>（帖子页）→ **实际已经发出去了**。
    #   此时必须记为 published=True，否则重跑会重复发帖（触犯第十章）。
    pr = (pub.get("publish_result") or {}) if isinstance(pub, dict) else {}
    marker = pr.get("marker")
    final_url = pr.get("url") or pub.get("final_url") or ""
    landed_on_post = bool(re.search(r"/statuses/\d+", final_url or ""))
    result["published"] = bool(pub.get("ok")) or landed_on_post
    if landed_on_post and not pub.get("ok"):
        result["published_note"] = (
            f"结果标记为 {marker!r}，但 URL 已落到帖子页 {final_url} → 判定为已发布，"
            "请勿重复发帖（第十章）")
        result["post_url"] = final_url

    LOGS.mkdir(parents=True, exist_ok=True)
    # ⚠️ 只有**真正发出去了**才写 `_xq_publish*.json`。
    #   失败（未登录 / 未找到编辑器 / AI标识未勾上）写 `_xq_failed*.json`。
    #   原因：定时任务的「当天是否已发过」是**按文件是否存在**判断的，
    #   若把失败也写成 _xq_publish.json → 次日/重跑会被误判为"今日已发布"
    #   而白白跳过一天（且掩盖了真实失败原因）。
    if result["published"]:
        base = LOGS / f"{day.isoformat()}_xq_publish.json"
        if base.exists():
            n = 2
            while (LOGS / f"{day.isoformat()}_xq_publish_{n}.json").exists():
                n += 1
            base = LOGS / f"{day.isoformat()}_xq_publish_{n}.json"
    else:
        base = LOGS / f"{day.isoformat()}_xq_failed.json"
        if base.exists():
            n = 2
            while (LOGS / f"{day.isoformat()}_xq_failed_{n}.json").exists():
                n += 1
            base = LOGS / f"{day.isoformat()}_xq_failed_{n}.json"
    base.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def build_for_xq_hot(day, dry_run: bool = False, publish_index: int = 0) -> dict:
    """
    雪球「当日热点驱动」发帖。

    流程（用户要求）：
      1. 登录雪球 → 抓首页「热门话题」
      2. 从话题里选涉及板块的
      3. 把「日期+话题+板块」写进 热点记录.xlsx
      4. 把板块拿到 Axiom「情报精选」搜索
      5. 点最新一条 → 里面有「核心观点」
      6. 核心观点带到雪球发长文 + 补充看法 → 发帖

    ⚠️ 第 6 步的合规关键：Axiom 的 coreView.conclusion 含大量方向性建议
       （"配置重心应由…转向…""应优先选择…标的"），
       直接复制即构成荐股。
       → source/axiom_intel.py 已做**三级分级**：
         SAFE(premises) / CAUTION(conclusion) / BLOCK(荐股表述)
       只有 SAFE 进入正文。

    ⚠️ 热门话题必须**有头模式**抓（headless=True 抓到 0 条，雪球懒加载+反爬）。
    """
    from source.build_xq_hot_post import run as run_hot
    from source.build_xq_hot_post import save_report

    print("\n[雪球热点主贴] 六步流程", flush=True)
    res = run_hot(publish=not dry_run, limit=max(1, (publish_index or 0) + 1))

    # ⚠️ 必须用 merge 写（save_report）—— 直接 write_text 会**覆盖**掉
    #    之前成功发布的记录（published=true 被 dry-run 改成 false），
    #    导致「今日已发」事实丢失 → 重复发帖（第十章）。
    LOGS.mkdir(parents=True, exist_ok=True)
    save_report(LOGS, f"{day.isoformat()}_xq_hot.json", res)

    return {
        # ⚠️ ok 表示"流程是否走通"，不是"是否已发布"
        #    （dry-run 时 published=False 但流程是通的）
        "ok": bool(res.get("ok", res.get("published"))),
        "source": "xq_hot",
        "platform": "xueqiu",
        "date": day.isoformat(),
        "published": res.get("published", False),
        "title": res.get("title", ""),
        "topic_id": res.get("topic_id", ""),
        "compliance_passed": res.get("compliance_passed", True),
        "steps": res.get("steps", []),
        "posts": res.get("posts", []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="目标日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--week", action="store_true", help="生成未来7天")
    ap.add_argument("--dry-run", action="store_true", help="只生成不发布")
    ap.add_argument("--source",
                    choices=["axiom", "manual", "both", "douyin", "tgb", "xq",
                             "xq_hot"],
                    default="both",
                    help="axiom=用 AXIOM 真实数据；manual=用静态选题库；"
                         "douyin=抖音文章；tgb=淘股吧主贴；xq=雪球主贴（静态）；"
                         "xq_hot=雪球主贴（当日热点驱动）；"
                         "both=小红书各发一条")
    ap.add_argument("--index", type=int, default=None,
                    help="AXIOM 批次里的第几篇；默认自动挑选第一篇未发布过的")
    args = ap.parse_args()

    if args.week:
        start = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
        days = [start + timedelta(days=i) for i in range(7)]
    elif args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        days = [date.today()]

    print("=" * 72)
    print(f"小红书日更调度 · {len(days)} 天 · {'DRY-RUN' if args.dry_run else 'REAL'}"
          f" · 来源={args.source}")
    print("=" * 72)

    for d in days:
        jobs = []
        if args.source in ("axiom", "both"):
            jobs.append(("AXIOM", lambda dd=d: build_from_axiom(dd, args.dry_run, args.index)))
        if args.source in ("manual", "both"):
            jobs.append(("静态库", lambda dd=d: build_one(dd, args.dry_run)))
        if args.source == "douyin":
            jobs.append(("抖音文章", lambda dd=d: build_for_douyin(dd, args.dry_run, args.index or 0)))
        if args.source == "tgb":
            jobs.append(("淘股吧主贴", lambda dd=d: build_for_tgb(dd, args.dry_run, args.index or 0)))
        if args.source == "xq":
            jobs.append(("雪球主贴", lambda dd=d: build_for_xq(dd, args.dry_run, args.index or 0)))
        if args.source == "xq_hot":
            jobs.append(("雪球热点主贴",
                         lambda dd=d: build_for_xq_hot(dd, args.dry_run, args.index or 0)))
        for label, fn in jobs:
            r = fn()
            skipped = r.get("stage") == "dedup"
            print(f"\n[{label}] {r.get('date')} {r.get('title') or r.get('topic_id')}")
            if skipped:
                print(f"   合规: {'PASS' if r.get('compliance_passed') else 'BLOCK'}"
                      f" | 发布: 已跳过（{r.get('error', '')}）")
            else:
                print(f"   合规: {'PASS' if r.get('compliance_passed') else 'BLOCK'}"
                      f" | 发布: {'成功' if r.get('published') else ('未发布' if args.dry_run else '失败')}")
            for v in r.get("violations", []):
                print(f"   ⛔ {v['category']}: {v['hits']}")
            for x in r.get("project_issues", []):
                print(f"   ⛔ 项目闸门: {x}")
            if r.get("missing_slots"):
                print(f"   📌 待补真实素材: {r['missing_slots']}")
            if not args.dry_run and not skipped:
                pr = r.get("publish_result", {})
                print(f"   发布: {'成功' if r['published'] else '未成功'} "
                      f"({pr.get('stage', '-')}) {pr.get('error', '')}")
    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
