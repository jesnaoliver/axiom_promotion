# 项目长期记忆 · xhs（小红书日更发布）

## 项目结构
- `scheduler.py` — 日更调度器（选题轮转 → 组装 → 封面 → 合规闸门 → 发布 → 日志）
- `content/compose.py` — 正文组装（模板 A/B + 槽位填充）
- `content/compliance.py` — 合规闸门（ERROR 级直接阻断，不可绕过）
- `content/topic_bank.py` — 选题库（35 条，A 线搭系统 / B 线回测科普）
- `content/facts/<选题id>.json` — 真实素材（`{"slots": {...}}`）
- `publish/publisher.py` — Playwright 发布器（自动勾选【笔记含 AI 合成内容】）
- `publish/login.py` — 扫码登录（登录态存 `.browser_profile/`）
- `logs/<日期>_publish.json` — 发布结果日志

## 选题轮转规则
`epoch = (目标日期 - 2026-11-01).days`，`idx = epoch % 35` → `TOPIC_BANK[idx]`。
同一天永远取同一条，不同天不重复。2026-11-02 → A02。

## 槽位约定（已修复，勿回退）
- `DEFAULT_SLOTS` 必须按线拆成 `DEFAULT_SLOTS_A` / `DEFAULT_SLOTS_B`。
  曾因单 dict 中 `method` 键重复，导致 A 线笔记被塞进 B 线方法论。
- 必填槽位见 `REQUIRED_SLOTS`。未被真实素材覆盖的槽位会进 `missing_slots`。
- 正文模板中的口径占位符用 `[[...]]` 哨兵，**不能**用 `{{...}}`——
  `str.format()` 会把 `{{` 折叠成 `{`，导致扫描失效、占位符漏发。
- B 线 4 个口径槽位（`区间` `笔数` `成本假设` `报告字段`）未填会被规则 8b 拦截。

## 关键约束（长期有效）
- **严禁编造**任何回测数据、收益数字、胜率。真实素材只能来自 `content/facts/`。
- **合规闸门拦截时不得绕过**，只能报告拦截原因。
- AI 生成内容依赖发布器自动勾选的【笔记含 AI 合成内容】声明。
- 发布后须**人工**回复评论区前 30 条评论（平台禁止 AI 托管互动）。
- 登录/验证码需人工完成，自动化任务不得代劳。

## 配图脱敏铁律（2026-11-03 血泪教训，勿回退）
- **截图方向性判断必须在 DOM 层做「句子级替换」，不能靠事后模糊。**
  旧规则 `JS_MASK_RISKY_FALLBACK` 有 `t.length > 240` 上限，
  AXIOM 日报宏观长段落（300~500 字）**整段逃逸**，
  导致"华鑫策略认为A股处于震荡筑底阶段，建议维持防御型哑铃配置…"
  明文进过已发布配图。现由 `JS_MASK_RISKY_SENTENCE` 从根上堵住。
- **任何遮蔽规则都不得只在"正常路径"生效，回退路径必须同样执行。**
  历史上「糊过头→回退」把 dashboard 右栏研报遮蔽整块丢掉，
  回退后的图比正常路径泄漏更多（15 条研报原文全露）。
  现已抽出 `_mask_reading_column()` 并通过 `on_fallback` 回调在回退时执行。
- **配图文案必须与图上实际可见内容一致。**
  曾写"每段都标了置信度"，但徽章只渲染在右栏（x≥1204），
  日报卡片内根本不显示 —— 属文案失实。
  改文案前必须先按 3:4 裁剪坐标验证可见范围。
- 词表只收**构成"行情判断/操作建议"语义**的词；
  纯描述性宏观词（预期/主导/传导/持续/延续）**刻意不收**，
  否则整段宏观速览被糊掉，图片失去演示价值。
- **叶子判定必须严格 + 几何护栏（2026-11-04 补齐）。**
  `JS_MASK_LEAF_TITLES` 原用 `el.children.length > 1`，让 kids=1 的**根容器**
  通过叶子检测 → textContent 含【/## → 模糊整页 → 回退图纯白（edge ratio 0.00649）。
  现为严格叶子 + 矩形过大跳过，intel_list edge ratio 回升到 0.042，表格可读。
- **海外个股名 / 涨幅 / 方向性表述必须进遮蔽词表或走句子级替换（2026-11-04 补齐）。**
  `KNOWN_COMPANIES` 只覆盖 A 股，Inovio/Voceryq 这类海外名完全不命中；
  "大涨"也不在 RISKY 词表 → intel_detail 弹窗正文整段明文可见。
  现由 `JS_MASK_DIALOG_PROSE` 整段替换（≥12 字且非结构标签）。
  **教训：词表是兜底，结构标签识别才是根治。**

## 发布去重铁律（2026-11-03 补齐，2026-11-10 修正）
- **`publish/publisher.py` 的 `_published_index()` 是硬闸门**：
  扫描 `logs/` 索引已发布 topic_id + 标题，重复则在发布动作前拦截。
- **`--index` 默认改为 None = 自动挑选第一篇未发布过的**（topic_id + 标题双重去重）。
- **同一自然日只发一篇**（`_published_today()`），已有成功记录则跳过并写
  `logs/<date>_axiom_skipped.json`。
- **「一天一篇」必须按平台隔离（2026-11-10 修复）。**
  原 `_published_today()` glob `{day}_*.json`，
  会把同日的**抖音/淘股吧日志**也算进来 →
  2026-11-10 抖音发布成功导致小红书被误判为「当日已发布」而跳过，
  报 `stage=dedup`，**掩盖了真实原因（账号封禁）**。
  现按日志 `platform`/`source` 字段过滤 douyin / tgb，文件名后缀兜底，
  与 `_douyin_published_today()` / `_tgb_published_today()` 保持一致。
  → 以后新增平台时，**每个平台的去重必须只认自己的日志**。
- 历史教训：11-02 17:43 提前发了 11-03 的内容，次日再跑就撞标题 → 平台判刷量。
- 发布节奏：约 17:4x 发布**次日**标注的内容
  （11-02 17:43→11-03，11-03 17:48→11-04，11-04 17:2x→11-05）。
  故跑真实发布必须显式带 `--date <次日>`，用默认当天日期会被 `_published_today()` 拦掉。
- `source/_probe_*.py` 是排查 AXIOM 页面结构的临时脚本，**用完必须删**，勿留在仓库。

## 🔴 平台风控：账号已被限制发文（2026-11-05，最高优先级）
- 平台对历史笔记的处置理由：**「笔记存在利用AI托管进行发文/互动的内容」**，
  明确指出「平台严格禁止任何利用技术手段模拟真人、进行非真实内容创作或互动的行为」。
  已处置笔记 4 篇（笔记管理页显示「未通过」，且「该篇笔记已不可被他人查看」）。
- 封禁表现：点「发布」后**立刻**弹 toast
  **「因违反社区规范禁止发笔记」**，约 8 秒后自动消失；
  内容全填好、URL 不跳转、笔记不落库。**发布页本身没有任何横幅或提示。**
- **已用对照实验证明是账号级、不是内容级**：
  换 benign 选题 `AX-agent-howto`（讲 AI 工作流、无行情判断）走同一流程，
  得到完全相同的封禁 toast。
- **平台风控是滞后的**：日志里 `published: true` ≠ 笔记最终存活。
  11-04 17:21 那次「发布成功」，其历史兄弟笔记是事后才被判「未通过」的。
  **每次发布后必须回查笔记管理页的「未通过」计数**，不能只看返回值。
- 代码侧已加固（`publish/publisher.py`）：
  - `wait_publish_result` 的 error_markers 增 `违反社区规范`/`禁止发笔记`；
    并新增 `_JS_COLLECT_TOASTS` 从 fixed/absolute 浮层单独捞 toast
    （`inner_text("body")` 取不到，少这步必然误报成 timeout）。
  - 命中时返回 `stage=account_blocked`（带 error + hint），不混作普通发布失败。
  - 新增 `_account_blocked()` 预检：读 logs/ 近 7 天的 account_blocked 记录，
    命中即**在填表前**拦截，不走完整填表+点击流程，避免反复触发更重处置。
    解禁后删掉对应封禁日志即可自动放行，无需改代码。
- **自动发布链路与平台规则存在根本冲突**（平台禁止 AI 托管发文/互动），
  是否继续走自动化需人工决策；申诉期间不要用脚本发文。
- **只读核查笔记管理页时，`inner_text("body")` 首次可能返回空串**
  （页面异步渲染未完成），易被误判为「笔记已清空」。
  必须**轮询等待 body 长度 > 50 再取值**（3 秒一次，最多 6 次）。

## 已知现状
- `content/facts/` 目前只有 README.md，无任何选题 JSON →
  每天走「默认骨架 + 8 个待补槽位」路径，正文为通用文案而非真实经历。
- 发布时段表 `PUBLISH_SLOTS = ["11:30","12:10","18:30","19:30","20:10"]`。
- 创作服务平台登录页与发布页 URL 相同（均为 `/publish/publish`），
  判断登录态**只能靠元素特征**，不能看 URL。
- `intel_list` / `agent` / `dashboard` 会触发「糊过头」回退（边缘比 < 0.12），
  属已知可接受兜底 —— 回退路径现已保证安全。
- `intel_list` 回退后几乎全白，配图信息量低
  （2026-11-04 已修叶子判定，回退图恢复可读，edge ratio 0.042）。
- **正文不得残留 Markdown 标记**（`**统计信号**` 等）：
  小红书不渲染 Markdown，会显示字面星号。
  11-09 教训：**手工删一次不是根治** —— 11-04 清过 `gen_from_axiom.py`，
  11-06 新增的 sample 模板分支又把 `**` 带回来了。
  现由 `content/compliance.py` 规则 10b（ERROR 级）拦截
  `**` / `#` / `>` / ``` / `[链接](url)`，漏了就发不出去。
  **新增模板分支时先确认正文无 `**`。**

## 验证命令
```bash
cd E:/WorkbuddyWork/2026-11-01-18-31-53/xhs
"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" content/test_compliance.py   # 应 12/12
"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" scheduler.py --dry-run
```
