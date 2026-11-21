"""
抖音文章（长图文）发布器。

【实测得到的编辑器结构】2026-11-04，账号 oliver_kwh

编辑器 URL：
  https://creator.douyin.com/creator-micro/content/post/article
      ?default-tab=5&enter_from=publish_page&media_type=article&type=new

入口路径（登录后从首页「作品发布」区点「发布文章」→「我要发文」）：
  首页 /creator-micro/home
    → 「发布文章  支持 8000 字图文」卡片
    → /creator-micro/content/upload?default-tab=5
    → 「我要发文」
    → /creator-micro/content/post/article?...

字段（左侧「基础信息」）：
  文章标题 *   input[placeholder*='文章标题']    0/30
  文章摘要      input[placeholder*='摘要']        0/30
  文章正文 *   .tiptap.ProseMirror                0/20000
  文章头图      点击上传图片
  AI 配图       AI生成
  封面设置 *   点击上传封面图（**必填**）
  添加话题 #   点击添加话题                        0/5
  选择配乐      点击添加合适作品风格音乐
  谁可以看      公开 / 好友可见 / 仅自己可见
  发布时间      立即发布 / 定时发布
  发布 · 暂存离开

⚠️ 三处与调研不符，已按实测修正：
  1. 正文上限是 **20000 字**，不是 8000
  2. **封面是必填**（带 * 号），不传会发布失败
  3. 摘要也是 **30 字**（不是长文本）

布局是三栏：左导航 / 中编辑区 / 右实时预览（手机样式）。
右侧预览会实时渲染标题与正文 —— 这让"发之前先看一眼"变得可行。

【正文用 tiptap】与小红书一致，所以文本输入逻辑可以完全复用。
"""

import random
import re
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
ARTICLES_DIR = ROOT / "content" / "for_douyin"

EDITOR_URL = ("https://creator.douyin.com/creator-micro/content/post/article"
              "?default-tab=5&enter_from=publish_page&media_type=article&type=new")
PUBLISH_ENTRY = "https://creator.douyin.com/creator-micro/content/upload?default-tab=5"

# ---- 实测选择器 ----
TITLE_SELECTORS = [
    "input[placeholder*='文章标题']",
    "input[placeholder*='标题']",
]
SUMMARY_SELECTORS = [
    "input[placeholder*='摘要']",
]
BODY_SELECTORS = [
    ".tiptap.ProseMirror",
    "[contenteditable='true'].ProseMirror",
    "div[contenteditable='true']",
]
# 话题输入框是弹层里的，不是主页面
TOPIC_SELECTORS = [
    "input[placeholder*='话题']",
    "input[placeholder*='搜索话题']",
    "[class*='semi-input'] input[placeholder*='话题']",
]

TITLE_MAX = 30
SUMMARY_MAX = 30
BODY_MAX = 20000
TOPIC_MAX = 5


def _first_visible(page, selectors, timeout: int = 8000):
    """返回第一个可见且可交互的元素（带等待）。"""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except Exception:
            continue
    return None


def _type_into(element, text: str, rng: random.Random, delay: float = 0.02) -> bool:
    """逐字输入，模拟真人。"""
    try:
        element.click()
        time.sleep(0.2)
    except Exception:
        pass
    for ch in text:
        try:
            element.type(ch, delay=delay * 1000)
        except Exception:
            return False
        if rng.random() < 0.05:
            time.sleep(rng.uniform(0.04, 0.15))
    return True


def fill_title(page, title: str, rng: random.Random) -> bool:
    title = title[:TITLE_MAX]
    el = _first_visible(page, TITLE_SELECTORS)
    if not el:
        return False
    try:
        el.click()
        time.sleep(0.2)
        page.keyboard.press("Control+A")
        time.sleep(0.12)
        page.keyboard.press("Delete")
    except Exception:
        pass
    return _type_into(el, title, rng, delay=0.05)


def fill_summary(page, summary: str, rng: random.Random) -> bool:
    summary = summary[:SUMMARY_MAX]
    el = _first_visible(page, SUMMARY_SELECTORS)
    if not el:
        return False
    try:
        el.click()
        time.sleep(0.2)
        page.keyboard.press("Control+A")
        time.sleep(0.12)
        page.keyboard.press("Delete")
    except Exception:
        pass
    return _type_into(el, summary, rng, delay=0.05)


def fill_body(page, body_text: str, rng: random.Random) -> bool:
    """
    填正文（tiptap）。

    与小红书不同：抖音正文上限 20000 字且**允许换行结构**，
    所以先逐字输入，失败再回退到 innerText 赋值。
    """
    el = _first_visible(page, BODY_SELECTORS)
    if not el:
        return False
    try:
        el.click()
        time.sleep(0.3)
    except Exception:
        pass

    # 逐字输入（保留换行）
    ok = _type_into(el, body_text, rng, delay=0.008)
    if ok:
        time.sleep(0.6)
        try:
            got = el.inner_text()
            if len(got.strip()) >= len(body_text.strip()) * 0.85:
                return True
        except Exception:
            return True

    # 回退：innerText 赋值 + 触发 input 事件
    try:
        page.evaluate(
            """(args) => {
                const [el, txt] = args;
                el.focus();
                el.innerText = txt;
                el.dispatchEvent(new InputEvent('input', {bubbles: true}));
            }""",
            [el, body_text],
        )
        time.sleep(0.6)
        return True
    except Exception:
        return False


def upload_images(page, image_paths: list[Path], which: str = "cover") -> int:
    """
    上传图片。

    ⚠️⚠️ 实测踩坑（四层坑叠加）：
      1. 编辑器里 **查不到任何 input[type=file]** ——
         `query_selector_all` 是 0，`locator("input[type=file]")` 也是 0。
      2. 点「点击上传封面图」后 file input **仍然不出现**。
      3. 真相是：抖音用的是**点击后弹系统文件选择框**的方案，
         没有可编程的 input 元素。
      4. `page.on("filechooser")` + 事件监听有时抓不到（时序不稳）。

    正解（实测唯一稳定的方式）：
        with page.expect_file_chooser(timeout=15000) as fc_info:
            page.get_by_text("点击上传封面图", exact=True).first.click()
        fc_info.value.set_files([...])

    关键点：
      · 用 `get_by_text(exact=True)` 而不是 query_selector_all ——
        Playwright 会自动滚动到元素并处理遮挡
      · 用 `expect_file_chooser` 上下文管理器而不是事件监听 —— 时序更稳

    上传成功后会弹出「编辑封面」面板（裁剪 + 花字），
    需要点确认才能落到编辑器里 —— 否则发布时提示"封面未设置"。

    which="cover" → 封面（必填）
    which="head"  → 文章头图
    """
    if not image_paths:
        return 0

    labels = {
        "cover": "点击上传封面图",
        "head": "点击上传图片",
    }.get(which, "点击上传图片")

    # 1) 定位上传入口（Playwright 会自动滚动）
    loc = page.get_by_text(labels, exact=True)
    try:
        if loc.count() == 0:
            return 0
        el = loc.first
        el.scroll_into_view_if_needed(timeout=6000)
        time.sleep(0.5)
    except Exception:
        return 0

    # 2) 用 expect_file_chooser 接管系统文件框
    try:
        with page.expect_file_chooser(timeout=15000) as fc_info:
            el.click(timeout=12000)
        fc_info.value.set_files([str(p) for p in image_paths])
    except Exception:
        return 0

    time.sleep(4.5)   # 等上传完成

    # 3) 处理「编辑封面」裁剪/花字面板
    confirm_cover_panel(page)
    return len(image_paths)


def sync_cover_from_head(page) -> bool:
    """
    点「同步头图为封面」。

    实测：抖音的封面设置区有一个「**同步头图为封面**」的按钮/开关。
    不点它，文章头图虽然上传成功，但封面区仍然是空的
    → 发布时报「封面未设置」。

    这是整个封面链路的最后一步，也是之前一直失败的原因。
    """
    for el in page.query_selector_all("div,span,button,a,label"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if "同步头图" not in t or len(t) > 24:
                continue
            el.scroll_into_view_if_needed(timeout=3000)
            time.sleep(0.3)
            el.click(timeout=6000, force=True)
            time.sleep(2.5)
            return True
        except Exception:
            continue
    return False


def confirm_cover_panel(page) -> bool:
    """
    关闭图片编辑/裁剪面板。

    ⚠️ 抖音有**两个不同的图片面板**，确认按钮文字不一样：
      · 文章头图 → 「图片编辑」对话框，确认按钮是 **「确定」**（红色）
      · 封面设置 → 「编辑封面」对话框，确认按钮是 **「完成」**（黑色）

    不点掉面板，它会一直**遮挡**后续操作（封面区、发布按钮都点不到），
    表现为发布时报"封面未设置"。

    所以：两个关键词都试，且优先用「确定」。
    """
    try:
        body = page.inner_text("body")
    except Exception:
        return False
    if not ("编辑封面" in body or "图片编辑" in body):
        return False

    def _click_label(label: str) -> bool:
        for el in page.query_selector_all("div,span,button,a"):
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip()
                if t != label:
                    continue
                bb = el.bounding_box()
                if not bb or bb["width"] < 30 or bb["height"] < 18:
                    continue
                el.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.3)
                el.click(timeout=6000, force=True)
                time.sleep(3.5)
                return True
            except Exception:
                continue
        return False

    # 1) 先试「确定」（图片编辑面板）
    if _click_label("确定"):
        return True

    # 2) 再试「完成」（编辑封面面板）
    if _click_label("完成"):
        return True

    # 3) 兜底：点最右下角的红色/黑色主按钮
    try:
        pos = page.evaluate(r"""
            () => {
                let best = null;
                document.querySelectorAll('div,span,button').forEach(el => {
                    const t = (el.textContent||'').trim();
                    if (t !== '确定' && t !== '完成') return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 40 || r.height < 20) return;
                    const cs = getComputedStyle(el);
                    const bg = cs.backgroundColor || '';
                    const m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                    const dark = m && (parseInt(m[1])+parseInt(m[2])+parseInt(m[3])) < 320;
                    const red = m && parseInt(m[1]) > 200 && parseInt(m[2]) < 110;
                    if (!dark && !red) return;
                    if (!best || r.x > best.x) best = {x: r.x + r.width/2, y: r.y + r.height/2};
                });
                return best;
            }""")
        if pos:
            page.mouse.click(pos["x"], pos["y"])
            time.sleep(3)
            return True
    except Exception:
        pass

    # 4) 最后兜底：ESC
    try:
        page.keyboard.press("Escape")
        time.sleep(2)
        return True
    except Exception:
        return False


def _click_text(page, texts: list[str], exact: bool = True) -> bool:
    """点击第一个文本匹配的元素（取最内层）。"""
    for el in page.query_selector_all("div,span,a,button,li"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if not t:
                continue
            if (t in texts) if exact else any(x in t for x in texts):
                el.click()
                return True
        except Exception:
            continue
    return False


def add_topics(page, topics: list[str], rng: random.Random) -> list[str]:
    """
    添加话题。

    抖音话题是**弹层选择器**：先点「添加话题」，再在弹层搜索并选中。
    不是小红书那种"正文里输入 # 词 + 回车"。
    """
    added = []
    for t in topics[:TOPIC_MAX]:
        # 点「添加话题」打开弹层
        if not _click_text(page, ["点击添加话题", "添加话题"], exact=False):
            break
        time.sleep(1.5)

        el = _first_visible(page, TOPIC_SELECTORS, timeout=4000)
        if not el:
            # 弹层里没有搜索框，尝试直接输入
            try:
                page.keyboard.type(t, delay=60)
            except Exception:
                pass
        else:
            try:
                el.click()
                time.sleep(0.2)
                el.type(t, delay=60)
            except Exception:
                pass
        time.sleep(1.2)

        # 选中第一个候选
        picked = False
        for el in page.query_selector_all("div,li,span"):
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
                if txt.startswith("#") and len(txt) <= 24 and t in txt:
                    el.click()
                    picked = True
                    break
            except Exception:
                continue
        if not picked:
            try:
                page.keyboard.press("Enter")
                picked = True
            except Exception:
                pass

        time.sleep(1.0)
        if picked:
            added.append(t)

        # 关闭弹层（点空白或取消）
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.8)
    return added


# 内容标识的合法文案。**不要**加入裸 "AI生成" ——
# 那是头图区的「AI 配图 / AI换图」功能，不是内容标识。
AI_DECL_LABELS = [
    "含有AI生成内容", "含AI生成内容", "AI生成内容", "内容由AI生成",
    "AI生成声明", "AI声明", "内容声明",
]


def _in_body_editor(el) -> bool:
    """元素是否在正文编辑器（tiptap）里。"""
    try:
        return bool(el.evaluate(
            "e => !!e.closest('.ProseMirror, .tiptap, [contenteditable=true]')"))
    except Exception:
        return False


def _in_preview(el) -> bool:
    """元素是否在右侧手机预览区（class 以 phone- 开头的容器）。"""
    try:
        return bool(el.evaluate(
            "e => !!e.closest('[class*=phone-], [class*=emptyHeadImage],"
            "[class*=player-container]')"))
    except Exception:
        return False


def declare_ai_content(page) -> bool:
    """
    勾选「含有AI生成内容」标识。

    抖音《内容标识使用规范》+ 网信办6类必选标签 —— 这是发布必经环节。

    ⚠️ 2026-11-06 修正一个真实 bug（此前造成过日志误报）：
    旧实现是 `"AI生成" in t and len(t) < 30` 的模糊匹配。
    兜底 label 里的裸 "AI生成" 会命中**正文段落**
    「我自己的做法是看任何AI生成的东西时，先找两样东西：」——
    于是对它执行了 el.click()，既没勾上任何标识，还会去点正文编辑器，
    而日志却记成「✅ AI 内容标识已勾选」（假成功）。
    （11-04/11-05 那两篇正文里没有「AI生成」四个字，才老实报了「未找到」，
      所以同一个 bug 表现成两种结果，极难察觉。）

    现在改为严格匹配：
      · 只接受「标识/声明」语义 label，不接受裸 "AI生成"
      · 命中元素必须是**纯标签**（自身文本 == label，长度 <= 20）
      · 排除正文编辑器与右侧预览区
      · 拿到 checkbox/switch 才算真勾上；否则只点纯 label
    """
    for label in AI_DECL_LABELS:
        for el in page.query_selector_all("label,span,div,p,button"):
            try:
                if not el.is_visible():
                    continue
                if _in_body_editor(el) or _in_preview(el):
                    continue
                t = (el.inner_text() or "").strip()
                # 必须是纯标签，不能是「……AI生成……某句话」这类容器/段落
                if t.replace(" ", "") != label.replace(" ", ""):
                    continue
                if len(t) > 20:
                    continue
                # 优先走 checkbox / switch
                try:
                    box = el.query_selector("input[type='checkbox']")
                    if box:
                        if not box.is_checked():
                            box.check(timeout=3000)
                        return True
                except Exception:
                    pass
                el.click(timeout=3000)
                return True
            except Exception:
                continue
    return False


def _visibility_radio(page, mode: str = "公开"):
    """
    在「谁可以看」分组里找到 mode 对应的 radio 元素。

    ⚠️ 2026-11-12 实测踩坑（回归测试抓到）：
    用「找含『谁可以看』的最小容器」来定位分组是**错的**——
    编辑器里那个容器可能同时包含**另一组** radio（发布时间：立即发布/定时发布），
    于是按「label 文本 == mode」去筛时找不到（因为它是 checkbox 不是 radio，
    或者它在更深层）。实测 `set_visibility` 直接抛「分组内未找到『公开』选项」。

    正解：**从 radio 自身向上找**含「谁可以看」的祖先（逐级最多 8 层），
    再用「label 文本 == mode」+「value」双重匹配定位。
    公开 / 好友可见 / 仅自己可见 的 value 分别是 0 / 2 / 1。
    """
    for el in page.query_selector_all("input[type='radio'], input[type='checkbox']"):
        try:
            lab = (el.evaluate(
                "e => (e.closest('label')?.innerText||'') || "
                "(e.parentElement?.innerText||'')") or "").strip()
            if lab != mode:
                continue
            in_group = el.evaluate(
                "e => { let n = e; for (let i=0;i<8 && n;i++){ n = n.parentElement;"
                "if (n && (n.innerText||'').includes('谁可以看')) return true; } return false; }")
            if not in_group:
                continue
            return el
        except Exception:
            continue
    return None


def set_visibility(page, mode: str = "公开") -> bool:
    """
    设置「谁可以看」。

    ⚠️ 2026-11-12 修正（真实缺陷）：
    旧实现是
        if mode == "公开": return True
        return _click_text(page, [mode], exact=True)
    —— 对「公开」**直接返回 True，从不点击 radio**，
    完全依赖编辑器的默认值。虽然 2026-11-12 实测默认值确实是「公开」
    （✔ checked=True value='0'），但「不点=赌默认值」在账号设置被改动后会
    静默发成非公开，而这类事故极难发现（11-04/11-05/11-07 三篇连续
    5 次执行都被误判为「发布成功」，实际对外不可见）。

    现在改为**显式点选**并回读校验：
      · 定位到「谁可以看」分组里 mode 对应的 radio → 点它的 LABEL（可见块）
      · 回读 checked 状态确认真的选上了
      · 若定位不到，抛异常让调用方感知（而不是静默假成功）
    """
    target = _visibility_radio(page, mode)
    if target is None:
        raise RuntimeError(f"「谁可以看」分组内未找到「{mode}」选项")

    # 已经是选中态 → 直接成功（幂等）
    try:
        if target.is_checked():
            return True
    except Exception:
        pass

    # 点它的 LABEL（可见块）；原生 input 常被样式隐藏（6x6），点它无效
    clicked = False
    try:
        handle = target.evaluate_handle("e => e.closest('label')")
        label_el = handle.as_element() if handle else None
        if label_el is not None:
            label_el.scroll_into_view_if_needed(timeout=3000)
            label_el.click(timeout=5000)
            clicked = True
    except Exception:
        clicked = False
    if not clicked:
        try:
            target.scroll_into_view_if_needed(timeout=3000)
            target.click(timeout=5000, force=True)
            clicked = True
        except Exception:
            pass
    if not clicked:
        raise RuntimeError(f"无法点击「{mode}」选项")

    time.sleep(1.5)

    # 回读校验
    try:
        if target.is_checked():
            return True
    except Exception:
        pass
    again = _visibility_radio(page, mode)
    if again is not None:
        try:
            if again.is_checked():
                return True
        except Exception:
            pass
    return False


def find_publish_button(page):
    """
    定位「发布」按钮**元素**。

    编辑器里既有「发布」也有「发布暂存离开」，
    必须精确匹配「发布」且面积较小（暂存离开那个更宽）。

    ⚠️ 实测踩坑（2026-11-04）：返回坐标再 page.mouse.click 是错的。
    「发布」按钮在**页面下半部分**，其 bounding_box 的 y ≈ 1620，
    而视口只有 1000 高 —— 坐标落在视口之外，
    mouse.click 点了空气，页面毫无反应，
    最终表现为"发布结果: timeout"。
    → 必须返回元素本身，用 el.click()（Playwright 会自动滚动到可视区）。
    """
    cands = []
    for el in page.query_selector_all("button,div[role='button'],span"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if t != "发布":          # 精确匹配，排除"发布暂存离开"
                continue
            bb = el.bounding_box()
            if not bb or bb["width"] < 40 or bb["height"] < 20:
                continue
            cands.append((bb["width"] * bb["height"], el))
        except Exception:
            continue
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def click_publish(page) -> bool:
    """
    点击「发布」。

    先把按钮滚进视口，再用元素点击 —— 保证点击真正落在按钮上。
    """
    el = find_publish_button(page)
    if not el:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=8000)
        time.sleep(1.2)
    except Exception:
        pass
    try:
        el.click(timeout=15000)
        return True
    except Exception:
        pass
    # 兜底：滚动后再用坐标点
    try:
        bb = el.bounding_box()
        if bb:
            page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            return True
    except Exception:
        pass
    return False


def handle_post_publish_dialog(page, timeout: int = 12) -> str:
    """
    点击「发布」后处理二次确认弹窗。

    抖音在点发布后可能弹出「内容声明 / AI 生成内容声明」确认框，
    不处理的话会一直停在弹窗上，表现为发布 timeout。

    策略：等几秒看有没有可见弹窗，
    里面有 AI 声明相关的勾选项就勾上，再点「确认/同意/确定/发布」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        # 已弹出的对话框容器
        dialog = None
        for sel in ["[role=dialog]", ".semi-modal-content", "[class*='dialog']",
                    "[class*='Dialog']", "[class*='modal-content']"]:
            try:
                for el in page.query_selector_all(sel):
                    if el.is_visible():
                        txt = (el.inner_text() or "").strip()
                        if txt and len(txt) < 2000:
                            dialog = el
                            break
            except Exception:
                continue
            if dialog:
                break

        if not dialog:
            return ""

        txt = (dialog.inner_text() or "").strip()
        # AI 声明相关 → 勾上
        if re.search(r"AI|生成|标识|合成", txt):
            for el in dialog.query_selector_all(
                    "input[type=checkbox], [role=checkbox], label, span, div"):
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                    if t and len(t) <= 30 and re.search(r"AI|生成内容|合成内容", t):
                        box = el.query_selector("input[type=checkbox]") or el
                        if box.evaluate("e => e.tagName") == "INPUT":
                            if not box.is_checked():
                                box.check(timeout=3000)
                                time.sleep(0.8)
                        else:
                            el.click(timeout=3000)
                            time.sleep(0.8)
                        break
                except Exception:
                    continue

        # 点确认类按钮
        for label in ("确认发布", "确定", "确认", "同意", "提交发布", "发布"):
            for el in dialog.query_selector_all("button, div[role=button], span"):
                try:
                    if not el.is_visible():
                        continue
                    if (el.inner_text() or "").strip() == label:
                        el.click(timeout=5000)
                        time.sleep(2.5)
                        return f"已处理弹窗并点击「{label}」"
                except Exception:
                    continue
        return f"检测到弹窗但未能自动关闭: {txt[:80]}"
    return ""


def wait_publish_result(page, timeout: int = 90) -> dict:
    """
    等待发布结果。

    ⚠️ 踩过的坑：错误关键词里**不能放"封面"**这种页面常驻字。
    编辑器左侧一直有「封面设置」四个字，只要它在 body 里，
    每次检查都会立刻误判成"发布失败" —— 实际发布可能早就成功了。
    错误关键词必须是**只会在报错时出现的短语**。
    """
    deadline = time.time() + timeout
    success_markers = ["发布成功", "作品发布成功", "已发布", "提交成功", "发布完成"]
    error_markers = ["发布失败", "请填写标题", "不能为空", "内容违规", "存在风险",
                     "审核不通过", "请稍后重试", "存在不可发布的内容", "违规内容",
                     "封面未设置", "请上传封面", "标题不能为空"]

    while time.time() < deadline:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        # URL 跳转 = 成功（跳回内容管理/首页）
        if "content/manage" in page.url:
            return {"ok": True, "marker": "url_change", "url": page.url}

        for m in success_markers:
            if m in body:
                return {"ok": True, "marker": m, "url": page.url}
        for m in error_markers:
            if m in body:
                i = body.find(m)
                return {"ok": False, "marker": m,
                        "detail": body[max(0, i - 80): i + 100].replace("\n", " "),
                        "url": page.url}
        time.sleep(2)

    return {"ok": None, "marker": "timeout", "url": page.url}


def _select_filter(page, filter_name: str, option: str) -> bool:
    """在内容管理页按筛选项筛选（如 体裁 → 长图文）。"""
    try:
        for el in page.query_selector_all("div,span,button,li"):
            try:
                if not el.is_visible():
                    continue
                if (el.inner_text() or "").strip() != filter_name:
                    continue
                el.click(timeout=5000)
                time.sleep(2)
                break
            except Exception:
                continue
        for el in page.query_selector_all("div,li,span,label"):
            try:
                if not el.is_visible():
                    continue
                if (el.inner_text() or "").strip() == option:
                    el.click(timeout=5000)
                    time.sleep(4)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _check_visibility(page, title: str) -> str:
    """
    核对目标作品在内容管理页里的**可见性**。

    ⚠️ 2026-11-12 新增（真实缺陷）：
    `verify_published` 原来只判断「标题是否出现在列表里」，
    于是「发出去但仅自己可见」的作品会被判成**发布成功**——
    11-04 / 11-05 / 11-07 三篇连续 5 次执行都被误报为成功，
    实际对外不可见（等于白发）。

    抖音的「非公开」角标换过两次（private-mark+「私密」
    → creator-ai-design-work-cover-private+「作品仅自己可见」），
    判定逻辑统一收敛到 publish/douyin/visibility.py 的多信号并集。

    返回形如「✅公开」「🔒非公开」；无法判定时返回「?」。
    """
    try:
        from publish.douyin.visibility import collect_works, visibility_label
        works = collect_works(page)
    except Exception as e:
        return f"? ({e})"

    key = (title or "")[:12]
    for w in works:
        txt = w.get("cardText") or ""
        if key and key not in txt:
            continue
        label = visibility_label(txt, w.get("cardClass"), w.get("descendantClasses"))
        dt = w.get("dt") or "?"
        st = w.get("status") or "?"
        return f"{label}（{dt}，状态={st}）"
    return "? (未在列表中找到该作品)"


def verify_published(session, title: str, timeout: int = 60) -> dict:
    """
    去内容管理页核实作品是否真的发布了。

    ⚠️ 为什么必须这么做：
    发布后抖音会给一个 toast，但 toast 可能一闪而过、
    或者页面不跳转导致判断不到 —— 靠提示文本猜不可靠。
    **内容管理页的列表是唯一权威依据。**

    ⚠️ 实测踩坑（2026-11-04）：内容管理页默认展示的是**视频作品**，
    文章（长图文）要通过「体裁 → 长图文」筛选才能看到。
    不加这个筛选，文章明明发成功了也会判定成"未在列表中找到"。
    """
    deadline = time.time() + timeout
    found = False
    detail = ""
    checked = False
    while time.time() < deadline:
        try:
            session.page.goto("https://creator.douyin.com/creator-micro/content/manage",
                              wait_until="domcontentloaded", timeout=90000)
            time.sleep(6)

            # 文章要走「体裁 → 长图文」筛选才看得到
            if not checked:
                checked = _select_filter(session.page, "体裁", "长图文")
                if checked:
                    time.sleep(4)

            body = session.page.inner_text("body")
            # 标题出现在列表里 = 发布成功
            if title and title[:12] in body:
                found = True
                idx = body.find(title[:12])
                detail = body[max(0, idx - 60): idx + 120].replace("\n", " ")
                # ⚠️ 标题在列表里 ≠ 对外可见。
                #    2026-11-12 实测：11-04/11-05/11-07 三篇都"发布成功"，
                #    但在内容管理页是非公开（作品仅自己可见）→ 等于白发。
                #    这里额外核对目标作品的可见性，把结论写进日志。
                vis = _check_visibility(session.page, title)
                detail += f" | 可见性: {vis}"
                break
            if "没有更多作品" in body:
                detail = "长图文筛选下没有作品"
                break
            m = re.search(r"作品\s*\((\d+)\)", body)
            detail = f"作品数: {m.group(1) if m else '?'}"
        except Exception:
            pass
        time.sleep(3)
    return {"ok": found, "detail": detail}


def publish_article(draft: dict, session=None) -> dict:
    """发布一篇抖音文章。"""
    from publish.douyin.browser import DouyinSession

    own_session = session is None
    session = session or DouyinSession(headless=False)
    log = []
    rng = random.Random()

    try:
        # ⚠️ 必须先进入上下文管理器，浏览器才会真正启动。
        #    DouyinSession.__enter__ 里做的是 launch_persistent_context，
        #    不进 __enter__ 的话 self.page / self.ctx 全是 None，
        #    后面 session.open() 直接 AttributeError: 'NoneType' has no 'goto'。
        #    （login.py 用的是 with 语句，所以 --check 能过，只有发布会炸。）
        if own_session:
            session.__enter__()
        session.open()
        if not session.is_logged_in():
            session.show_login_hint()
            if not session.wait_for_login(timeout=900):
                return {"ok": False, "stage": "login", "error": "抖音登录未完成", "log": log}
        log.append("已登录")

        # 进编辑器
        # ⚠️ 实测：直接 goto 编辑器深链会 ERR_CONNECTION_ABORTED（抖音风控）。
        # 正解：先访问首页建立会话，再走「作品发布 → 发布文章 → 我要发文」的正常路径。
        session.open()
        time.sleep(3)
        session.page.goto(PUBLISH_ENTRY, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)

        body_text = session.page.inner_text("body")
        if "文章标题" not in body_text:
            # 点「我要发文」进编辑器
            _click_text(session.page, ["我要发文"], exact=True)
            time.sleep(6)
            body_text = session.page.inner_text("body")

        if "文章标题" not in body_text:
            # 兜底：直接深链（此时已有会话，通常不会 abort）
            try:
                session.page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=90000)
                time.sleep(6)
                body_text = session.page.inner_text("body")
            except Exception:
                pass

        if "文章标题" not in body_text:
            return {"ok": False, "stage": "editor",
                    "error": "未能进入文章编辑器", "log": log}
        log.append(f"已进入文章编辑器: {session.page.url[:70]}")
        time.sleep(2)

        # 标题
        if not fill_title(session.page, draft["title"], rng):
            return {"ok": False, "stage": "title", "error": "未找到标题输入框", "log": log}
        log.append(f"标题已填: {draft['title'][:30]}")
        time.sleep(1.0)

        # 摘要
        summary = (draft.get("summary") or draft["body"][:28]).replace("\n", " ")
        if fill_summary(session.page, summary, rng):
            log.append(f"摘要已填: {summary[:30]}")

        # 正文
        if not fill_body(session.page, draft["body"], rng):
            return {"ok": False, "stage": "body", "error": "未找到正文编辑器", "log": log}
        log.append(f"正文已填 {len(draft['body'])} 字")
        time.sleep(2.0)

        # 图片：先传文章头图（抖音的主图，右侧预览会显示「有文章头图」样式），
        # 再传封面设置。两次都会弹面板，必须逐个关掉，否则面板会遮挡后续操作。
        images = [Path(p) for p in (draft.get("images") or [])]
        if images:
            n = upload_images(session.page, images[:1], which="head")
            log.append(f"文章头图已上传 {n} 张" if n else "⚠️ 文章头图未上传")
            time.sleep(1.5)

            n2 = upload_images(session.page, images[:1], which="cover")
            log.append(f"封面已上传 {n2} 张" if n2 else "⚠️ 封面未上传")
            time.sleep(1.5)

            # 确保没有遗留面板挡住
            confirm_cover_panel(session.page)

            # 关键一步：把头图同步为封面
            if sync_cover_from_head(session.page):
                log.append("已同步头图为封面")
            else:
                log.append("⚠️ 未找到「同步头图为封面」按钮")
            time.sleep(1.5)

        # 话题
        topics = draft.get("tags") or []
        added = add_topics(session.page, topics, rng)
        if added:
            log.append(f"话题已加: {' '.join(added)}")
        else:
            log.append("⚠️ 话题未添加（可能是弹层结构变化，需人工确认）")

        # AI 标识
        if declare_ai_content(session.page):
            log.append("AI 内容标识已勾选")
        else:
            log.append("⚠️ 未找到 AI 内容标识选项（发布前需人工勾选）")

        # 可见性：**显式**确认「谁可以看 = 公开」。
        # ⚠️ 2026-11-12 新增：以前这个函数从没被调用过（定义了但没接线），
        #    可见性完全靠编辑器默认值。默认值一旦被改动就会静默发成非公开，
        #    而「发出去但对外不可见」在内容管理页里很难第一眼发现
        #    （11-04/11-05/11-07 就是这样连续 5 次执行被误报成「发布成功」）。
        try:
            if set_visibility(session.page, "公开"):
                log.append("可见性已确认为「公开」")
            else:
                log.append("⚠️ 可见性未能确认为「公开」（需人工确认）")
        except Exception as e:
            log.append(f"⚠️ 可见性设置异常（需人工确认）: {e}")

        # 发布
        if not click_publish(session.page):
            return {"ok": False, "stage": "publish_btn", "error": "未找到发布按钮", "log": log}
        log.append("已点击发布")

        # 点击发布后可能弹出「AI 生成内容声明」等二次确认弹窗
        handled = handle_post_publish_dialog(session.page)
        if handled:
            log.append(f"发布后弹窗已处理: {handled}")

        result = wait_publish_result(session.page)
        log.append(f"发布结果: {result.get('marker')}")

        # 权威核实：去内容管理页确认作品真的在列表里
        verify = verify_published(session, draft["title"])
        log.append(f"内容管理核实: {'已确认发布' if verify['ok'] else '未在列表中找到'} "
                   f"({verify.get('detail', '')[:60]})")

        return {
            "ok": bool(result.get("ok")) or bool(verify["ok"]),
            "stage": "done",
            "title": draft["title"],
            "log": log,
            "publish_result": result,
            "verify": verify,
            "final_url": result.get("url", ""),
        }
    finally:
        if own_session:
            # 用 __exit__ 收尾：它会同时关掉浏览器上下文和 Playwright 驱动进程。
            # 只关 ctx 的话，playwright 的 driver 会残留成孤儿进程。
            try:
                session.__exit__(None, None, None)
            except Exception:
                try:
                    session.ctx.close()
                except Exception:
                    pass


def main():
    import json

    files = sorted(ARTICLES_DIR.glob("*.json"))
    if not files:
        print("❌ 没有草稿，请先运行: python source/gen_douyin.py")
        return
    print("可发布的草稿：")
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {d['id']}  {d['title']}  ({len(d['body'])}字)")


if __name__ == "__main__":
    main()
