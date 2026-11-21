# 日更发布自动化 · 执行记录

## 2026-11-02（首次运行）

**结果：发布失败（stage=login），未发布任何内容。**

- 选题 A02《AI甩锅「风格切换」，我拉了数据》，发布时段 18:30，封面 cover_A02.jpg
- 合规闸门：通过，0 violations，1 warning（配图 1 张，低于最优 6-9 张）
- 失败阶段：`login` —— 登录态失效
- 根因：`.browser_profile/Default/Cookies` 不存在，即从未完成过扫码登录
- 待人工动作：运行 `publish/login.py` 完成扫码/短信登录后重跑 `scheduler.py`

### 本次修复的 3 个缺陷

1. **`compose.py` 槽位串线**：`DEFAULT_SLOTS` 单个 dict 中 `method` 键重复定义，
   后者（B 线方法论）覆盖前者（A 线方法论），导致 A 线笔记被塞进回测方法论。
   已拆分为 `DEFAULT_SLOTS_A` / `DEFAULT_SLOTS_B`，并新增 `REQUIRED_SLOTS`。
2. **`missing_slots` 对 A 线恒为空**：模板 A 使用单花括号槽位，`fill_template`
   只扫描 `{{...}}`，故 A 线永远报 0 待补。现改为按线扫描必填槽位，
   未被真实素材覆盖的槽位会如实列出。
3. **B 线口径占位符不被拦截**：README 声称口径槽位未填会触发拦截，实际放行。
   根因是 `str.format()` 会把 `{{...}}` 折叠成 `{...}`，扫描不到。
   已将正文模板改为 `[[...]]` 哨兵，并在 compliance.py 增加规则 8b
   「真实素材槽位未填写」（ERROR 级）。合规自测 11/11 → 12/12。

### 附带修复

- `publish/login.py` 登录成功判定用 `if "login" not in page.url`，
  但创作服务平台登录页与发布页 URL 同为 `/publish/publish`，该条件永不成立，
  脚本会一直空转。已改为基于页面元素特征判断（与 publisher.py 的
  `is_login_wall` 一致），并加 5 分钟超时。

### 下次运行注意

- 登录态修好后重跑 `scheduler.py` 即可，无需改动代码
- `content/facts/` 目前只有 README.md，无任何选题 JSON，
  每天都会走「默认骨架 + 8 个待补槽位」路径；
  补齐 `content/facts/<选题id>.json` 才能让正文带上真实素材

## 2026-11-04（第二次运行）

**结果：发布成功（stage=done，published=true）。**

- 选题 `AX-2026-11-03-intel`，标题《信息太多时，稀缺的不是信息，是结构》
- 发布标注日期：`2026-11-05`（沿用既定节奏 —— 傍晚 17:4x 发次日标注内容）
- 配图 2 张：`cover_AX-2026-11-03-intel.jpg` + `shot_intel_list.jpg`（AXIOM 情报精选真实界面，DOM 层脱敏）
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张，低于最优 6-9 张）
- AI 合成内容声明：已勾选（`ai_declared: true`）
- 正文 520 字，标签 6 个，无 Markdown 残留
- 日志：`logs/2026-11-05_axiom_publish.json`

### 本次修复的脱敏缺陷（配图泄漏）

1. **intel_detail 弹窗散文泄漏**：海外个股名（Inovio/Voceryq）、涨幅（213%）、
   方向性表述直接明文可见 → 新增 `JS_MASK_DIALOG_PROSE`，
   弹窗内长正文（≥12 字且非结构标签）整段替换为
   「（该条涉及个股与方向性判断，已隐去）」，标题同步替换，
   仅保留「核心观点/推理前提/结论/中层观点/性质/影响/时间/空间」等结构标签。
2. **intel_list 回退纯白**：`JS_MASK_LEAF_TITLES` 原判定 `el.children.length > 1`，
   使 kids=1 的根容器通过检测 → textContent 含【/## → 模糊整页。
   改为严格叶子判定 + 几何护栏（矩形过大直接跳过），
   同时该规则与 `JS_MASK_INTEL_LIST` 同时接入正常路径与 `on_fallback` 回退路径。
3. **配图文案失实**：intel_list 表头实为
   标题/作者/群组名称/一句话总结/版本号/原文/收录时间/发布日期（无分类列），
   原文写「分类统计告诉我哪类信息突然变多」与图不符 → 已改写为与可见列一致的说法。
4. **正文 Markdown 残留**：`**统计信号**` 等粗体标记在小红书不渲染，
   会显示字面星号 → `source/gen_from_axiom.py` 全部去掉粗体。

### 下次运行注意

- 探针脚本 `source/_probe_*.py`（8 个临时排查脚本）已全部清理，
  排查 AXIOM 页面结构时如需新建，用完同样要删。
- 发布节奏保持：约 17:4x 发布**次日**标注的内容，
  命令为 `scheduler.py --source axiom --date <次日>`。
- 发布后仍需人工回复评论区前 30 条评论（平台禁止 AI 托管互动）。

## 2026-11-05（第三次运行）

**结果：发布失败（stage=account_blocked），账号被平台限制发文，未发布任何内容。**

- 目标日期标注 `2026-11-06`，选题 `AX-2026-11-04-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 配图 2 张：封面 + `shot_intel_detail.jpg`（已核验脱敏有效，仅结构标签 + 占位符）
- 失败阶段：`account_blocked` —— 点「发布」后立刻弹
  **「因违反社区规范禁止发笔记」**，约 8 秒后消失，笔记不落库

### 根因：账号级封禁，非内容问题

平台处置理由（读自笔记管理页「查看修改建议」→ 笔记审核详情）：
**「笔记存在利用AI托管进行发文/互动的内容」**，明确禁止「利用技术手段
模拟真人、进行非真实内容创作或互动」。历史 4 篇已判「未通过」且不可查看。

**对照实验**：换 benign 选题 `AX-agent-howto`（AI 工作流、无行情判断）
走同一流程，得到**完全相同**的封禁 toast → 证明是账号级限制。
发布页本身无任何横幅，封禁只在点击那一刻暴露。

### 本次修复（publish/publisher.py）

1. error_markers 增 `违反社区规范`/`禁止发笔记`/`社区规范禁止`
   —— 原先误报成 `timeout`，真实原因丢失。
2. 新增 `_JS_COLLECT_TOASTS`：从 fixed/absolute 浮层单独捞 toast 文本
   （`inner_text("body")` 取不到它）。
3. 新增 `stage=account_blocked` 分支，带 error + hint。
4. 新增 `_account_blocked()` 预检：读 logs/ 近 7 天封禁记录，
   命中即在**填表前**拦截，避免反复触发更重处置；
   解禁后删掉对应封禁日志即自动放行。

合规自测仍 12/12。探针脚本 6 个已全部清理。

### 关键教训

- **平台风控是滞后的**：`published: true` ≠ 笔记最终存活。
  11-04 那次「成功」的笔记其兄弟是事后被判「未通过」的。
  每次发布后必须回查笔记管理页「未通过」计数。
- 平台封禁提示只存在几秒，等待逻辑必须额外采集浮层 toast。
- 判断「内容问题 vs 账号问题」的唯一可靠手段：换一个明显无害的选题跑一遍。
- 截图环节首次失败是 Chrome updater 占用（TargetClosedError），
  **重试一次即恢复**，非代码问题；本次回退发生在
  dashboard / intel_list / agent 三场景，intel_detail 正常。

### 下次运行注意

- `_account_blocked()` 命中时**不会**再发任何内容，会如实报拦截原因。
  申诉/解禁前不要绕过它，也不要换选题重试（无效且可能加重处置）。
- 需人工在 App 内「帮助与客服 → 笔记申诉」处理。

## 2026-11-05（第四次运行，11:52 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期标注 `2026-11-06`，选题 `AX-2026-11-04-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- **本次是 `_account_blocked()` 预检在填表前拦截**，日志只有 3 行：
  浏览器模式 → 已通过登录校验 → ⛔ 预检拦截。
  与 11-05 上午那次（走到点击发布才被弹 toast）不同，**未再触碰发布按钮**，
  未产生新的被处置记录。
- 日志：`logs/2026-11-06_axiom_publish.json`（已覆盖，published=false）

### 关键新发现：11-04 那篇「发布成功」的笔记也已判未通过

只读核查笔记管理页（`/new/note-manager`）确认：
**全部 5 篇笔记均为「未通过」**，含 11-04 17:21 那篇日志里 `published: true` 的
《信息太多时，稀缺的不是信息，是结构》。
处置详情原文（弹窗内）：
> 处置结果：该篇笔记已不可被他人查看 / 处置时间 2026-11-04 16:59
> 存在问题：笔记存在利用AI托管进行发文/互动的内容
> 平台建议：…避免用AI代替真人发文互动等操作…
> 如有异议可前往 小红书App-帮助与客服-笔记申诉 入口申诉

**再次印证：日志 `published: true` ≠ 笔记存活**。账号已被全面处置，
11-04 之后无任何笔记可用。

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与上次一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已核验 `shot_intel_detail.jpg` 与 `shot_intel_list.jpg`：
  可见文本仅结构标签（核心观点/推理前提/结论/pyramid_derived/v2.0）
  与占位符「（该条涉及个股与方向性判断，已隐去）」，无个股名、无涨幅、
  无方向性判断 —— 脱敏有效。
- 数据核验：正文「10 条，总量约 1177 条」与六类计数均来自 AXIOM 接口
  （`meta.total` 真实值 + 真实分类计数），非写死。
- 合规自测 12/12 通过。探针脚本已删除。

### 下次运行注意

- 封禁状态**未见缓解**，预检会持续拦截，直到人工申诉成功并删除
  `logs/2026-11-06_axiom_publish.json` 中的 account_blocked 记录。
- 不要因为「预检拦截」就绕过它去点发布 —— 那只会再产出一篇被处置的笔记。
- 平台规则本身禁止 AI 托管发文/互动，自动化链路与该规则存在根本冲突，
  需人工决策是否继续。

## 2026-11-06（第五次运行，13:28 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期 `2026-11-06`，选题 `AX-2026-11-06-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 日志：`logs/2026-11-06_axiom_publish.json`（覆盖，published=false）
- 预检在填表前拦截，**未再触碰发布按钮**，未产生新的被处置记录

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与前两次完全一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已人工核验 `shot_intel_detail.jpg` / `shot_intel_list.jpg` / `shot_dashboard.jpg`：
  可见文本仅结构标签（核心观点/推理前提/结论/pyramid_derived/v2.0）、
  占位符「（该条涉及个股与方向性判断，已隐去）」、
  「（该段 N 处方向性判断，已隐去）」，无个股名、无涨幅、无方向性判断原文。

### 数据核验（本次新增做法）

- 正文「10 条，总量约 1191 条」与分类计数 NEWS:1 / RESEARCH:5 / TRADING:4
  **与 AXIOM 接口 `research_analysis()` 实时返回值逐项一致**
  （返回条数 10，Counter 一致，createdAt 最新 2026-11-06T04:09:36Z）。
- 注意：分类计数**每天会变**（11-05 是 6 类/1177 条，11-06 是 3 类/1191 条），
  草稿每次从接口实时取数，不存在写死。

### 下次运行注意

- 封禁状态**仍未缓解**（`_account_blocked()` 连续三次命中）。
- 若人工申诉成功，删掉 `logs/2026-11-06_axiom_publish.json` 即可自动放行，
  无需改代码。
- 合规自测 12/12 通过；`source/_probe_*` 无残留。

## 2026-11-08（第六次运行，16:03 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期 `2026-11-08`，选题 `AX-2026-11-06-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 日志：`logs/2026-11-08_axiom_publish.json`（published=false）
- 预检在填表前拦截，**未再触碰发布按钮**，未产生新的被处置记录

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与前三次完全一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已人工核验 `shot_intel_detail.jpg` / `shot_intel_list.jpg`：
  可见文本仅结构标签与占位符「（该条涉及个股与方向性判断，已隐去）」，
  无个股名、无涨幅、无方向性判断原文。

### 数据核验

- 正文「10 条，总量约 1204 条」与分类计数
  COMPANY:1 / NEWS:3 / RESEARCH:3 / TRADING:3
  **与 AXIOM 接口 `research_analysis()` 实时返回值逐项一致**
  （rows=10，meta.total=1204，Counter 一致）。
- 注意：本次最新研报 `createdAt` 仍是 2026-11-06（接口无新增研报入库），
  故正文结构与 11-06 那次一致，但总量从 1191 增至 1204。
  **`source_date` 取的是接口最新条目的 createdAt，不是运行当天** ——
  数据源没更新时，topic_id 也会停留在旧日期（如 AX-2026-11-06-research）。

### 本次新增：只读核查账号状态的方法

- 路径：`publish/browser.py` 的 `open_session()` → 打开
  `creator.xiaohongshu.com/new/note-manager` → 读 `inner_text("body")`。
- 核查结果：**全部 5 篇笔记仍为「未通过」**，11-04 之后无新笔记落库，
  封禁状态**仍未缓解**。发布页可正常打开、登录态有效（login_wall=False），
  但平台不显示任何横幅提示 —— 仍只能靠历史日志判断。
- 临时探针 `source/_probe_status.py` 已删除（遵守「用完必须删」铁律）。

### 下次运行注意

- 封禁状态**仍未缓解**（`_account_blocked()` 连续四次命中）。
- 申诉成功前不要绕过预检 —— 那只会再产出一篇被处置的笔记。
- 平台规则本身禁止 AI 托管发文/互动，自动化链路与该规则存在根本冲突，
  需人工决策是否继续。

## 2026-11-09（第七次运行，12:46 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期 `2026-11-09`，选题 `AX-2026-11-08-sample`
  《看不到样本量的结论，我一律不采信》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 日志：`logs/2026-11-09_axiom_publish.json`（published=false）
- 预检在填表前拦截，**未再触碰发布按钮**，未产生新的被处置记录

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与前四次完全一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已人工核验 `shot_dashboard.jpg`（放大逐行读）：可见「（该句涉及方向性判断，已隐去）」
  「（该段 2 处方向性判断，已隐去）」「（该条涉及方向性判断，已隐去）」及
  事实性 bullet，无个股名、无代码、无涨幅、无方向性判断原文。

### 数据核验

- `daily_count: 69` 与 AXIOM `daily_history()` 去重 `reportDate` 实时值一致
  （最新 reportDate = 2026-11-08）。
- 正文引文为 2026-11-08 日报「A 股行业聚焦」段原文，非写死。

### 本次修复：正文 Markdown 残留复发（根治）

- 11-04 清理过 `gen_from_axiom.py` 的粗体，但 **11-06 新增的 sample 模板分支
  （`:261`）又带回 `**`** → 正文出现字面星号。
- **手工删一次不是根治。** 现于 `content/compliance.py` 新增
  **规则 10b「正文残留 Markdown 标记」（ERROR 级）**，正则覆盖
  `**` / `# 标题` / `> 引用` / ``` / `[链接](url)`，漏了就发不出去。
- `content/test_compliance.py` 补 2 个用例 → **14/14 通过**。
- 选题库 35 条模板已扫描，无 Markdown 残留。

### 只读核查账号状态（第六次）

- `publish/browser.py` 的 `open_session()` → 打开 `/new/note-manager` → 读 `body`。
- **全部 5 篇笔记仍为「未通过」**，最新一篇仍是 2026-11-04 17:21，
  11-04 之后无新笔记落库，**封禁仍未缓解**。
- 临时探针 `source/_probe_status.py` 用完已删除。

### 下次运行注意

- 封禁状态**仍未缓解**（`_account_blocked()` 连续五次命中）。
- 申诉成功前不要绕过预检 —— 那只会再产出一篇被处置的笔记。
- **新增模板分支时先确认正文无 `**`**（规则 10b 会拦，但先写对更省事）。

## 2026-11-10（第八次运行，11:20 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期 `2026-11-10`，选题 `AX-2026-11-09-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 日志：`logs/2026-11-10_axiom_publish.json`（published=false）
- 预检在填表前拦截，**未再触碰发布按钮**，未产生新的被处置记录

### 本次修复：跨平台去重误判（scheduler.py）

- `_published_today()`（小红书）原 glob `{day}_*.json`，
  会把**同日的抖音/淘股吧日志**也算进去。
- 2026-11-10 抖音 10:52 发布成功 → 小红书被误判为"当日已发布"而跳过，
  报 `stage=dedup`，**掩盖了真实原因（账号封禁）**。
- 修复：按 `platform`/`source` 字段过滤 douyin / tgb，文件名后缀兜底；
  与 `_douyin_published_today()` / `_tgb_published_today()` 对齐。
- 回归 6/6 符合预期（11-10 xhs=False、douyin=True；11-09 xhs=False、
  tgb=True；11-04 / 11-02 xhs=True）。
- **教训：去重必须按平台隔离，"一天一篇"不是全平台共用一个额度。**

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与前五次一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已人工核验 `shot_intel_detail.jpg` 与封面
  `cover_AX-2026-11-09-research.jpg`：可见文本仅结构标签与占位符
  「（该条涉及个股与方向性判断，已隐去）」，
  无个股名、无代码、无涨幅、无方向性判断、无站外域名。

### 数据核验

- `research_analysis()` rows=10，`categoryL1` 计数
  COMPANY:1 / NEWS:2 / RESEARCH:4 / TRADING:3 —— 与正文逐项一致。
- `meta.total = 1213` —— 与正文「总量约 1213 条」一致
  （11-08 是 1204，总量每天递增，草稿实时取数不写死）。
- `daily_history()` 去重 `reportDate` = 70 —— 与 `daily_count: 70` 一致，
  最新 reportDate = 2026-11-09（故 source_date 与 topic_id 均为 11-09）。

### 只读核查账号状态（第七次）

- `publish/browser.py` 的 `open_session()` → `/new/note-manager` → 读 `body`。
- **全部 5 篇笔记仍为「未通过」**，最新一篇仍是 2026-11-04 17:21，
  11-04 之后无新笔记落库，**封禁仍未缓解**。登录态有效。
- 临时探针 `source/_probe_status.py` 用完已删除。

### 合规自测

- `content/test_compliance.py` **14/14 通过**。

### 下次运行注意

- 封禁状态**仍未缓解**（`_account_blocked()` 连续六次命中）。
- 申诉成功前不要绕过预检 —— 那只会再产出一篇被处置的笔记。
- 平台规则本身禁止 AI 托管发文/互动，自动化链路与该规则存在根本冲突，
  需人工决策是否继续。

## 2026-11-11（第九次运行，14:41 自动化）

**结果：发布被预检拦截（stage=account_blocked），未提交任何内容。**

- 目标日期 `2026-11-11`，选题 `AX-2026-11-10-research`
  《我存研报改了分类方式，效率差了一个量级》
- 三层合规闸门：PASS，0 violations，0 project_issues，1 warning（配图 2 张）
- 日志：`logs/2026-11-11_axiom_publish.json`（published=false）
- 预检在填表前拦截，**未再触碰发布按钮**，未产生新的被处置记录

### 截图环节（正常）

- 四场景回退（糊过头→最保守脱敏重截）发生在
  `dashboard` / `intel_list` / `agent`，`intel_detail` 正常 —— 与前六次完全一致。
- `compose_shot.py --scene all` 四张 1080×1440 全部生成成功。
- 已人工核验 `shot_intel_detail.jpg` / `shot_intel_list.jpg` /
  `cover_AX-2026-11-10-research.jpg`：可见文本仅结构标签、占位符
  「（该条涉及个股与方向性判断，已隐去）」，无个股名/代码/涨幅/方向性判断/站外域名。

### 数据核验

- `research_analysis()` rows=10，`categoryL1` 计数
  COMPANY:1 / INDUSTRY:1 / NEWS:2 / RESEARCH:4 / TRADING:2 —— 与正文逐项一致。
- `meta.total = 1224` —— 与正文「总量约 1224 条」一致。
- 最新 createdAt = 2026-11-10T11:35:19Z → 故 source_date / topic_id 均为 11-10。
- `daily_count: 71`。

### 只读核查账号状态（第八次）

- `publish/browser.py` 的 `open_session()` → `/new/note-manager` → 读 `body`。
- **全部 5 篇笔记仍为「未通过」**，最新一篇仍是 2026-11-04 17:21，
  11-04 之后无新笔记落库，**封禁仍未缓解**。登录态有效。
- 注：首次读取 body 为空（页面未渲染完），需轮询等待
  `inner_text("body").strip()` 长度 > 50 再取值。
- 临时探针 `source/_probe_status.py` 用完已删除（遵守「用完必须删」铁律）。

### 合规自测

- `content/test_compliance.py` **14/14 通过**。

### 下次运行注意

- 封禁状态**仍未缓解**（`_account_blocked()` 连续七次命中）。
- 申诉成功前不要绕过预检 —— 那只会再产出一篇被处置的笔记。
- 平台规则本身禁止 AI 托管发文/互动，自动化链路与该规则存在根本冲突，
  需人工决策是否继续。

