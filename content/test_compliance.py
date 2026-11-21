"""
合规闸门自测 —— 用真实违规案例验证拦截能力。

每个用例对应一个已被平台/监管公开处置的违规形态。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from content.compliance import check_note  # noqa: E402

BASE_TAGS = ["量化交易", "回测", "Python量化"]

CASES = [
    ("AI选股：每日TOP50个股+买点+胜率表",
     {"title": "今日推荐3只个股", "body": "今日推荐3只个股，明日买什么，胜率58%，最大回撤-5.5%，历史7成概率吃肉。", "tags": BASE_TAGS, "images": []},
     "非法荐股 + 误导性信息（无口径的胜率）"),

    ("暗号导流换资料",
     {"title": "免费诊股，评论区打暗号1150领Excel", "body": "评论区打暗号【1150】自动领完整Excel，含分时买点、主力成本。", "tags": BASE_TAGS, "images": []},
     "违规导流 + 非法荐股"),

    ("微信导流（谐音变体）",
     {"title": "加V拿策略源码", "body": "有需要的朋友可以加微信号同号，进群交流，扫码进群。", "tags": BASE_TAGS, "images": []},
     "违规导流（联系方式 + 拉群）"),

    ("手机号明文",
     {"title": "联系我拿模板", "body": "有问题的可以打给我 13800138000，或者私信我。", "tags": BASE_TAGS, "images": []},
     "违规导流（明文联系方式）"),

    ("6位股票代码",
     {"title": "这只票我拿了三个月", "body": "600519 从 1600 跌到 1400，我的判断是还会跌，建议离场。", "tags": BASE_TAGS, "images": []},
     "非法荐股（具体代码 + 买卖判断）"),

    ("收益承诺",
     {"title": "年化收益30%的策略", "body": "这套策略稳赚不赔，保本保息，历史年化收益30%。", "tags": BASE_TAGS, "images": []},
     "误导性信息 / 收益承诺"),

    ("虚假人设",
     {"title": "首席分析师的私藏方法", "body": "我作为机构首席分析师，持牌多年，带单稳赚。", "tags": BASE_TAGS, "images": []},
     "虚假人设 / 资质伪装"),

    ("绩效数字无口径",
     {"title": "回测结果很漂亮", "body": "夏普2.3，收益率45%，年化收益32%。", "tags": BASE_TAGS, "images": []},
     "误导性信息（缺时间区间/样本/模拟盘口径）"),

    ("域名字符串",
     {"title": "完整教程在这里", "body": "完整教程在 xiaohongshu-abc.com，大家自行取用。", "tags": BASE_TAGS, "images": []},
     "违规导流（站外域名）"),

    ("未勾选AI声明",
     {"title": "AI帮我写复盘报告", "body": "记录一下今天的复盘过程。", "tags": BASE_TAGS, "images": [], "ai_declared": False},
     "AI 生成内容未标识"),

    ("B线口径槽位未填（占位符残留）",
     {"title": "回测踩坑记录", "body": ("先交代实验口径：\n📌 数据区间：{{区间}}\n"
              "📌 样本数量：{{笔数}}\n📌 成本假设：{{成本假设}}\n\n"
              "以上只是我自己的记录和方法分享，不构成任何投资建议。"),
      "tags": BASE_TAGS, "images": []},
     "真实素材槽位未填写（不能带 {{}} 半成品发布）"),

    ("正文残留 Markdown 粗体",
     {"title": "看不到样本量的结论，我一律不采信",
      "body": ("我后来加了一条硬规则：**任何一段结论，我看不到它的样本量和时间窗口，就不采信。**\n\n"
               "以上只是我自己的记录和方法分享，不构成任何投资建议。"),
      "tags": BASE_TAGS, "images": []},
     "正文残留 Markdown 标记（小红书会显示字面星号）"),

    ("正文残留 Markdown 标题与链接",
     {"title": "我的复盘方法", "body": ("# 今日复盘\n\n看 [这个教程](https://example.com/a) 就懂了。\n\n"
               "以上只是我自己的记录和方法分享，不构成任何投资建议。"),
      "tags": BASE_TAGS, "images": []},
     "正文残留 Markdown 标记（标题/链接）"),

    ("合规内容（应放行）",
     {"title": "回测跑了3年才发现：均线金叉在震荡市里是负期望",
     "body": ("先交代实验口径：数据区间 2021-01 至 2026-10，样本 87 笔，"
              "含手续费与滑点，模拟回测非实盘，不代表未来表现。\n\n"
              "均线金叉在震荡市里是负期望的，这个结论是跑完才知道的。\n\n"
              "以上只是我自己的记录和方法分享，不构成任何投资建议。"),
      "tags": BASE_TAGS, "images": []},
     "应当放行", False),
]


def main():
    print("=" * 78)
    print("合规闸门自测")
    print("=" * 78)
    passed = 0
    for case in CASES:
        name, note, expect = case[0], case[1], case[2]
        expect_block = case[3] if len(case) > 3 else True
        r = check_note(note)
        got_block = not r["passed"]
        ok = (expect_block == got_block)
        passed += ok
        icon = "✅" if ok else "❌"
        print(f"\n{icon} {name}")
        print(f"   期望: {'拦截' if expect_block else '放行'} | 实际: {'拦截' if got_block else '放行'} | {expect}")
        for v in r["violations"]:
            print(f"      ⛔ {v['category']}: {'、'.join(map(str, v['hits'][:4]))}")
        if r["warnings"]:
            for w in r["warnings"]:
                print(f"      ⚠️  {w['category']}")
    print("\n" + "=" * 78)
    print(f"结果: {passed}/{len(CASES)} 通过")
    print("=" * 78)
    sys.exit(0 if passed == len(CASES) else 1)


if __name__ == "__main__":
    main()
