"""
抖音作品**可见性**判定与修复（长图文 / 体裁=长图文）。

🔴 为什么需要这个模块（2026-11-12 实测踩坑）：
抖音在内容管理页标示「非公开作品」的角标**换过至少两次**，每次都让旧判定失效：

| 时间 | 角标 class | 角标文案 | 卡片文案 | 旧判定结果 |
|---|---|---|---|---|
| 2026-11-06 | `private-mark-hAsuyc` | 「私密」 | 「私密」 | ✅ 能识别 |
| 2026-11-12 | `creator-ai-design-work-cover-private` | **空文本**（锁图标） | 「作品仅自己可见」/「限制自己可见」 | ❌ **全漏判** |

11-12 实测：11-04、11-05 的角标 class 里已**不含** "private"（是
`...-work-cover-private`，且在**子 span** 上，不是卡片容器上），
角标文本也**不再有「私密」二字**（只剩一个空文本锁图标 span）。
→ 旧代码 `"private" in card_cls or "私密" in card_text` 两条都失效，
   把「非公开」作品全部误判成「✅公开」。

后果很实际：11-07 那篇（11-07 23:15 发布）**从 11-08 起就已经是非公开**，
但连续 4 次执行（11-08 / 11-09 / 11-10 / 11-11）的报告都写
「11-04、11-05 两篇私密」，**从没人发现 11-07 也是**。
→ 本模块用**多信号并集**判定，任一命中即判非公开，避免再次漏判。

判定信号（任一命中 → 非公开）：
1. 卡片文本含「私密」/「作品仅自己可见」/「限制自己可见」/「仅自己可见」
2. 卡片 class 含 `disabled`（抖音用 `video-card-new-disabled-*` 表示被限可见）
3. 卡片**子元素** class 含 `private`（锁图标 span：`...-work-cover-private`）

⚠️ 为什么不能只看「有没有『设置权限』按钮」：
   - 11-04 / 11-05（有「设置权限」但**实际非公开**）→ 会漏判
   - 11-07（**没有**「设置权限」，只有「查看详情」）→ 也会漏判
   两者方向相反，必须用多信号并集。
"""

from __future__ import annotations

# 非公开的**文本**信号
PRIV_TEXT_HINTS = (
    "私密",
    "作品仅自己可见",
    "限制自己可见",
    "仅自己可见",
)

# 非公开的 **class** 信号（卡片容器自身）
PRIV_CLASS_HINTS = (
    "disabled",
)

# 非公开的 **class** 信号（卡片**后代**元素，锁图标）
PRIV_DESCENDANT_CLASS_HINTS = (
    "private",
)


def is_private_text(text: str | None) -> bool:
    """文本里是否出现非公开标记。"""
    if not text:
        return False
    return any(h in text for h in PRIV_TEXT_HINTS)


def is_private_class(cls: str | None) -> bool:
    """卡片容器 class 是否表示非公开（disabled）。"""
    if not cls:
        return False
    low = cls.lower()
    return any(h in low for h in PRIV_CLASS_HINTS)


def has_private_descendant(descendant_classes: list[str] | None) -> bool:
    """卡片后代元素里是否有私有锁图标（class 含 private）。"""
    for c in descendant_classes or []:
        if c and "private" in c.lower():
            return True
    return False


def is_private_card(card_text: str | None,
                    card_class: str | None = None,
                    descendant_classes: list[str] | None = None) -> bool:
    """
    综合判定一个作品卡片是否**非公开**。

    Args:
        card_text: 卡片 innerText
        card_class: 卡片容器的 class
        descendant_classes: 卡片**后代**元素的 class 列表
    """
    return (is_private_text(card_text)
            or is_private_class(card_class)
            or has_private_descendant(descendant_classes))


def visibility_label(card_text: str | None,
                     card_class: str | None = None,
                     descendant_classes: list[str] | None = None) -> str:
    """返回「🔒非公开」/「✅公开」标签，供探针输出。"""
    return "🔒非公开" if is_private_card(card_text, card_class, descendant_classes) else "✅公开"


def collect_works(page, body_text: str = "") -> list[dict]:
    """
    从内容管理页（已应用「体裁 → 长图文」筛选）抽取所有作品行。

    返回每条：{title, dt, status, card_text, card_class, descendant_classes,
              private, has_set_perm}

    只读：不点击任何东西。
    """
    data = page.evaluate(r"""
    () => {
      const out = [];
      const sels = 'div[class*=video-card-sfaSzz], div[class*=card-item], li[class*=card-item]';
      document.querySelectorAll(sels).forEach(card => {
        const txt = (card.innerText||'').trim();
        if (!txt || !txt.includes('删除作品')) return;
        const dm = txt.match(/(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})/);
        const dt = dm ? `${dm[1]}-${String(dm[2]).padStart(2,'0')}-${String(dm[3]).padStart(2,'0')} ${dm[4]}` : '';
        const title = txt.split('\n').map(s=>s.trim()).filter(Boolean)[0] || '';
        const status = (txt.match(/已发布|审核中|发布成功|审核未通过|未通过/)||[''])[0];
        const desc = [];
        card.querySelectorAll('*').forEach(el => {
          const c = (el.className && el.className.baseVal !== undefined)
                    ? el.className.baseVal : (el.className||'');
          if (typeof c === 'string' && c) desc.push(c);
        });
        out.push({
          title, dt, status,
          cardText: txt,
          cardClass: (card.className && card.className.baseVal !== undefined)
                     ? card.className.baseVal : (card.className||''),
          descendantClasses: desc,
          hasSetPerm: txt.includes('设置权限'),
        });
      });
      return out;
    }""")

    works = []
    for d in data:
        d["private"] = is_private_card(d.get("cardText"),
                                       d.get("cardClass"),
                                       d.get("descendantClasses"))
        works.append(d)
    return works
