# AXIOM 多平台推广系统

基于 AXIOM 数据源的自动化内容生成与多平台发布系统，覆盖**小红书 / 雪球 / 抖音 / 淘股吧**四个渠道。

> ⚠️ **本仓库不含任何账号凭证与登录态。** 首次使用请务必先读 [环境准备](#二环境准备) 一节。

---

## 一、快速开始

```bash
git clone https://github.com/jesnaoliver/axiom_promotion.git
cd axiom_promotion

# 1. 装依赖
pip install playwright requests
playwright install chromium

# 2. 配凭证
cp .env.example .env    # 然后编辑 .env 填入真实账号

# 3. 空跑验证（不发布）
python scheduler.py --dry-run
```

---

## 二、环境准备

### 凭证配置

仓库只提供 `.env.example`（仅键名、无值）。复制后填入真实值：

| 键名 | 用途 |
|---|---|
| `axiom_user` / `axiom_passwd` | AXIOM 数据源登录（拉取指标数据） |
| `xq_user` / `xq_passwd` | 雪球账号 |
| `xgb_user` / `xgb_passwd` | 小红书 / 淘股吧账号 |

**`.env` 已被 `.gitignore` 排除，切勿提交、切勿外传。**

### 登录态

各平台登录态存在**本机**的浏览器 profile 目录（`.xq_profile/`、`.douyin_profile/`、`.tgb_profile/`、`.browser_profile/`），同样已排除在仓库外。

**每个平台都需要人工扫码登录一次**，profile 会各自在本机生成。登录态无法共享——这是刻意设计，详见 [安全边界](#七安全边界)。

---

## 三、目录结构

```
├── scheduler.py              日更调度器：选题轮转 → 组装 → 封面 → 合规闸门 → 发布 → 日志
├── source/                   内容生成器与数据源接入
│   ├── axiom_api.py          AXIOM API 客户端（凭证走 .env）
│   ├── axiom_intel.py        情报数据处理
│   ├── gen_xq.py             雪球版内容改写器
│   ├── gen_douyin.py         抖音版内容改写器
│   ├── gen_tgb.py            淘股吧版内容改写器
│   ├── gen_from_axiom.py     通用 AXIOM 内容生成
│   ├── build_axiom_covers.py 封面图生成
│   ├── site_compliance_report.py 站点合规检查报告生成
│   └── sector_map.py         板块映射
├── content/                  内容组装与合规
│   ├── compose.py            正文组装（模板 A/B + 槽位填充）
│   ├── compliance.py         小红书合规闸门（ERROR 级直接阻断）
│   ├── xq_compliance.py      雪球合规闸门
│   ├── douyin_compliance.py  抖音合规闸门
│   ├── tgb_compliance.py     淘股吧合规闸门
│   ├── topic_bank.py         选题库（35 条）
│   ├── cover.py              封面生成
│   ├── facts/                真实素材（每个选题一个 JSON）
│   └── for_xq/ for_tgb/ for_douyin/ for_xq_hot/  各平台成稿
├── publish/                  发布器（Playwright）
│   ├── browser.py            浏览器会话层
│   ├── login.py              扫码登录
│   ├── publisher.py          小红书发布器
│   ├── douyin/               抖音发布器 + 可见性控制
│   ├── xq/                   雪球发布器
│   ├── tgb/                  淘股吧发布器
│   └── sync_login.py         登录态同步
├── logs/                     发布日志（**去重闸门依赖此目录，勿删**）
├── outputs/                  产出成品图
└── 各类策略与方案文档（.md）
```

---

## 四、核心机制

### 选题轮转

```
epoch = (目标日期 - 2026-11-01).days
idx   = epoch % 35
topic = TOPIC_BANK[idx]
```

同一天永远取同一条，不同天不重复。`2026-11-02 → A02`。

### 合规闸门（不可绕过）

每个平台有独立合规模块，**ERROR 级直接阻断发布**，只能报告拦截原因，不得绕过。

小红书闸门规则覆盖：真实素材缺失、槽位未填、收益类表述、Markdown 残留（`**` `#` `>` ``` `[链接](url)`）等。

### 发布去重（硬闸门）

`publish/publisher.py` 的 `_published_index()` 扫描 `logs/` 索引已发布的 topic_id 与标题，重复则在**发布动作之前**拦截。

**`logs/` 目录必须保留**，否则去重失效，同一篇内容重发会被平台判刷量。

「一天一篇」判定按平台隔离——小红书只认 `*_publish.json` / `*_axiom_publish.json`，抖音只认 `*_douyin_*`，淘股吧只认 `*_tgb_*`。新增平台时**必须只认自己的日志**。

---

## 五、验证命令

```bash
# 合规闸门自测（应 12/12 通过）
python content/test_compliance.py

# 空跑，不发布
python scheduler.py --dry-run
```

---

## 六、当前已知问题

| 事项 | 状态 |
|---|---|
| 小红书账号发文受限 | 平台以「利用 AI 托管进行发文/互动」为由处置 4 篇笔记，**账号级封禁**（换 benign 选题同样被拦）。解禁前不要跑发布脚本 |
| `content/facts/` 素材为空 | 目前只有 README.md，走「默认骨架 + 待补槽位」路径，正文为通用文案而非真实经历 |
| 抖音合规区窄于小红书 | 无「官网主页」外链以外出口；AI 批量起号被专项打击，内容必须差异化 |
| 站点合规待整改 | `efficientinvest.cn/vibe-trading` 的 title / canonical / SSR / 风险提示均不合规，抖音审核大概率驳回 |
| 抖音账号资质待定 | 蓝V 主体、ICP 备案主体一致性、金融从业资质、账号类型均未确认 |

---

## 七、安全边界

以下内容**刻意排除在仓库之外**，且不应通过任何渠道回传：

- **`.env`** —— 明文密码
- **`.browser_profile/` `.douyin_profile/` `.xq_profile/` `.tgb_profile/`** —— 各平台 session cookie，泄露等同于账号被接管
- **`assets/`** —— 验证码/滑块调试截图
- **`source/probe_*.py` 等临时排查脚本** —— 用完即弃，留在本地

> `source/steal_xq_cookie.py` 是从本机 Chrome 提取雪球 cookie 导入自动化 profile 的工具（仅限 xueqiu.com 域名）。它本身不含凭证，但属于绕过滑块的敏感工具，**使用前请确认合规性**。

另外三条硬规则：

1. **严禁编造**任何回测数据、收益数字、胜率。真实素材只能来自 `content/facts/`。
2. **合规闸门拦截时不得绕过**，只能报告拦截原因。
3. **登录/验证码需人工完成**，自动化任务不得代劳。发布后须**人工**回复评论区前 30 条评论（平台禁止 AI 托管互动）。

---

## 八、协作约定

- 进度、结论、卡点写在项目任务条目与评论区
- 产出物（策略文档、选题清单、发布记录）提交回仓库对应目录
- 涉及登录态、密码、Cookie 的内容一律不入库
- 临时排查脚本用完即删，不进版本库

---

## 九、相关文档

| 文档 | 内容 |
|---|---|
| `方案.md` | 总方案，含抖音合规调研结论 |
| `雪球推广策略.md` | 雪球推广策略 |
| `雪球热点发帖流程.md` | 雪球热点发帖标准流程 |
| `淘股吧推广策略.md` | 淘股吧推广策略 |
| `站点合规修复清单.md` | 站点整改清单（含可粘贴的 HTML/nginx 配置） |
| `热点记录.xlsx` | 历史热点记录，用于选题复盘 |
| `.workbuddy/memory/MEMORY.md` | **项目长期记忆**：配图脱敏铁律、去重铁律、风控记录，必读 |
