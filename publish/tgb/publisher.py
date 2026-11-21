"""
淘股吧主贴发布器。

【发帖页实测结构】2026-11-09，笔名 kwhzqy

URL: https://www.tgb.cn/topic/newTopic?davFlag=dv

表单字段：
  笔名:        kwhzqy（只读展示）
  标题:        input#subject              placeholder="请输入标题"
  主帖类型:     投票（勾选框）
  跟帖权限:     input#chese / #vip / #vip1
  内容:        div#tgbEditor
                 └ div#editor-page
                    └ div.ks-editor          （金科编辑器，870×402）
                       └ textarea#content_body （底层提交字段）
  看好股票:     input#recommendStock       placeholder="添加看好股票（限1支）"
  分类(选填):   研股 复盘 实盘 方法论 可转债 其他
                ⚠️ 页面明确写「选择研股分类必须添加股票或者关联看好股票」
                → 我们只选「方法论」，不碰看好股票
  话题:        input#talkName              placeholder="一个好的话题能带来更多的关注"
  提交按钮:    input#submit1 (submitY) / #submit2 (submitN) / #submit3
                cls: .input_b1 / .input_b2

【正文填充的关键】
ks-editor 是**自研编辑器**，不是 contenteditable，也不是 iframe。
但它底层有一个 `textarea#content_body`（宽高为 0，隐藏）——
这是提交表单真正带值的字段。
→ 最稳做法：**直接给 #content_body 赋值 + 触发 input/change 事件**，
  再用键盘输入兜底（编辑器可能把值同步回隐藏 textarea）。

【提交判定】
淘股吧是**先审后发**：提交后进入待审核，不会立刻出现在前台。
所以判据是「提交成功提示」而不是「列表里出现」。
"""

import random
import re
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
POSTS_DIR = ROOT / "content" / "for_tgb"

NEW_TOPIC = "https://www.tgb.cn/topic/newTopic?davFlag=dv"

# 分类：只用方法论（研股会强制要求绑定个股）
CATEGORY = "方法论"

TITLE_MAX = 60


def fill_subject(page, title: str) -> bool:
    """填标题（input#subject）。"""
    try:
        el = page.query_selector("#subject")
        if el and el.is_visible():
            el.click(timeout=4000)
            time.sleep(0.2)
            el.fill("")
            el.type(title[:TITLE_MAX], delay=30)
            time.sleep(0.3)
            return True
    except Exception:
        pass
    return False


def fill_body(page, body_text: str) -> bool:
    """
    填正文（金科编辑器 ks-editor）。

    策略：
      1. 点进编辑器，用键盘输入（保真度最好，能触发编辑器内部逻辑）
      2. 校验长度；不够则回退到直接给隐藏 textarea#content_body 赋值
    """
    # 1) 找编辑器容器
    editor = None
    for sel in ["#tgbEditor .ks-editor", "#tgbEditor", "#editor-page",
                ".ks-editor", "#content_body"]:
        try:
            el = page.query_selector(sel)
            if el:
                editor = el
                break
        except Exception:
            continue
    if editor is None:
        return False

    try:
        editor.click(timeout=5000)
        time.sleep(0.5)
    except Exception:
        pass

    # 2) 键盘输入
    ok = True
    for ch in body_text:
        try:
            page.keyboard.insert_text(ch)
        except Exception:
            ok = False
            break
        if ch == "\n":
            time.sleep(0.02)
    time.sleep(0.8)

    # 3) 校验：隐藏 textarea 是否已有内容
    got = ""
    try:
        got = page.evaluate("() => (document.querySelector('#content_body')||{}).value || ''")
    except Exception:
        got = ""
    if len(got.strip()) >= len(body_text.strip()) * 0.9:
        return True

    # 4) 回退：直接赋值隐藏 textarea
    try:
        page.evaluate(
            """(args) => {
                const [txt] = args;
                const ta = document.querySelector('#content_body');
                if (!ta) return;
                ta.value = txt;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                // 同步到编辑器可编辑区（如果它是 contenteditable）
                const ed = document.querySelector('#tgbEditor .ks-editor[contenteditable="true"]')
                        || document.querySelector('#editor-page [contenteditable="true"]');
                if (ed) {
                    ed.innerText = txt;
                    ed.dispatchEvent(new InputEvent('input', {bubbles: true}));
                }
            }""",
            [body_text],
        )
        time.sleep(0.8)
        return True
    except Exception:
        return ok


def select_category(page, category: str = CATEGORY) -> bool:
    """
    选择分类。

    实测结构（2026-11-09）：
      <span class="classify isyg"  data-type="1">研股</span>
      <span class="classify notyg" data-type="2">复盘</span>
      <span class="classify notyg" data-type="3">实盘</span>
      <span class="classify notyg" data-type="4">方法论</span>
      ...
      <input type="hidden" id="classify" name="type" value="4">

    ⚠️⚠️ **踩坑：class 里的 isyg / notyg 不是"是否选中"的可靠标记**
      点击「方法论」后，hidden input#classify 的 value 已经是 4（正确），
      但 span 的 class 仍然显示「研股=isyg、方法论=notyg」——
      也就是**样式类根本没跟着更新**。
      → 如果用 class 判断"有没有选中"，会永远误判成"选中了研股"。

    ✅ 正确判据：**读 hidden input#classify[name=type] 的 value**，
       再和目标分类的 data-type 比对。
       data-type: 研股=1 复盘=2 实盘=3 方法论=4 可转债=5 其他=6

    ⚠️ 绝不选「研股」（data-type=1）—— 选它会强制要求绑定 1 支个股。
    """
    TYPE_MAP = {"研股": 1, "复盘": 2, "实盘": 3,
                "方法论": 4, "可转债": 5, "其他": 6}

    def _current_type() -> int:
        try:
            return page.evaluate(
                """() => {
                    const el = document.querySelector('#classify[name="type"], input[name="type"]');
                    return el ? parseInt(el.value||'0') : 0;
                }""")
        except Exception:
            return 0

    target = TYPE_MAP.get(category)
    if not target:
        return False

    if _current_type() == target:
        return True

    # 1) 先尝试真实点击（保留平台自己的联动逻辑）
    try:
        page.evaluate(
            """(t) => {
                const el = [...document.querySelectorAll('span.classify')]
                    .find(e => e.getAttribute('data-type') === String(t));
                if (!el) return;
                el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                el.click();
            }""",
            target,
        )
        time.sleep(1.5)
    except Exception:
        pass

    if _current_type() == target:
        return True

    # 2) 点击没生效 → 直接改 hidden input + 同步 class
    try:
        page.evaluate(
            """(args) => {
                const [t, label] = args;
                const hid = document.querySelector('#classify[name="type"], input[name="type"]');
                if (hid) {
                    hid.value = String(t);
                    hid.dispatchEvent(new Event('change', {bubbles: true}));
                }
                // 同步视觉状态
                document.querySelectorAll('span.classify').forEach(e => {
                    const on = e.getAttribute('data-type') === String(t);
                    e.className = on ? 'classify isyg' : 'classify notyg';
                });
            }""",
            [target, category],
        )
        time.sleep(1.0)
        return _current_type() == target
    except Exception:
        return False


def fill_topic(page, topic: str) -> bool:
    """
    填话题。

    实测：
      input#talkName       搜索框（可见）
      input#talkNameHid    隐藏字段（真正提交的值）
      input#talkType      隐藏字段（话题类型）
    页面会提示「请选择合法的话题」如果没真正选上。

    ⚠️ 直接在搜索框 type 不算选中 —— 必须点选候选，
       否则 talkNameHid 会是 '-'，提交时报「请选择合法的话题」。
    """
    def _hidden_val() -> str:
        try:
            return page.evaluate(
                """() => {
                    const el = document.querySelector('#talkNameHid');
                    return el ? (el.value||'').trim() : '';
                }""") or ""
        except Exception:
            return ""

    def _input_val() -> str:
        try:
            return page.evaluate(
                """() => {
                    const el = document.querySelector('#talkName');
                    return el ? (el.value||'').trim() : '';
                }""") or ""
        except Exception:
            return ""

    # 已选过且匹配
    hid = _hidden_val()
    if hid and hid not in ("-", "") and topic in hid:
        return True

    try:
        el = page.query_selector("#talkName")
        if not el or not el.is_visible():
            return False
        el.click(timeout=4000)
        time.sleep(0.3)
        el.fill("")
        el.type(topic[:20], delay=45)
        time.sleep(1.8)
    except Exception:
        return False

    # 点选候选
    for el in page.query_selector_all("li,span,div,a"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if not t or len(t) > 20:
                continue
            if topic not in t:
                continue
            el.click(timeout=4000, force=True)
            time.sleep(1.5)
            hid = _hidden_val()
            if hid and hid not in ("-", ""):
                return True
        except Exception:
            continue

    # 没有候选，但输入框有值 → 平台可能接受自由话题
    if _hidden_val() and _hidden_val() != "-":
        return True
    return bool(_input_val()) and _hidden_val() != "-"


def dismiss_dialogs(page) -> list[str]:
    """
    关闭页面上弹出的提示框（如"当前话题正在参与比赛活动…"这类说明弹窗）。

    实测：点「原创提交」后会弹一个说明框，需要点「我知道了」才能继续。
    """
    dismissed = []
    for label in ("我知道了", "知道了", "确定", "好的", "关闭"):
        for el in page.query_selector_all("a,span,div,button"):
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip()
                if t != label:
                    continue
                el.click(timeout=4000, force=True)
                dismissed.append(label)
                time.sleep(1.2)
                break
            except Exception:
                continue
        if dismissed:
            break
    return dismissed


def submit(page) -> bool:
    """
    点击提交。

    实测按钮（注意 value 不是"发表"）：
      input#submit1 name=submitY value="原创提交"  cls=.input_b1  ← 主提交
      input#submit2 name=submitN value="暂存提交"  cls=.input_b2  ← 存草稿
      input#submit3                value="取消提交"  cls=.input_b2
    """
    for sel in ["#submit1", "input[name='submitY']", ".input_b1"]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.5)
                # ⚠️ submit1 是 <input type=button>，只靠 el.click() 可能不触发
                #    （实测点击后页面无反应、无请求）。
                #    → 必须用真实鼠标坐标点击。
                bb = el.bounding_box()
                if bb:
                    page.mouse.click(bb["x"] + bb["width"] / 2,
                                     bb["y"] + bb["height"] / 2)
                time.sleep(2.5)
                # JS 兜底
                try:
                    page.evaluate("() => { const b=document.querySelector('#submit1'); if(b) b.click(); }")
                except Exception:
                    pass
                time.sleep(2.0)
                dismissed = dismiss_dialogs(page)
                if dismissed:
                    time.sleep(1.5)
                return True
        except Exception:
            continue
    # 兜底：按文本
    for el in page.query_selector_all("button,a,input[type=submit],span,div"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or el.get_attribute("value") or "").strip()
            if t in ("原创提交", "发表", "发表帖子", "发布", "提交"):
                el.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.4)
                el.click(timeout=6000, force=True)
                time.sleep(2.5)
                dismiss_dialogs(page)
                return True
        except Exception:
            continue
    return False


def wait_submit_result(page, timeout: int = 60) -> dict:
    """
    等待提交结果。

    ⚠️⚠️ 淘股吧提交成功后**直接跳转到帖子页**，页面里没有"发表成功"提示：
      帖子页 URL 形态   /a/2wAbfN9pI2c      （已发布）
      待审核页 URL 形态 /topic/waitForView?topicID=xxx  （审核中）
    → **必须以 URL 特征判定成功**，不能靠找提示文字。

    ⚠️⚠️ **关键词不能包含页面常驻文字**（同类坑已踩三次）：
      · 抖音：「封面设置」常驻 → 误判"发布失败"
      · 淘股吧：「选择研股分类**必须添加股票**」常驻 → 误判"必须添加股票"
      → 错误关键词必须是只在报错时才出现的短语。
    """
    deadline = time.time() + timeout
    success_text = ["发表成功", "发布成功", "提交成功", "审核中", "等待审核",
                    "已发表", "发帖成功", "发表完成", "已提交"]
    # ⚠️ 只保留真正的报错短语，不含任何常驻说明文字
    error = ["发表失败", "发布失败", "请输入标题", "标题不能为空", "内容不能为空",
             "内容违规", "存在敏感", "请稍后重试", "发帖过于频繁",
             "您今天发帖次数已达上限", "含有违规内容", "禁止发布",
             "请选择合法的话题", "至少选择一个分类", "主题不能为空"]

    while time.time() < deadline:
        url = page.url
        body = ""
        try:
            body = page.inner_text("body")
        except Exception:
            pass

        # ---- 成功判据 1：已跳转到帖子页 ----
        # 帖子页 URL：/a/2wAbfN9pI2c  或  /topic/waitForView?topicID=...
        if re.search(r"/a/2w[A-Za-z0-9]+", url):
            return {"ok": True, "marker": "posted",
                    "detail": f"已跳转帖子页: {url}", "url": url}
        if "waitForView" in url:
            return {"ok": True, "marker": "waitForView",
                    "detail": f"已提交待审核: {url}", "url": url}

        # ---- 成功判据 2：提示文字 ----
        for m in success_text:
            if m in body:
                i = body.find(m)
                return {"ok": True, "marker": m,
                        "detail": body[max(0, i - 60): i + 100].replace("\n", " "),
                        "url": url}

        # ---- 失败判据 ----
        for m in error:
            if m in body:
                i = body.find(m)
                return {"ok": False, "marker": m,
                        "detail": body[max(0, i - 80): i + 110].replace("\n", " "),
                        "url": url}
        time.sleep(2)

    # ---- 超时后的**最终 URL 裁决**（2026-11-09 实测踩坑，重要）----
    # 淘股吧提交成功后是**整页跳转**到帖子页，跳转发生在最后一次轮询
    # 之后也会出现。实测 2026-11-09 14:55 那篇：
    #   循环 60s 内每 2s 都没命中 → 返回 marker=timeout
    #   但 timeout 返回那一刻 page.url 已经是 https://www.tgb.cn/a/2wAbfN9pI2c
    #   → 帖子其实**已发布成功**
    # 若把 timeout 当失败，上层会判 published=false 并触发重发，
    # 而实际帖子已经在站上 → 直接撞上社区规则二.4「大量重复发帖」。
    # → 超时后必须再看一次 URL：命中帖子页/待审核页即判定成功。
    final_url = page.url
    if re.search(r"/a/2w[A-Za-z0-9]+", final_url):
        return {"ok": True, "marker": "posted",
                "detail": f"超时后确认已跳转帖子页: {final_url}",
                "url": final_url, "via": "final_url_check"}
    if "waitForView" in final_url:
        return {"ok": True, "marker": "waitForView",
                "detail": f"超时后确认已提交待审核: {final_url}",
                "url": final_url, "via": "final_url_check"}
    return {"ok": None, "marker": "timeout", "url": final_url}


def publish_post(post: dict, session=None) -> dict:
    """发布一篇淘股吧主贴。post 需含 title / body / category。"""
    from publish.tgb.browser import TgbSession

    own = session is None
    log = []

    # ⚠️ TgbSession 必须先进入上下文（__enter__）才会创建 browser/page，
    #    直接 TgbSession() 的 page 是 None → AttributeError。
    if own:
        session = TgbSession(headless=False)
        session.__enter__()

    try:
        session.open()
        if not session.is_logged_in():
            return {"ok": False, "stage": "login", "error": "淘股吧未登录", "log": log}
        log.append("已登录")

        session.page.goto(NEW_TOPIC, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)
        if "sso.tgb.cn" in session.page.url:
            return {"ok": False, "stage": "login", "error": "登录态失效", "log": log}
        log.append("已进入发帖页")

        # 标题
        if fill_subject(session.page, post["title"]):
            log.append(f"标题已填: {post['title'][:40]}")
        else:
            return {"ok": False, "stage": "title", "error": "未找到标题输入框", "log": log}

        # 正文
        if fill_body(session.page, post["body"]):
            log.append(f"正文已填 {len(post['body'])} 字")
        else:
            return {"ok": False, "stage": "body", "error": "未找到正文编辑器", "log": log}

        # 分类（必须在正文之后、提交之前）
        cat_ok = select_category(session.page, post.get("category", CATEGORY))
        log.append(f"分类「{post.get('category', CATEGORY)}」: "
                   f"{'已选' if cat_ok else '❌ 未选中'}")
        if not cat_ok:
            return {"ok": False, "stage": "category",
                    "error": f"未能选中分类 {post.get('category', CATEGORY)}", "log": log}

        # 话题：**默认不填**
        # ⚠️ 实测：话题搜索框的候选项**全部是官方推广活动**
        #   （#优先曝光计划 / #流量加码计划 / #全站推送计划 / #板块轮动…），
        #   没有任何普通话题。
        #   → 一旦选中就等于挂了付费活动位（规则一.10 广告），而且会弹
        #     「当前话题正在参与比赛活动，活动要求发布主帖、字数大于600字…」
        #   → 个人号零引流策略下，**不填话题是最安全的选择**。
        topic = post.get("topic") or ""
        if topic:
            if fill_topic(session.page, topic):
                log.append(f"话题已填: {topic}")
            else:
                log.append(f"⚠️ 话题未设置（{topic}）")
        else:
            log.append("话题：不填（避免误入官方推广活动位）")

        time.sleep(1.5)

        # 发表
        if not submit(session.page):
            return {"ok": False, "stage": "submit_btn", "error": "未找到发表按钮", "log": log}
        log.append("已点击发表")

        # 提交后可能还有二次确认弹窗
        for _ in range(2):
            d = dismiss_dialogs(session.page)
            if d:
                log.append(f"已关闭弹窗: {','.join(d)}")
                time.sleep(1.5)
            else:
                break

        result = wait_submit_result(session.page)
        log.append(f"提交结果: {result.get('marker')}")
        if result.get("detail"):
            log.append(f"  提示: {result['detail'][:120]}")

        return {
            "ok": bool(result.get("ok")),
            "stage": "done",
            "title": post["title"],
            "log": log,
            "publish_result": result,
            "final_url": session.page.url,
        }
    finally:
        if own:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


def main():
    import json

    files = sorted(POSTS_DIR.glob("*.json"))
    if not files:
        print("❌ 没有草稿，请先运行: python source/gen_tgb.py")
        return
    print("可发布的草稿：")
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {d['id']}  [{d.get('category', CATEGORY)}]  {d['title']}  ({len(d['body'])}字)")


if __name__ == "__main__":
    main()
