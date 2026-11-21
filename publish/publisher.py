"""
小红书图文笔记发布器（Playwright + 本地 Chrome）

设计要点：
1. 使用本地已安装的 Chrome（channel="chrome"），不下载 Chromium
2. launch_persistent_context 持久化登录态，扫码一次长期复用
3. 发布前强制执行合规检查（compliance.py），不通过则拒绝发布
4. 自动勾选【笔记含 AI 合成内容】声明（2026-02 起平台强制要求）
5. 拟人化操作节奏：随机延时、逐字输入，避免被判定为自动化脚本

用法：
    python publisher.py --draft draft.json
    python publisher.py --draft draft.json --headless   # 不建议首次使用
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.compliance import ComplianceError, check_note  # noqa: E402
from publish.browser import (  # noqa: E402
    PUBLISH_URL, open_session, on_login_wall, publish_page_ready,
)

ROOT = Path(__file__).parent.parent
USER_DATA_DIR = ROOT / ".browser_profile"

# 拟人化节奏区间（秒）
TYPING_DELAY = (0.04, 0.12)
STEP_DELAY = (0.8, 2.2)


def human_delay(rng: random.Random, rng_range) -> None:
    time.sleep(rng.uniform(*rng_range))


def _type_into(element, text: str, rng: random.Random) -> None:
    """向元素逐字输入，模拟真人键盘节奏。"""
    element.click()
    try:
        element.press("Control+A")
        element.press("Delete")
    except Exception:
        pass
    human_delay(rng, (0.2, 0.5))
    for ch in text:
        element.type(ch, delay=int(rng.uniform(*TYPING_DELAY) * 1000))
        if rng.random() < 0.03:  # 偶尔停顿，模拟思考
            human_delay(rng, (0.3, 1.0))


def type_slowly(page, selector: str, text: str, rng: random.Random) -> bool:
    """按选择器逐字输入（保留旧接口）。"""
    try:
        element = page.wait_for_selector(selector, timeout=15000, state="visible")
    except PWTimeout:
        return False
    _type_into(element, text, rng)
    return True


def _active_tab(page) -> str | None:
    """当前激活的页签名称。"""
    for t in page.query_selector_all(".creator-tab"):
        try:
            cls = t.get_attribute("class") or ""
            if "active" in cls or "selected" in cls:
                return (t.inner_text() or "").strip()
        except Exception:
            continue
    return None


def select_text_tab(page) -> bool:
    """
    切到【上传图文】标签页。

    实测：页签结构为 div.creator-tab，且 PC 端与移动端各有一份，
    必须挑**可见**的那个点击，否则报 "element is outside of the viewport"。
    切换成功的标志是出现「上传图片」按钮。
    """
    # 已经切好了？
    if page.query_selector("text=上传图片"):
        return True

    deadline = time.time() + 15
    while time.time() < deadline:
        for t in page.query_selector_all(".creator-tab"):
            try:
                if (t.inner_text() or "").strip() != "上传图文":
                    continue
                if not t.is_visible():
                    continue
                t.scroll_into_view_if_needed(timeout=3000)
                t.click(timeout=5000, force=True)
                time.sleep(1.5)
                if page.query_selector("text=上传图片"):
                    return True
                break
            except Exception:
                continue
        time.sleep(0.8)
    return bool(page.query_selector("text=上传图片"))


# 编辑区选择器（实测小红书创作服务平台 2026-11 结构）
TITLE_SELECTORS = [
    "input[placeholder*='填写标题']",
    "input[placeholder*='标题']",
    "input.d-text",
]
BODY_SELECTORS = [
    ".tiptap.ProseMirror",
    ".tiptap",
    ".ProseMirror",
    "[contenteditable='true']",
]


def wait_editor_ready(page, timeout: int = 30000) -> bool:
    """
    等待标题/正文编辑区渲染完毕。

    实测：上传第一张图片后页面自动切到图文模式，
    标题 input(placeholder='填写标题会有更多赞哦') 与
    正文 div.tiptap.ProseMirror 才会出现。
    """
    for sel in TITLE_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            return True
        except PWTimeout:
            continue
    for sel in BODY_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=5000, state="visible")
            return True
        except PWTimeout:
            continue
    return False


def _first_visible(page, selectors: list[str]):
    """按优先级找第一个可见匹配元素。"""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=8000, state="visible")
            if el:
                return el
        except PWTimeout:
            continue
    return None


def upload_images(page, image_paths: list[Path], rng: random.Random) -> bool:
    """
    上传图片。

    实测流程：图文页签下有一个「上传图片」红色按钮，点击它才挂载
    file input；也可以直接对 input[type=file] 塞文件。
    两者都尝试，优先直接塞文件（更稳）。
    """
    # 先点「上传图片」按钮，让 file input 进入可写状态
    try:
        btn = page.wait_for_selector("text=上传图片", timeout=5000, state="visible")
        btn.click()
        human_delay(rng, (0.8, 1.5))
    except PWTimeout:
        pass

    # 找可用的 file input（优先可见的；隐藏的也能 set_input_files）
    input_el = None
    deadline = time.time() + 15
    while time.time() < deadline:
        cands = page.query_selector_all("input[type='file']")
        if cands:
            # 优先取 accept 含 image 的
            for el in cands:
                try:
                    acc = el.get_attribute("accept") or ""
                    if "image" in acc:
                        input_el = el
                        break
                except Exception:
                    continue
            if input_el is None:
                input_el = cands[-1]
            break
        time.sleep(0.5)

    if input_el is None:
        return False

    try:
        input_el.set_input_files([str(p) for p in image_paths])
    except Exception:
        return False
    human_delay(rng, (3.0, 6.0))
    return True


def fill_tags(page, tags: list[str], rng: random.Random) -> str:
    """
    追加话题标签。

    实测坑（两次踩中）：
      1. 在正文里输入 #xxx 再按回车 → 触发**平台推荐话题**下拉，
         回车选中的是 #howto用AI抢救一切 这类无关话题，污染笔记。
      2. 点「话题」按钮走搜索弹层 → 精确匹配不稳定，容易选错或选空。

    最终方案：把标签作为**纯文本**追加到正文末尾（#标签1 #标签2）。
    小红书会自行解析并索引这些话题，且不会触发任何推荐话题插入。
    这样既保证标签是我们想要的，又不会混入无关话题。

    返回实际写入的标签字符串，便于日志核对。
    """
    editor = None
    for sel in BODY_SELECTORS:
        try:
            editor = page.wait_for_selector(sel, timeout=8000, state="visible")
            if editor:
                break
        except PWTimeout:
            continue
    if editor is None:
        return ""

    tag_text = "  ".join(f"#{t}" for t in tags)
    try:
        editor.click()
        page.keyboard.press("Control+End")
        time.sleep(0.4)
        # 先清掉可能的残留空话题
        page.keyboard.press("Backspace")
        time.sleep(0.2)
        page.keyboard.type(f"\n{tag_text}", delay=45)
        human_delay(rng, (0.6, 1.2))
        # 不按回车 —— 避免触发推荐话题
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        return ""
    return tag_text


def declare_ai_content(page) -> bool:
    """
    勾选【笔记含 AI 合成内容】声明。

    平台 2026-02 起要求 AI 生成/辅助生成内容必须主动标识，未标识将被限制分发。

    实测结构：页面下方「内容设置」区 → 「添加内容类型声明」→
    弹层里是 div.d-grid-item，每个选项含 d-option-name 文本
    （虚构演绎 / 笔记含AI合成内容 / 内容包含营销广告 / 内容来源声明）。
    点击选项卡片即可勾选（自定义 checkbox，非原生 input）。
    """
    try:
        page.mouse.wheel(0, 3500)
        time.sleep(1.2)
    except Exception:
        pass

    # 1) 打开声明选择弹层
    opened = False
    for sel in ["text=添加内容类型声明", "text=添加内容声明", "text=内容类型声明"]:
        try:
            el = page.wait_for_selector(sel, timeout=5000, state="visible")
            el.scroll_into_view_if_needed(timeout=3000)
            el.click()
            time.sleep(1.3)
            opened = True
            break
        except PWTimeout:
            continue
    if not opened:
        return False

    # 2) 在弹层里点「笔记含AI合成内容」选项卡片
    for sel in [
        "div.d-grid-item:has-text('笔记含AI合成内容')",
        "div.d-grid-item:has-text('AI合成内容')",
        "div.d-option:has-text('AI合成内容')",
        "div.d-option-name:has-text('AI合成内容')",
        "text=笔记含AI合成内容",
        "text=AI合成内容",
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=3000, state="visible")
            el.scroll_into_view_if_needed(timeout=2000)
            el.click()
            time.sleep(0.9)
            # 关闭弹层
            for confirm in ["text=确定", "text=确认", "text=完成", "text=知道了"]:
                try:
                    btn = page.wait_for_selector(confirm, timeout=1000, state="visible")
                    btn.click()
                    time.sleep(0.6)
                    break
                except PWTimeout:
                    continue
            # 点掉弹层遮罩
            try:
                page.mouse.wheel(0, -100)
                page.keyboard.press("Escape")
            except Exception:
                pass
            return True
        except PWTimeout:
            continue
    return False


def find_red_publish_button(page, bottom_ratio: float = 0.22) -> tuple[float, float] | None:
    """
    用像素分析定位底部红色「发布」按钮。

    背景：小红书创作服务平台的底部操作栏是 **封闭 Shadow DOM** 的
    Web Component（<xhs-publish-btn>），JS 拿不到 shadowRoot，
    Playwright 的 text/locator 也无法穿透 ——
    所有基于 DOM 的定位方式全部失效。

    但按钮是**红色实心胶囊**，颜色是最稳定的特征。
    所以：截图 → 在底部区域找红色像素团 → 取团中心 → 换算成 CSS 坐标。

    deviceScaleFactor 默认为 1，截图像素坐标 = CSS 坐标。
    """
    try:
        import io

        from PIL import Image

        vp = page.viewport_size or {"width": 1440, "height": 900}
        shot = page.screenshot()
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        w, h = img.size

        y_start = int(h * (1 - bottom_ratio))
        px = img.load()

        # 收集红色像素
        pts = []
        for y in range(y_start, h):
            for x in range(w):
                r, g, b = px[x, y]
                if r > 190 and g < 110 and b < 110 and r - g > 90 and r - b > 90:
                    pts.append((x, y))

        if len(pts) < 200:
            return None

        # 简单聚类：按 x 分桶，只保留最大的连续桶群
        xs = sorted(p[0] for p in pts)
        clusters: list[list[int]] = [[xs[0]]]
        for x in xs[1:]:
            if x - clusters[-1][-1] <= 12:
                clusters[-1].append(x)
            else:
                clusters.append([x])

        best = max(clusters, key=len)
        x_lo, x_hi = best[0], best[-1]
        sel = [(x, y) for x, y in pts if x_lo <= x <= x_hi]
        if len(sel) < 200:
            return None
        ys = [p[1] for p in sel]
        cx = sum(p[0] for p in sel) / len(sel)
        cy = (min(ys) + max(ys)) / 2

        # 截图尺寸与视口尺寸的比例
        sx = vp["width"] / w
        sy = vp["height"] / h
        return (cx * sx, cy * sy)
    except Exception:
        return None


def click_publish(page) -> bool:
    """
    点击底部「发布」按钮。

    根因（连踩五次才找到）：底部操作栏是**封闭 Shadow DOM** 的
    Web Component（<xhs-publish-btn>），shadowRoot 为 null，
    Playwright 的 text 选择器、locator、elementFromPoint 全部拿不到
    里面的「发布」文字 —— DOM 层面的定位方式彻底失效。

    正解：靠**颜色**定位。按钮是红色实心胶囊，用像素分析找红色像素团，
    这不依赖任何 DOM 结构，页面改版也不易失效。
    """
    # 方式 1：像素分析找红色主按钮（最稳）
    pos = find_red_publish_button(page)
    if pos:
        page.mouse.click(pos[0], pos[1])
        return True

    # 方式 2：兜底 —— 点 <xhs-publish-btn> 组件内偏右位置
    try:
        for el in page.query_selector_all("xhs-publish-btn"):
            if not el.is_visible():
                continue
            bb = el.bounding_box()
            if not bb:
                continue
            page.mouse.click(bb["x"] + bb["width"] * 0.59, bb["y"] + bb["height"] * 0.48)
            return True
    except Exception:
        pass

    return False


# 抓取浮层 toast 的文本。
# 关键：点击「发布」后的拒绝提示（「因违反社区规范禁止发笔记」）
# 挂在 fixed/absolute 定位的浮层里，不一定出现在 inner_text("body") 中，
# 而且只停留约 8 秒就会自动消失 —— 抓不到就永远等不到，只能报 timeout。
_JS_COLLECT_TOASTS = """
() => {
    const out = [];
    document.querySelectorAll('*').forEach(el => {
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'absolute') return;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 150) return;
        const r = el.getBoundingClientRect();
        if (r.width < 60 || r.height < 18) return;
        if (r.width > 800 && r.height > 400) return;
        out.push(t);
    });
    return [...new Set(out)].join(' \\n ');
}
"""


def wait_publish_result(page, timeout: int = 45) -> dict:
    """
    等待发布结果。

    成功标志：出现「发布成功」提示，或跳转到笔记管理/成功页。
    失败标志：出现错误 toast（如「请填写标题」「发布失败」）。

    注意：平台侧的**账号级封禁提示**（如「因违反社区规范禁止发笔记」）
    只在点击发布后立即弹出、约 8 秒后消失，
    因此必须同时从 body 文本和浮层 toast 两处采集，
    否则会把「被平台拒发」误报成「超时」。
    """
    deadline = time.time() + timeout
    success_markers = ["发布成功", "提交成功", "笔记发布成功"]
    error_markers = ["发布失败", "请填写", "不能为空", "请输入标题", "内容违规",
                     "存在风险", "审核不通过", "请稍后重试",
                     # 账号级封禁：点击「发布」后**立刻**弹出，且约 8 秒后自动消失。
                     # 2026-11-05 实测：不加这两个 marker 会被误判成 timeout，
                     # 真实原因（社区规范封禁）就丢了。
                     "违反社区规范", "禁止发笔记", "社区规范禁止"]

    while time.time() < deadline:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        # toast 挂在浮层容器里，inner_text("body") 可能取不到 → 单独捞一次
        toast_text = ""
        try:
            toast_text = page.evaluate(_JS_COLLECT_TOASTS) or ""
        except Exception:
            toast_text = ""
        haystack = body + "\n" + toast_text

        for m in success_markers:
            if m in haystack:
                return {"ok": True, "marker": m, "url": page.url}
        for m in error_markers:
            if m in haystack:
                # 提取错误上下文
                idx = haystack.find(m)
                ctx_text = haystack[max(0, idx - 60): idx + 80].replace("\n", " ")
                return {"ok": False, "marker": m, "detail": ctx_text, "url": page.url}

        # URL 跳转也算成功
        if "note-manager" in page.url or "success" in page.url.lower():
            return {"ok": True, "marker": "url_change", "url": page.url}

        time.sleep(1.5)

    return {"ok": None, "marker": "timeout", "url": page.url}


def is_login_wall(page) -> bool:
    """判断当前是否停留在登录墙（委托给 browser 层统一实现）。"""
    return on_login_wall(page)


def _published_index() -> tuple[set, set]:
    """
    扫描 logs/ 目录，索引**已成功发布**的 topic_id 与标题。

    返回 (topic_ids, titles) 两个集合。

    为什么需要这个守卫：
    1. 小红书对同账号重复发布高度敏感 —— 同一篇内容发两次会被判刷量，
       直接影响账号权重。
    2. AXIOM 的选题是按数据日期生成的（如 AX-2026-11-02-conf），
       同一天重跑会**稳定地产出同一个 topic_id 和同一标题** ——
       也就是说"不小心重跑一次"就等于重复发布。
    3. 历史日志显示 2026-11-03 曾在 11-02 17:43 提前发布过一篇
       （topic=AX-2026-11-01-conf），次日再跑若不拦截就会撞车。

    守卫在**发布动作之前**拦截，属于硬闸门，不可绕过。
    """
    topic_ids: set = set()
    titles: set = set()
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return topic_ids, titles
    for p in logs_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("published"):
            continue
        tid = d.get("topic_id")
        if tid:
            topic_ids.add(tid)
        title = d.get("title")
        if title:
            titles.add(title)
    return topic_ids, titles


def _account_blocked(max_age_days: int = 7) -> bool:
    """
    账号是否已被平台限制发文。

    2026-11-05 实测：平台因「利用AI托管进行发文/互动」处置账号后，
    点击「发布」立刻弹出「因违反社区规范禁止发笔记」并拒绝落库，
    且**换任何选题都一样被拦**（已用 benign 选题做过对照验证），
    属账号级限制而非内容问题。

    发布页本身不显示任何横幅，所以只能靠历史日志里是否已出现
    `stage == "account_blocked"` 来判断 —— 封过一次就别再反复尝试，
    避免继续触发更重的处置，也让日志如实反映拦截原因。

    只看最近 `max_age_days` 天的记录：账号可能已被人工解禁，
    届时删掉对应的封禁日志（或把 max_age_days 调小）即可自动恢复，
    不需要改代码。
    """
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return False
    cutoff = time.time() - max_age_days * 86400
    for p in logs_dir.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pr = d.get("publish_result") or {}
        if pr.get("stage") == "account_blocked":
            return True
        if "违反社区规范" in str(pr.get("publish_result", {}).get("marker", "")):
            return True
    return False


def publish(draft: dict, headless: bool = False) -> dict:
    """执行发布。返回结果字典。"""
    rng = random.Random()

    note = {
        "title": draft["title"],
        "body": draft["body"],
        "tags": draft.get("tags", []),
        "images": draft.get("images", []),
    }

    # ---------- 去重闸门（同一篇/同一标题不得重复发布） ----------
    topic_id = draft.get("id") or ""
    used_ids, used_titles = _published_index()
    if topic_id and topic_id in used_ids:
        return {
            "ok": False, "stage": "dedup",
            "error": f"该选题已发布过，拒绝重复发布: {topic_id}",
            "hint": "同一篇内容重复发布会被平台判为刷量。"
                    "如需发新内容，请等待 AXIOM 产出新数据，或换用 --index 选另一篇。",
        }
    if note["title"] and note["title"] in used_titles:
        return {
            "ok": False, "stage": "dedup",
            "error": f"该标题已发布过，拒绝重复发布: {note['title']}",
            "hint": "同标题内容重复发布会被平台判为重复内容/刷量。",
        }

    # ---------- 合规闸门（不通过直接拒绝发布） ----------
    try:
        report = check_note(note)
    except ComplianceError as e:
        return {"ok": False, "stage": "compliance", "error": str(e)}
    if not report["passed"]:
        return {"ok": False, "stage": "compliance", "error": report["violations"]}

    images = [Path(p) for p in note["images"]]
    missing = [str(p) for p in images if not p.exists()]
    if missing:
        return {"ok": False, "stage": "preflight", "error": f"图片不存在: {missing}"}

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        session = open_session(headless=headless, p=p)
        page = session.page
        try:
            log = [f"浏览器模式: {session.mode}"]
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            human_delay(rng, (3.0, 5.0))

            # 登录态检查：创作服务平台的登录页 URL 与发布页相同，
            # 必须靠页面元素特征判断，不能只看 URL。
            if is_login_wall(page):
                return {
                    "ok": False,
                    "stage": "login",
                    "error": "登录态失效：请先运行 python publish/login.py 完成登录",
                    "hint": "创作服务平台需「手机号 + 短信验证码」登录；"
                            "或用 --remote-debugging-port 启动日常 Chrome 后由脚本接管",
                    "log": log,
                }
            log.append("已通过登录校验")

            # ---------- 账号级发文限制预检 ----------
            # 2026-11-05 实测：账号被平台限制发文后，发布页本身**不会**显示
            # 任何横幅或提示，只有点「发布」才弹「因违反社区规范禁止发笔记」。
            # 所以预检靠「历史发布日志里是否已有 account_blocked 记录」来判断：
            # 一旦封过号，就不要再走完整填表流程（徒增被处置风险），
            # 直接如实报告拦截原因。
            if _account_blocked():
                log.append("⛔ 预检拦截：该账号此前已被平台限制发文（account_blocked）")
                return {
                    "ok": False,
                    "stage": "account_blocked",
                    "error": "账号已被平台限制发文（历史日志记录了 account_blocked）",
                    "hint": "需人工在 App 内完成申诉/解禁后，再恢复自动发布。"
                            "本次未提交任何内容，不会产生新的重复笔记。",
                    "log": log,
                }

            if not select_text_tab(page):
                log.append("页签未显式切换（上传图片后会自动进入图文模式）")

            if not upload_images(page, images, rng):
                return {"ok": False, "stage": "upload", "error": "图片上传失败", "log": log}
            log.append(f"已上传 {len(images)} 张图片")

            # 新手引导弹窗（"图片可以编辑啦，快来试试吧"）会遮挡页面
            human_delay(rng, (2.0, 3.5))
            for sel in ["text=我知道了", "text=知道了"]:
                try:
                    el = page.wait_for_selector(sel, timeout=1500, state="visible")
                    el.click()
                    log.append("已关闭引导弹窗")
                    human_delay(rng, (0.5, 1.0))
                    break
                except PWTimeout:
                    continue

            # 上传图片后，标题/正文编辑区才会渲染出来
            if not wait_editor_ready(page):
                return {"ok": False, "stage": "editor", "error": "标题/正文编辑区未就绪", "log": log}
            log.append("编辑区已就绪")

            title_el = _first_visible(page, TITLE_SELECTORS)
            if title_el is None:
                return {"ok": False, "stage": "title", "error": "标题输入框未找到", "log": log}
            _type_into(title_el, note["title"], rng)
            log.append(f"标题已填入: {note['title']}")

            body_el = _first_visible(page, BODY_SELECTORS)
            if body_el is None:
                return {"ok": False, "stage": "body", "error": "正文编辑器未找到", "log": log}
            _type_into(body_el, note["body"], rng)
            log.append(f"正文已填入 {len(note['body'])} 字")

            written = fill_tags(page, note["tags"], rng)
            log.append(f"标签已追加: {written or '(失败)'}")

            ai_declared = declare_ai_content(page)
            log.append(f"AI 合成内容声明: {'已勾选' if ai_declared else '未找到勾选入口（需人工确认）'}")

            human_delay(rng, (1.5, 3.0))
            if not click_publish(page):
                return {"ok": False, "stage": "publish", "error": "发布按钮未找到", "log": log}
            log.append("已点击发布")

            result = wait_publish_result(page)
            log.append(f"发布结果: {result.get('marker')}")
            if result.get("detail"):
                log.append(f"错误详情: {result['detail']}")

            # ---------- 账号级封禁识别 ----------
            # 2026-11-05 实测：账号因「利用AI托管进行发文/互动」被平台处置后，
            # 点击「发布」会**立刻**弹出「因违反社区规范禁止发笔记」，
            # 约 8 秒后自动消失 —— 表现为内容全部填好、URL 不跳转、笔记不落库。
            # 这是**账号级**限制（换任何选题都一样被拦），不是内容问题。
            # 一旦命中，必须明确报出原因，避免被误读成「超时」或「内容违规」，
            # 也避免后续继续反复尝试触发更重的处置。
            if result.get("marker") and "违反社区规范" in str(result.get("marker")):
                log.append("⛔ 账号被平台限制发文（非内容问题）：需人工处理后再发布")
                return {
                    "ok": False,
                    "stage": "account_blocked",
                    "title": note["title"],
                    "ai_declared": ai_declared,
                    "error": "因违反社区规范禁止发笔记 —— 账号级发文限制",
                    "detail": result.get("detail", ""),
                    "hint": "平台已因「利用AI托管进行发文/互动」处置该账号的历史笔记，"
                            "并限制继续发文。需人工在 App 内申诉/解禁后，再恢复自动发布。",
                    "log": log,
                    "publish_result": result,
                    "final_url": page.url,
                }

            page.screenshot(path=str(ROOT / "assets" / "_published_last.png"))

            return {
                "ok": bool(result.get("ok")),
                "stage": "done",
                "title": note["title"],
                "ai_declared": ai_declared,
                "log": log,
                "publish_result": result,
                "final_url": result.get("url", ""),
            }
        finally:
            session.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True, help="draft json 路径")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--force", action="store_true", help="跳过合规检查（仅调试用，不建议）")
    args = ap.parse_args()

    draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
    if args.force:
        draft["_skip_compliance"] = True

    result = publish(draft, headless=args.headless)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
