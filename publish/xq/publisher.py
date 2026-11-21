"""
雪球主贴发布器。

【编辑器结构（待登录后实测确认）】
雪球写文章入口候选：
  https://xueqiu.com/statuses/create
  雪球 App / 首页「写文章」

【雪球发帖的关键约束（必须遵守，否则会被删帖/禁言/封号）】
1. **必须勾选「本文包含AI生成内容」**（社区规则第十二章第4款，强制）
2. **单帖关联个股 ≤3 支**
3. 不得出现：个股名称/代码、买卖点、目标价、止损位、收益率、
   交割单/持仓截图、站外联系方式、产品名、暗语
4. 不得"回复关键词领取资料"（第八章第6款）
5. 能力边界必须写成**事实陈述**（"它不做选股"），免责条款无效
6. **不建组合**（组合默认公开、不可删）

【发布后的验证】
雪球是「智能反垃圾先拦截 + 人工审核 + 举报复核」：
  命中风控的内容会先**限时加私（限制展示）**，不进粉丝信息流。
→ 发布后必须核实是否"对外可见"，不能只看"发布成功"。
"""

import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
POSTS_DIR = ROOT / "content" / "for_xq"

# ⚠️ 用**短帖（主贴）编辑器** —— 实测 2026-11-18 唯一能真正发出去的路径
#
# 【两条路径的实测对比】
# A. 长文编辑器 mp.xueqiu.com/writeV2/
#    · 有「创作声明」下拉（含"本文包含AI生成内容"）✅
#    · 点「发布」→ POST /xq/statuses/draft/save.json  ← **只存草稿！**
#      原因：长文发布需先**申请专栏**（页面有"申请专栏"入口），新号发不了
#    → 结论：长文路径现阶段不可用
#
# B. 短帖（主贴）编辑器 xueqiu.com/ 首页「发帖」
#    · 点「发帖」→ 底部展开 .lite-editor
#    · 点「长文」工具栏按钮 → 切长文模式（type=2）
#    · 点「发布」→ POST /statuses/update.json → 返回帖子 id ✅ **真正发布**
#    · ⚠️ 短帖编辑器**没有 AI 标识选项**（只有长文编辑器有）
#      → 缓解：正文末尾明确写「本文由 AI 辅助整理」（自声明，降低"隐匿标识"风险）
#
# 【成功判据】监听 POST /statuses/update.json 的响应，取 id
CREATE_URL = "https://xueqiu.com/"

TITLE_MAX = 30          # 雪球标题约 30 字上限
BODY_MAX = 20000

# ⚠️ 2026-11-11 实测：`https://xueqiu.com/statuses/create` 返回雪球 **404 页**
#    （"抱歉！没有找到你想要的内容"），根本不是编辑器。
#    → 发帖入口必须**从首页动态发现**（右上角「写文章」下拉 → 真实 href），
#      下面的 CREATE_URL 只作为最后兜底。
CREATE_URL_CANDIDATES = [
    "https://xueqiu.com/statuses/create",
    "https://xueqiu.com/statuses/create.json",
    "https://xueqiu.com/write",
    "https://xueqiu.com/newarticle",
    "https://xueqiu.com/compose",
]


def discover_create_url(session) -> str | None:
    """
    从首页动态发现真实的「写文章」入口 URL。

    ⚠️ 为什么必须动态发现（2026-11-11 实测）：
      · `/statuses/create` → 404
      · `/write` → 403 且被踢回首页
      · `/newarticle`、`/compose` → 404
      → 硬编码 URL 一律失效，只能走首页右上角的下拉入口。

    返回真实 URL；找不到则返回 None（由调用方报错并截图）。

    ⚠️⚠️ **2026-11-18 实测修正**：
    1. 首页的入口文案是「**长文**」（`\ue646\n长文` → mp.xueqiu.com/write/），
       原正则 `/写|发|创作|投稿/` **匹配不到"长文"** → 入口被跳过 → 报"未找到发文入口"。
       → 正则已补上「长文」。
    2. 更稳的做法：**直接用实测可用的常量 URL**（动态发现做兜底）。
    3. 短帖入口 `a.lite-editor__submit` 在 xueqiu.com/statuses/create，
       但**短帖页没有 AI 生成内容标识** → 必须用长文编辑器。
    """
    # 0) 首选：实测可用的长文编辑器入口
    if CREATE_URL:
        return CREATE_URL

    page = session.page
    try:
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)
    except Exception:
        pass

    # 1) 找页面上所有「写/发/创作」入口元素
    cands = page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('a[href],div[role=button],span,button,li,div,i').forEach(el => {
        const t = (el.innerText||'').trim();
        const h = el.getAttribute('href')||'';
        if (!t || t.length > 10) return;
        if (!/写|发|创作|投稿|长文/.test(t)) return;
        if (/验证码|登录|注册|下载|发文章按钮|发布文章/.test(t)) return;
        const r = el.getBoundingClientRect();
        if (r.width===0||r.height===0) return;
        out.push({t:t, h:h, cls:(el.className||'').toString().slice(0,50),
                  x:Math.round(r.x), y:Math.round(r.y)});
      });
      return out.slice(0,25);
    }""")

    # 2) 优先带 href 的
    for c in cands:
        h = c.get("h") or ""
        if h and ("write" in h.lower() or "create" in h.lower()
                  or "article" in h.lower() or "post" in h.lower()):
            url = h if h.startswith("http") else "https://xueqiu.com" + (
                h if h.startswith("/") else "/" + h)
            print(f"   发现发文入口链接: {c['t']!r} -> {url}", flush=True)
            return url

    # 3) 点击文字入口
    for label in ("写文章", "发文章", "写文章 ", "创作", "投稿"):
        try:
            loc = page.locator(f"xpath=//*[normalize-space(text())='{label.strip()}']").first
            if not loc.count():
                continue
            loc.click(timeout=5000, force=True)
            time.sleep(8)
            url = page.url
            if "statuses/create" not in url and "xueqiu.com" not in url:
                continue
            if url.startswith("https://xueqiu.com/statuses/create") or \
               url.startswith("https://www.xueqiu.com/statuses/create"):
                # 已知该 URL 返回 404 → 判定失败，继续试下一个
                body = page.inner_text("body")
                if "没有找到你想要的内容" in body or "404" in page.title():
                    continue
            print(f"   点击入口 {label.strip()!r} -> {url}", flush=True)
            return url
        except Exception:
            continue

    # 4) 兜底遍历候选 URL（跳过已知 404 的）
    for u in CREATE_URL_CANDIDATES:
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=40000)
            time.sleep(5)
            body = page.inner_text("body")
            if "没有找到你想要的内容" in body or "Whitelabel" in body:
                continue
            if "标题" in body or "写文章" in body:
                print(f"   候选 URL 命中: {u}", flush=True)
                return u
        except Exception:
            continue
    return None


def _type_into(element, text: str, rng: random.Random, delay: float = 0.02) -> bool:
    """逐字输入（真实键盘事件，雪球对 isTrusted 敏感）。"""
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
            time.sleep(rng.uniform(0.04, 0.14))
    return True


def find_editor(page) -> dict:
    """
    定位雪球发文编辑器的各个输入区。

    雪球编辑器（写文章）通常包含：
      · 标题输入框
      · 正文富文本区（contenteditable）
      · 话题/标签输入
      · 「添加AI标识」勾选项
      · 图片上传
      · 「发布」按钮
    这里用多候选 + 几何特征做稳健定位。
    """
    out = {}

    # 标题：长文编辑器里是 textarea（placeholder="请输入标题"）
    for sel in ["textarea[placeholder*='标题']", "textarea[class*='title']",
                "input[placeholder*='标题']", "textarea[placeholder*='请输入']"]:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    out["title"] = el
                    break
        except Exception:
            continue
        if "title" in out:
            break

    # 正文：长文编辑器是 div.ProseMirror（contenteditable）
    try:
        for sel in ["div.ProseMirror", "[class*='ProseMirror']",
                    "[contenteditable='true']", "div[contenteditable]"]:
            try:
                for el in page.query_selector_all(sel):
                    if not el.is_visible():
                        continue
                    bb = el.bounding_box()
                    if not bb or bb["width"] < 200 or bb["height"] < 100:
                        continue
                    out["body"] = el
                    break
            except Exception:
                continue
            if "body" in out:
                break
    except Exception:
        pass

    return out


def fill_title(page, title: str, rng: random.Random) -> bool:
    el = find_editor(page).get("title")
    if not el:
        return False
    try:
        el.click()
        time.sleep(0.2)
        el.press("Control+A")
        time.sleep(0.1)
        el.press("Delete")
    except Exception:
        pass
    return _type_into(el, title[:TITLE_MAX], rng, delay=0.05)


def fill_body(page, body_text: str, rng: random.Random) -> bool:
    el = find_editor(page).get("body")
    if not el:
        return False
    try:
        el.click()
        time.sleep(0.3)
    except Exception:
        pass
    # 雪球编辑器对 isTrusted 敏感 → 逐字真实输入
    ok = _type_into(el, body_text, rng, delay=0.008)
    if ok:
        time.sleep(1.0)
        try:
            got = el.inner_text()
            if len(got.strip()) >= len(body_text.strip()) * 0.9:
                return True
        except Exception:
            return True

    # 回退：直接赋值
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
        time.sleep(0.8)
        return True
    except Exception:
        return ok


def fill_tags(page, tags: list[str], rng: random.Random) -> list[str]:
    """
    添加话题标签。

    雪球的话题有两条路径：
      A) 发帖页独立的话题输入框（搜索 → 选候选）
      B) 正文里直接写 #话题#

    ⚠️ 这里的实现是"多候选 + 校验"，因为控件结构要登录后实测才能定稿：
      先找 placeholder 含"话题/标签/搜索话题"的输入框；
      找不到就退回正文追加 #话题# 的方式。
    """
    added = []

    # 路径 A：独立话题输入框
    for sel in ["input[placeholder*='话题']", "input[placeholder*='标签']",
                "input[placeholder*='搜索话题']"]:
        try:
            el = page.query_selector(sel)
            if not el or not el.is_visible():
                continue
            for t in tags[:4]:
                el.click()
                time.sleep(0.2)
                el.press("Control+A")
                time.sleep(0.1)
                el.press("Delete")
                time.sleep(0.15)
                el.type(t, delay=45)
                time.sleep(1.2)
                # 点选候选（或直接回车）
                picked = False
                for cand in page.query_selector_all("li,span,div,a"):
                    try:
                        if not cand.is_visible():
                            continue
                        ct = (cand.inner_text() or "").strip()
                        if ct == t or (t in ct and len(ct) < 16):
                            cand.click(timeout=4000, force=True)
                            picked = True
                            break
                    except Exception:
                        continue
                if not picked:
                    try:
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                time.sleep(0.8)
                added.append(t)
            break
        except Exception:
            continue

    return added


def append_tags_to_body(page, tags: list[str]) -> bool:
    """
    路径 B：把话题追加到正文末尾（#话题# 形式）。

    雪球正文里 #xxx# 会被识别成话题。
    ⚠️ 只在独立输入框找不到时启用。
    """
    if not tags:
        return False
    try:
        ed = find_editor(page).get("body")
        if not ed:
            return False
        ed.click()
        time.sleep(0.3)
        # ⚠️ 话题之间**必须有空格**，否则会连成 "#a#b#c"
        suffix = "\n\n" + " ".join(f"#{t}#" for t in tags[:4]) + " "
        ed.press("End")
        time.sleep(0.2)
        for ch in suffix:
            ed.type(ch, delay=22)
        time.sleep(0.8)
        # ⚠️ 输入 # 会弹出「热门话题」自动补全浮层，遮挡正文 → ESC 关掉
        for _ in range(3):
            try:
                page.keyboard.press("Escape")
                time.sleep(0.3)
            except Exception:
                break
        # 点正文空白处让浮层失焦
        try:
            ed.click()
            time.sleep(0.3)
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)
        return True
    except Exception:
        return False


def enable_ai_badge(page) -> bool:
    """
    勾选「本文包含AI生成内容」标识。

    雪球社区规则第十二章第4款：**涉 AI 生成内容必须主动标识**。

    【实测结构（长文编辑器 /writeV2/，2026-11-18）】
    「创作声明」是一个**下拉选择器**，不是复选框：
        <div class="index_selectContainer_1ei">
          <div class="selectEle index_selectWrap_2sc">          ← 触发器（显示当前值）
            <div class="selectEle index_name_3r8 false">不添加创作声明</div>
            <span class="index_suffixIcon_3l5 ..."><i>↓</i></span>
          </div>
          <div class="index_optionContainer_1OG ...">          ← 选项容器（默认隐藏）
            ...本文包含AI生成内容...
          </div>
        </div>

    ⚠️ 直接找文案点击**永远失败** —— 选项容器是隐藏的，is_visible() 为 False。
    → 必须**先点触发器展开**，再点选项。
    """
    # 1) 先展开「创作声明」下拉
    #    ⚠️⚠️ 两个关键：
    #    a) 必须限定在「创作声明」那一行内 —— 页面上有两个同 class 的下拉
    #       （分享范围：公开/私人；创作声明：不添加创作声明/本文包含AI生成内容）
    #       全局按 class 找会点到「分享范围」，那里根本没有 AI 那条
    #    b) 必须用 **DOM click**（evaluate 里 el.click()），
    #       用 mouse.click(x,y) 实测**展开不了**（选项容器仍是 display:none）
    trig = page.evaluate(r"""() => {
        const secs = [...document.querySelectorAll('div')].filter(e => {
            const t = (e.textContent || '');
            return t.includes('创作声明') && e.querySelector('div.index_selectWrap_2sc');
        });
        if (!secs.length) return null;
        secs.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length);
        const t = secs[0].querySelector('div.index_selectWrap_2sc');
        if (!t) return null;
        const cur = (t.textContent||'').trim();
        t.click();
        return {cur};
    }""")
    if not trig:
        print("   ⚠️ 未找到「创作声明」下拉触发器", flush=True)
    else:
        print(f"   创作声明当前值: {trig['cur']!r}", flush=True)
        time.sleep(1.5)
        disp = page.evaluate(r"""() => {
            const secs = [...document.querySelectorAll('div')].filter(e => {
                const t = (e.textContent || '');
                return t.includes('创作声明') && e.querySelector('div.index_selectWrap_2sc');
            });
            if (!secs.length) return 'no-section';
            secs.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length);
            const oc = secs[0].querySelector('div[class*=index_optionContainer]');
            return oc ? getComputedStyle(oc).display : 'no-container';
        }""")
        print(f"   选项容器 display: {disp}", flush=True)

    # 2) 点「本文包含AI生成内容」选项（DOM click）
    opt = page.evaluate(r"""() => {
        const secs = [...document.querySelectorAll('div')].filter(e => {
            const t = (e.textContent || '');
            return t.includes('创作声明') && e.querySelector('div.index_selectWrap_2sc');
        });
        if (!secs.length) return {ok:false, why:'no-section'};
        secs.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length);
        const sec = secs[0];
        const cands = [...sec.querySelectorAll('span,div,label,li,a')].filter(e => {
            const t = (e.textContent||'').trim();
            if (!t.includes('本文包含AI生成内容')) return false;
            if (t.length > 20) return false;
            return e.children.length === 0;     // 叶子节点
        });
        if (!cands.length) {
            const oc = sec.querySelector('div[class*=index_optionContainer]');
            return {ok:false, why:'no-leaf-option',
                    disp: oc ? getComputedStyle(oc).display : 'no-container'};
        }
        cands.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length);
        const el = cands[0];
        const txt = (el.textContent||'').trim();
        el.click();
        return {ok:true, txt};
    }""")
    if not opt.get("ok"):
        print(f"   ⚠️ 未找到「本文包含AI生成内容」选项（{opt.get('why')} "
              f"display={opt.get('disp')}）", flush=True)
    else:
        time.sleep(1.8)
        now = page.evaluate(r"""() => {
            const secs = [...document.querySelectorAll('div')].filter(e => {
                const t = (e.textContent || '');
                return t.includes('创作声明') && e.querySelector('div.index_selectWrap_2sc');
            });
            if (!secs.length) return 'none';
            secs.sort((a,b) => (a.textContent||'').length - (b.textContent||'').length);
            const trig = secs[0].querySelector('div.index_selectWrap_2sc');
            return trig ? (trig.textContent||'').trim() : 'none';
        }""")
        print(f"   创作声明当前值: {now!r}", flush=True)
        if "AI生成内容" in now:
            print("   ✅ AI 生成内容标识已勾选", flush=True)
            return True

    print("   ⚠️ 未找到「本文包含AI生成内容」选项", flush=True)
    return False


def find_publish_button(page):
    """定位「发布」按钮（长文编辑器右下角）。"""
    cands = []
    for el in page.query_selector_all("button,a,div[role=button]"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            c = (el.get_attribute("class") or "")
            # 长文编辑器：button.publish_button-dark_2Fg
            if "publish_button" in c:
                cands.append((0, c, el))       # class 命中优先
                continue
            if t not in ("发布", "预览发布", "发表", "立即发布"):
                continue
            bb = el.bounding_box()
            if not bb or bb["width"] < 40 or bb["height"] < 20:
                continue
            cands.append((1, c, el))
        except Exception:
            continue
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    el = cands[0][2]
    try:
        el.scroll_into_view_if_needed(timeout=4000)
        time.sleep(0.6)
        bb = el.bounding_box()
        if not bb:
            return None
        return (bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    except Exception:
        return None


def wait_publish_result(page, timeout: int = 90) -> dict:
    """
    等待发布结果。

    ⚠️ 雪球是「智能反垃圾先拦截 + 人工审核」：
    发布后可能处于"审核中"或"限时加私（限制展示）"，
    所以**必须额外核实对外可见性**，不能只看"发布成功"提示。
    """
    deadline = time.time() + timeout
    success = ["发布成功", "发表成功", "提交成功", "已发布", "发布完成", "审核中"]
    error = ["发布失败", "发表失败", "不能为空", "请输入标题", "内容违规",
             "存在敏感", "请稍后重试", "含有违规内容", "禁止发布",
             "发布过于频繁", "字数太少", "标题不能为空"]

    while time.time() < deadline:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""

        for m in success:
            if m in body:
                i = body.find(m)
                return {"ok": True, "marker": m,
                        "detail": body[max(0, i - 60): i + 100].replace("\n", " "),
                        "url": page.url}
        for m in error:
            if m in body:
                i = body.find(m)
                return {"ok": False, "marker": m,
                        "detail": body[max(0, i - 80): i + 110].replace("\n", " "),
                        "url": page.url}
        time.sleep(2)

    return {"ok": None, "marker": "timeout", "url": page.url}



def _publish_btn_state(page):
    """返回发布按钮的 (x, y, cls)；找不到返回 None。"""
    return page.evaluate(r"""() => {
        const b = [...document.querySelectorAll('a,button')].filter(e => {
            const t = (e.textContent||'').trim();
            return t === '发布' && /lite-editor__submit/.test(e.className||'');
        });
        if (!b.length) return null;
        const e = b[0];
        e.scrollIntoView({block:'center'});
        const r = e.getBoundingClientRect();
        return {x:r.x+r.width/2, y:r.y+r.height/2,
                cls:(e.className||'').toString().slice(0,60)};
    }""")


def _refill_body(page, full_text: str) -> bool:
    """重新填正文（覆盖）。"""
    return page.evaluate(r"""(txt) => {
        const eds = [...document.querySelectorAll('.medium-editor-element')]
            .filter(e => e.offsetParent !== null);
        if (!eds.length) return false;
        const e = eds[0];
        e.focus();
        e.innerHTML = "<p>" + txt.replace("\n", "</p><p>") + "</p>";
        e.dispatchEvent(new InputEvent('input', {bubbles:true}));
        return true;
    }""", full_text)

def publish_post(post: dict, session=None) -> dict:
    """
    发布一篇雪球主贴。

    【架构：独立子进程发布】
    实测：同样的发布代码，在**集成流程**里反复失败（点「发布」没反应），
    在**独立进程**里 100% 成功 —— 根因是主流程残留的页面状态
    （浮层/焦点/localStorage 草稿）污染了编辑器交互。

    → 这里把发布动作交给 `publish/xq/publish_worker.py` 独立进程执行，
      主流程与它用 JSON 文件通信。
    ⚠️ 同一 profile 不能同时开两个浏览器 → 主流程必须先释放 session。
    """
    import json
    import subprocess
    import tempfile

    log = []
    tmp = Path(tempfile.gettempdir()) / f"xq_pub_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    cfile = tmp / "content.json"
    rfile = tmp / "result.json"
    cfile.write_text(json.dumps({
        "title": post.get("title", ""),
        "body": post.get("body", ""),
        "tags": post.get("tags", []),
        "ai_marked": post.get("ai_marked", True),
    }, ensure_ascii=False), encoding="utf-8")

    worker = ROOT / "publish" / "xq" / "publish_worker.py"
    py = sys.executable

    # ⚠️ 释放 profile：主流程的浏览器必须先关掉
    if session is not None:
        try:
            session.release()
            log.append("已释放主流程浏览器（让出 profile）")
        except Exception as e:
            log.append(f"释放浏览器异常: {str(e)[:50]}")

    log.append("启动发布子进程…")
    try:
        proc = subprocess.run(
            [py, str(worker), str(cfile), str(rfile)],
            capture_output=True, text=True, timeout=420,
            cwd=str(ROOT),
        )
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                log.append(line.strip())
        if proc.returncode != 0 and proc.stderr:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            for line in tail:
                log.append("stderr: " + line[:160])
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "publish", "error": "发布子进程超时",
                "log": log}

    if rfile.exists():
        try:
            res = json.loads(rfile.read_text(encoding="utf-8"))
            log += res.get("log", [])
            if res.get("ok"):
                return {"ok": True, "stage": "done",
                        "title": post.get("title"),
                        "post_id": res.get("post_id"),
                        "post_url": res.get("post_url"),
                        "log": log}
            return {"ok": False, "stage": "published",
                    "error": "发布子进程未确认成功",
                    "log": log}
        except Exception as e:
            return {"ok": False, "stage": "published",
                    "error": f"读取子进程结果失败: {str(e)[:80]}", "log": log}

    return {"ok": False, "stage": "published",
            "error": "发布子进程未产出结果", "log": log}


def main():
    import json

    files = sorted(POSTS_DIR.glob("*.json"))
    if not files:
        print("❌ 没有草稿，请先运行: python source/gen_xq.py")
        return
    print("可发布的草稿：")
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {d['id']}  {d['title']}  ({len(d['body'])}字, AI标识={d.get('ai_marked')})")


if __name__ == "__main__":
    main()
