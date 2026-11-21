"""
雪球发布子进程 —— 独立进程执行发布。

【为什么要独立进程（2026-11-18）】
发布动作在**集成流程**里反复失败（"点发布没反应"），
但同样的代码在**独立进程**里 100% 成功。
根因是页面状态污染：主流程已经打开过首页、滚动过、展开过热门话题面板，
浏览器里残留了各种状态（浮层、焦点、localStorage 草稿），
导致编辑器的交互与干净会话不一致。

→ 架构：**主流程写 content.json → 关闭自己的浏览器 → 子进程独立发布 → 写 result.json**
→ 主流程读 result.json

【本进程内的序列（= 实测 100% 成功的序列）】
  1. goto 首页 → sleep 9
  2. 点「发帖」按钮 → sleep 4
  3. 激活编辑器（必要时点占位符）
  4. **一次性 innerHTML 写入正文（含话题标签）** → sleep 2
  5. 点「发布」→ 监听 POST /statuses/update.json 的响应取 id

【🔴 话题标签：2026-11-19 关键修正】
──────────────────────────────
实测结论（用户手动验证 + 详情页 DOM 核对）：
  · 用户手动发的 417579218 用了 3 个话题：#研究方法# #家电# #消费#
    → 详情页里确实渲染成 3 个 topic 链接（a[href*="k?q="]）→ **有效**
  · 我昨天发的 417455724 正文里 `#` 出现 **0 次** → 话题根本没插进去
  · 用户实测：**4 个话题会失败，3 个正常**

之前失败的两个原因（都已规避）：
  A. 工具栏「#」按钮 → 它的实现是**替换正文**，把已填内容冲掉
  B. 正文里逐字输入 `#` → 触发「热门话题」自动补全浮层 → 遮挡发布按钮
     （浮层里全是带个股名的热门话题，一旦点进去就等于把个股名写进正文）

→ **本版做法：把话题标签和正文一起，一次性 innerHTML 写入**
  · 不点工具栏 # 按钮
  · 不逐字输入 # → 不触发自动补全浮层
  · 话题用 `#标签#` 文本形式，雪球服务端会解析成 topic
  · 标签数量**硬上限 3 个**

【发布接口格式（实测抓包）】
  POST https://www.xueqiu.com/statuses/update.json
  Content-Type: application/x-www-form-urlencoded
    status=<HTML正文，换行 <br>>
    allow_reward=false
    ai_disclose=0|1      ← 1 = 声明「本文包含AI生成内容」
    post_position=pc_home_post
    session_token=<CSRF>
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

HOME = "https://xueqiu.com/"
USER_ID = "2569689091"

# 话题标签硬上限（用户实测：4 个失败，3 个正常）
TAG_MAX = 3

AI_DECLARATION = "（本文由 AI 辅助整理，仅作信息与方法记录，不构成投资建议。）"


def build_tags_text(tags: list[str]) -> str:
    """把标签数组转成雪球能识别的话题文本。"""
    clean = []
    for t in (tags or []):
        t = (t or "").strip().lstrip("#").rstrip("#").strip()
        if t and t not in clean:
            clean.append(t)
    clean = clean[:TAG_MAX]
    if not clean:
        return ""
    # 话题之间必须有空格，否则会连成 "#a#b#c"
    return " ".join(f"#{t}#" for t in clean)


def build_html(text: str) -> str:
    """把纯文本转成雪球编辑器要的 HTML（换行 → <br>）。"""
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return esc.replace("\n", "<br>")


def publish(content: dict, result_path: Path) -> dict:
    from playwright.sync_api import sync_playwright

    title = (content.get("title") or "").strip()
    body = content.get("body") or ""
    tags = content.get("tags") or []
    want_ai = bool(content.get("ai_marked", True))

    out = {"ok": False, "log": []}

    # ---- 组装最终正文（正文 + AI声明 + 话题，一次性写入）----
    parts = []
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    full = "\n\n".join(parts)

    # AI 声明（短帖编辑器没有 AI 勾选框 → 正文自声明）
    if want_ai and AI_DECLARATION not in full:
        full = full + "\n\n" + AI_DECLARATION

    # 话题标签（≤3 个）
    tags_text = build_tags_text(tags)
    if tags_text:
        full = full + "\n\n" + tags_text

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / ".xq_profile"),
            channel="msedge",
            headless=False,
            args=["--window-size=1600,1000", "--start-maximized",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})

        allpost = []
        posted = {}

        def on_any(r):
            try:
                if r.request.method == "POST" and "insights" not in r.url:
                    allpost.append(r.url[:120])
            except Exception:
                pass
        page.on("request", on_any)

        def on_resp(r):
            try:
                if r.request.method != "POST":
                    return
                if "/statuses/update.json" not in r.url:
                    return
                try:
                    d = json.loads(r.text())
                except Exception:
                    d = {}
                if d.get("id"):
                    posted["id"] = d["id"]
                    posted["raw"] = d
                posted["payload"] = (r.request.post_data or "")[:600]
            except Exception:
                pass

        page.on("response", on_resp)

        try:
            # 1) 首页
            page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
            time.sleep(9)

            # 2) 点「发帖」
            pos = page.evaluate(r"""() => {
                const b = [...document.querySelectorAll('button')]
                    .find(e => (e.textContent||'').trim() === '发帖');
                if (!b) return null;
                b.scrollIntoView({block:'center'});
                const r = b.getBoundingClientRect();
                return {x:r.x+r.width/2, y:r.y+r.height/2};
            }""")
            if not pos:
                out["log"].append("未找到「发帖」按钮")
                return out
            page.mouse.click(pos["x"], pos["y"])
            time.sleep(4)

            # ⚠️ 编辑器可能处于「未激活」状态
            #    .lite-editor 可见但 .fake-placeholder 也可见
            #    → 输入区没展开 → 必须点占位符激活
            for _ in range(6):
                state = page.evaluate(r"""() => {
                    const fp = document.querySelector('.fake-placeholder');
                    const fpVis = fp ? (fp.offsetParent !== null) : false;
                    const eds = [...document.querySelectorAll('[contenteditable]')]
                        .filter(e => e.offsetParent !== null
                                     && e.getBoundingClientRect().height > 20);
                    return {fpVis, editorReady: eds.length > 0};
                }""")
                if state.get("editorReady"):
                    break
                if state.get("fpVis"):
                    pos2 = page.evaluate(r"""() => {
                        const fp = document.querySelector('.fake-placeholder');
                        if (!fp) return null;
                        fp.scrollIntoView({block:'center'});
                        const r = fp.getBoundingClientRect();
                        return {x: Math.round(r.x + r.width/2),
                                y: Math.round(r.y + r.height/2)};
                    }""")
                    if pos2:
                        page.mouse.click(pos2["x"], pos2["y"])
                        out["log"].append("已点击占位符激活编辑器")
                        time.sleep(2.5)
                        continue
                time.sleep(1.5)

            time.sleep(1.5)
            out["log"].append("已展开编辑器")

            # 3) 一次性写入正文 + 话题
            #    ⚠️ 不逐字输入、不点工具栏 # 按钮 → 不触发自动补全浮层
            filled = page.evaluate(r"""(html) => {
                const tas = [...document.querySelectorAll('textarea')]
                    .filter(e => e.offsetParent !== null);
                const ces = [...document.querySelectorAll('[contenteditable]')]
                    .filter(e => e.offsetParent !== null);
                const target = tas[0] || ces[0];
                if (!target) return false;
                target.focus();
                if (target.tagName === 'TEXTAREA') {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(target, html);
                } else {
                    target.innerHTML = html;
                }
                target.dispatchEvent(new InputEvent('input', {bubbles: true}));
                return true;
            }""", build_html(full))
            if not filled:
                out["log"].append("未找到正文编辑区")
                return out
            time.sleep(2.5)
            out["log"].append(f"正文已填 {len(full)} 字"
                              + (f"（含话题 {tags_text}）" if tags_text else ""))

            # 3b) 校验：正文里话题标签是否真的在
            check = page.evaluate(r"""() => {
                const ed = document.querySelector('.medium-editor-element')
                    || [...document.querySelectorAll('[contenteditable]')]
                        .filter(e=>e.offsetParent!==null)[0];
                if (!ed) return {ok:false};
                const t = ed.innerText || '';
                const m = t.match(/#[^#\s]{1,12}#/g) || [];
                return {ok:true, tags:m, tail:t.slice(-120)};
            }""")
            if check.get("ok"):
                out["log"].append(f"   正文内话题: {check.get('tags')}")
                if tags_text and not check.get("tags"):
                    out["log"].append("   ⚠️ 话题未出现在正文里")

            # 3c) 关掉可能出现的浮层（保险）
            for _ in range(2):
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except Exception:
                    break

            # 4) 发布按钮
            #    ⚠️ 页面侧边栏也有「发布」菜单项 → 必须精确 class 定位
            btn = page.evaluate(r"""() => {
                const cands = [];
                document.querySelectorAll('a.lite-editor__submit,button.lite-editor__submit')
                  .forEach(e => cands.push(e));
                if (!cands.length) {
                  document.querySelectorAll('a,button,div[class*=submit]').forEach(e => {
                    if ((e.textContent||'').trim() === '发布'
                        && /submit/i.test(e.className||'')) cands.push(e);
                  });
                }
                if (!cands.length) return null;
                const b = cands[0];
                b.scrollIntoView({block:'center'});
                const r = b.getBoundingClientRect();
                return {x:r.x+r.width/2, y:r.y+r.height/2,
                        cls:(b.className||'').toString().slice(0,50),
                        tag:b.tagName,
                        disabled: /disabled/i.test(b.className||'')};
            }""")
            if not btn:
                out["log"].append("未找到发布按钮")
                return out
            out["log"].append(f"发布按钮: {btn.get('tag')}.{btn.get('cls')}")
            if btn.get("disabled"):
                out["log"].append("发布按钮是禁用状态")
                return out
            page.mouse.click(btn["x"], btn["y"])
            time.sleep(1.5)

            # 即时诊断
            out["log"].append(f"   点击后POST: {allpost[-6:]}")
            try:
                out["log"].append("   编辑器内容: " + str(
                    page.evaluate("""() => {
                        const e = document.querySelector('.medium-editor-element');
                        return e ? (e.innerText||'').slice(-120) : 'none';
                    }""")))
                out["log"].append("   页面尾部: " + str(
                    page.inner_text("body")[-260:]))
                page.screenshot(path=str(ROOT / "assets" / "_xq_worker_click.png"))
            except Exception as e:
                out["log"].append(f"   诊断异常: {str(e)[:60]}")

            # 5) 等结果
            #    成功信号两个：a) POST 响应里有 id  b) 页面出现「发布成功」
            deadline = time.time() + 45
            while time.time() < deadline:
                if posted.get("id"):
                    break
                try:
                    if "发布成功" in page.inner_text("body"):
                        out["log"].append("   页面出现「发布成功」")
                        posted["page_ok"] = True
                        break
                except Exception:
                    pass
                time.sleep(0.8)

            if posted.get("id") or posted.get("page_ok"):
                out["ok"] = True
                out["post_id"] = posted.get("id")
                if posted.get("id"):
                    out["post_url"] = f"https://xueqiu.com/{USER_ID}/{posted['id']}"
                out["log"].append(f"✅ 发布成功 id={posted.get('id')}")
                if posted.get("payload"):
                    out["log"].append(f"   请求体: {posted['payload'][:300]}")
            else:
                out["log"].append("未抓到发布成功的响应")
                try:
                    page.screenshot(path=str(ROOT / "assets" / "_xq_worker_failed.png"))
                except Exception:
                    pass
        except Exception as e:
            out["log"].append(f"异常: {type(e).__name__}: {str(e)[:120]}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return out


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("content")
    ap.add_argument("result")
    args = ap.parse_args()

    content = json.loads(Path(args.content).read_text(encoding="utf-8"))
    out = publish(content, Path(args.result))
    Path(args.result).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(f"  {x}" for x in out["log"]), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
