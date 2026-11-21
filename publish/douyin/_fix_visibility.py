"""
把内容管理页里**非公开**的抖音长图文作品改为「公开」。

【为什么需要】
2026-11-12 实测发现 3 篇已发布作品对外不可见（= 白发）：
  · 2026-11-04 18:01  4个板块AI标了2个高置信，剩2个自己查
  · 2026-11-05 15:12  4个板块，材料只有个位数的我不看结论
  · 2026-11-07 23:15  16128条信息，我只看其中2类
原因：发布时「谁可以看」没有显式选成「公开」，且平台侧把非公开作品
显示为「作品仅自己可见 / 限制自己可见」——旧的判定逻辑认不出来，
于是连续 5 次执行都把「发布成功」误报成成功。

【操作流程（逐条）】
1. 内容管理页 → 体裁 → 长图文
2. 找到目标作品卡片
3. 点「设置权限」→ 在「权限设置」弹层里选「公开」→ 点「保存」
4. 回读卡片状态确认变成公开

⚠️ 安全约束：
  · 只操作**明确指定标题**的作品，不做批量模糊匹配
  · 每次保存前会二次确认弹层里当前选中的就是「公开」
  · 保存后**重新读取**卡片状态校验，失败会如实报告
  · 不会删除作品、不会改标题正文、不会动任何其他作品

用法:
    python publish/douyin/_fix_visibility.py "标题前10字" "标题前10字"
    python publish/douyin/_fix_visibility.py --list      # 只列出非公开作品
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from publish.douyin.browser import DouyinSession  # noqa: E402
from publish.douyin.visibility import collect_works, visibility_label  # noqa: E402
from publish.douyin._probe_check_title import _select_filter  # noqa: E402

MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"


def _open_manage(session):
    session.page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=90000)
    time.sleep(6)
    _select_filter(session.page, "体裁", "长图文")
    time.sleep(4)


def _list_nonpublic(session):
    _open_manage(session)
    works = collect_works(session.page)
    bad = [w for w in works if w.get("private")]
    print(f"长图文作品共 {len(works)} 条，其中非公开 {len(bad)} 条：")
    for w in bad:
        print(f"  🔒 {w.get('dt')} | 状态={w.get('status') or '(无)'} | "
              f"设置权限={w.get('hasSetPerm')} | {(w.get('title') or '')[:36]}")
    return works


def _set_public_for_card(session, card):
    """在指定卡片上打开「设置权限」，选「公开」，保存。返回 (ok, 说明)。"""
    # 1) 点「设置权限」
    opened = False
    for el in card.query_selector_all("span,div,button,a"):
        try:
            if el.is_visible() and (el.inner_text() or "").strip() == "设置权限":
                el.click(timeout=5000)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False, "卡片内未找到「设置权限」按钮"

    time.sleep(3)

    # 2) 弹层里确认「谁可以看」分组存在
    body = session.page.inner_text("body")
    if "谁可以看" not in body:
        session.page.keyboard.press("Escape")
        time.sleep(1)
        return False, "未打开「权限设置」弹层"

    # 3) 选「公开」radio
    # ⚠️ 2026-11-12 实测（DOM dump 结论）：
    #    「公开」这一项的结构是
    #      <LABEL class="radio-d4zkru">  ← 可见、可点，rect 132x28
    #        <input type=radio value=0>  ← 原生 input，被样式隐藏（6x6）
    #        文本「公开」
    #    → 必须点 **LABEL**（可见的那块），点隐藏的 input 无效。
    #    另外 input 的 value：公开=0 / 好友可见=2 / 仅自己可见=1。
    picked = False
    for el in session.page.query_selector_all("input[type='radio'], input[type='checkbox']"):
        try:
            val = el.get_attribute("value")
            lab = (el.evaluate(
                "e => (e.closest('label')?.innerText||'') || "
                "(e.parentElement?.innerText||'')") or "").strip()
            if not (lab == "公开" or (val == "0" and lab in ("公开", ""))):
                continue
            # 必须在「谁可以看」分组内（逐级向上最多 8 层）
            in_group = el.evaluate(
                "e => { let n = e; for (let i=0;i<8 && n;i++){ n = n.parentElement;"
                "if (n && (n.innerText||'').includes('谁可以看')) return true; } return false; }")
            if not in_group:
                continue
            # 点它的 LABEL（可见块），而不是隐藏的 input
            handle = el.evaluate_handle("e => e.closest('label')")
            label_el = handle.as_element() if handle else None
            clicked = False
            if label_el is not None:
                try:
                    label_el.scroll_into_view_if_needed(timeout=3000)
                    label_el.click(timeout=5000)
                    clicked = True
                except Exception:
                    clicked = False
            if not clicked:
                try:
                    el.click(timeout=5000, force=True)
                    clicked = True
                except Exception:
                    pass
            if clicked:
                picked = True
                break
        except Exception:
            continue
    if not picked:
        session.page.keyboard.press("Escape")
        time.sleep(1)
        return False, "弹层内未找到「公开」选项"

    time.sleep(1)

    # 4) 回读 radio 状态，确认真的选上了
    # ⚠️ 2026-11-12 实测踩坑：这里**不能**用 `i.closest('div,section,form')` 来找
    #    「谁可以看」分组 —— closest 只往上走一两层，拿到的是 radio 组本身，
    #    其 innerText 不含「谁可以看」（那是个**兄弟**label，不是祖先文本）
    #    → 恒返回 null → 误判为「没点上」→ 白白放弃保存。
    #    正确做法：逐级向上最多 8 层找含「谁可以看」的祖先。
    checked = session.page.evaluate(r"""
      () => {
        for (const i of document.querySelectorAll("input[type=radio],input[type=checkbox]")) {
          const lab = (i.closest('label')?.innerText||'') || (i.parentElement?.innerText||'');
          if ((lab||'').trim() !== '公开') continue;
          let n = i;
          for (let k=0;k<8 && n;k++){
            n = n.parentElement;
            if (n && (n.innerText||'').includes('谁可以看')) return i.checked;
          }
        }
        return null;
      }""")
    if checked is None:
        session.page.keyboard.press("Escape")
        time.sleep(1)
        return False, "回读不到「公开」radio 状态，已放弃保存"
    if not checked:
        session.page.keyboard.press("Escape")
        time.sleep(1)
        return False, "点击后「公开」未处于选中状态，已放弃保存"

    # 5) 点「保存」
    saved = False
    for el in session.page.query_selector_all("button,span,div[role='button'],div"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if t != "保存":
                continue
            # 必须在权限弹层内（弹层里同时含「谁可以看」和「保存」）
            in_group = el.evaluate(
                "e => { let n = e; for (let i=0;i<8 && n;i++){ n = n.parentElement;"
                "if (n && (n.innerText||'').includes('谁可以看')) return true; } return false; }")
            if not in_group:
                continue
            el.click(timeout=5000)
            saved = True
            break
        except Exception:
            continue
    if not saved:
        session.page.keyboard.press("Escape")
        time.sleep(1)
        return False, "弹层内未找到「保存」按钮"

    time.sleep(3)
    return True, "已保存"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    list_only = "--list" in sys.argv

    with DouyinSession(headless=False) as session:
        session.open()
        if not session.is_logged_in():
            print("❌ 未登录，无法核查")
            return 3

        works = _list_nonpublic(session)

        if list_only:
            return 0

        if not args:
            print("\n用法: python publish/douyin/_fix_visibility.py \"标题前10字\" [...]")
            return 2

        rc = 0
        for key in args:
            print(f"\n=== 处理: {key} ===")
            # 重新载入列表，避免弹层残留影响后续查找
            _open_manage(session)
            works = collect_works(session.page)
            target = None
            for w in works:
                if key in (w.get("cardText") or ""):
                    target = w
                    break
            if target is None:
                print(f"  ⚠️ 未找到标题含 {key!r} 的作品")
                rc = 1
                continue

            before = visibility_label(target.get("cardText"),
                                      target.get("cardClass"),
                                      target.get("descendantClasses"))
            print(f"  修改前: {before} | {target.get('dt')} | "
                  f"{(target.get('title') or '')[:36]}")

            if not target.get("private"):
                print("  ℹ️ 该作品已是公开，跳过")
                continue

            # 重新按 DOM 拿到卡片元素
            card = None
            for el in session.page.query_selector_all(
                    "div[class*='video-card-sfaSzz'], div[class*='card-item']"):
                try:
                    t = (el.inner_text() or "")
                except Exception:
                    continue
                if key in t and "删除作品" in t:
                    card = el
                    break
            if card is None:
                print("  ⚠️ 未定位到作品卡片元素")
                rc = 1
                continue

            ok, msg = _set_public_for_card(session, card)
            print(f"  操作: {msg}")

            # 6) 回读校验
            _open_manage(session)
            after_works = collect_works(session.page)
            after = "?"
            for w in after_works:
                if key in (w.get("cardText") or ""):
                    after = visibility_label(w.get("cardText"),
                                             w.get("cardClass"),
                                             w.get("descendantClasses"))
                    break
            print(f"  修改后: {after}")
            if after != "✅公开":
                print("  ❌ 复核未通过（仍非公开）")
                rc = 1
            else:
                print("  ✅ 已确认公开")

    return rc


if __name__ == "__main__":
    sys.exit(main())
