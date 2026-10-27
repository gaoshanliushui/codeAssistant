# CodeAssistant · 基于 LangGraph 的多专家代码助手

一套**运行时可动态装配**的多 Agent 编排框架，采用「专家（Expert）— 专家团（Team）— 主编排器（Supervisor）」三层架构，
在它之上提供了一套真实的代码助手工具链（文件读写、代码检索、命令执行、Git）。

```
用户请求
   │
   ▼
主编排器 Orchestrator ── 意图路由 ──┬─▶ 专家子图（独立编译 / 独立状态 / 独立检查点）
   │                                │
   │                                └─▶ 专家团子图
   │                                        ├─ Supervisor：主理人按条件边动态分派
   │                                        └─ Parallel：Send 扇出并行汇
   │
   └── 每个成员本身就是一个专家子图（可嵌套）
```

---

## 快速开始

```bash
# 1. 安装（pypi.org 直连不通时加镜像）
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[all,dev]"

# 2. 看看有哪些专家和专家团
codeassistant experts

# 3. 单次执行（默认 mock 模型，无需任何 API Key）
codeassistant run "帮我看看这个项目的结构"

# 4. 交互式会话
codeassistant chat

# 5. 启动 Web 服务
codeassistant serve --port 8765
```

接入真实模型：

```bash
export CODEASSISTANT_PROVIDER=openai
export CODEASSISTANT_MODEL=deepseek-chat
export CODEASSISTANT_BASE_URL=https://api.deepseek.com/v1
export CODEASSISTANT_API_KEY=sk-xxx
```

`anthropic` 同理（`CODEASSISTANT_MODEL=claude-sonnet-4-5`）。完整配置见 `.env.example`。

---

## 命令

| 命令 | 说明 |
|------|------|
| `codeassistant chat` | 交互式会话，高危操作在终端里逐条审批 |
| `codeassistant run "任务"` | 执行一次任务，`--expert` / `--team` 可跳过路由，`--json` 输出结构化结果 |
| `codeassistant experts` | 列出所有专家与专家团（含工具白名单与人审点） |
| `codeassistant skills` | 列出技能及其绑定到哪些专家 |
| `codeassistant reload` | 热重载专家包，变更的专家图自动失效重建 |
| `codeassistant serve` | 启动 FastAPI + SSE Web 服务 |

全局参数：`--workspace`（沙箱根目录）、`--provider`、`--model`、`--checkpointer`。

---

## 三层架构

### 专家（Expert）—— 独立编译的 LangGraph 子图

每个专家封装成一张**单独编译**的图，拥有独立的 State Schema、Checkpointer 与中断逻辑，
因此可以脱离主编排器单独编译、单独测试、整体替换。

```
START → plan → act ─┬─(有工具调用)→ execute_tools → act
                     └─(无工具调用)→ finalize → END
```

- 手写 ReAct 循环，不依赖 `create_agent` 预构建，模型后端（OpenAI / Anthropic / Mock）可互换
- 工具按专家白名单裁剪，命中高危工具时进入人工审批
- 图工厂以 `(name, config_hash)` 为键缓存已编译的图；配置变更 → hash 变更 → 缓存失效并热更新

### 专家团（Team）—— 专家的编排层

两种协作模式，共享同一套状态协议与成员调用逻辑：

- **Supervisor**：主理人（lead）用 LLM 决定下一位派给谁，成员产出**必须**回传主理人再转交下一位
  （跨成员信息流一律经主理人中转，成员之间不直连），主理人最终汇编交付报告
- **Parallel**：`Send` API 把同一任务扇出给所有成员并发执行，reduce 节点汇总

成员在图里是**原生子图节点**（每个成员专家子图直接 `add_node` 进专家团图），
所以成员内部的人工审批能一路冒泡到前端；并行模式下多个成员同时挂起时，
用「中断 ID → 审批结论」的映射一次性恢复。

模式可以在专家包里用 `agent-tools.json` 声明（见下），也可以用 `--mode` 临时覆盖。

### 主编排器（Orchestrator）—— 意图路由

只做路由与汇总，不亲自产出专业内容：

| 路由 | 触发条件 |
|------|----------|
| `expert` | 命中某个单体专家 |
| `team` | 命中某个专家团 |
| `direct` | 没有更合适的专家，交给内置代码助手 |

路由优先用 LLM 判断（工具调用），模型不可用时退回关键词规则，离线也能工作。

---

## 统一契约层

三层之间只通过 `codeassistant/contracts.py` 的协议通信，映射是**显式函数**而非隐式字段透传：

```python
ExpertInput  = {task, context, upstream, session_id}
ExpertOutput = {expert, status, summary, artifacts, findings, next_action, turns_used}
TeamInput    = {task, context, members, mode, session_id}
TeamOutput   = {team, status, summary, deliverables, transcript, rounds}

team_input_to_expert_input()      # 团级任务 → 成员输入
expert_output_to_deliverable()    # 成员输出 → 团队交付物
merge_team_outputs()              # 成员产出 → 团队结论（状态按失败/需人工/部分/成功归并）
```

---

## 记忆隔离

三层命名空间的 `thread_id`，多用户 / 多专家并行互不干扰：

| 层级 | thread_id | 用途 |
|------|-----------|------|
| 会话级 | `s:<session_id>` | 主编排器的路由与总结记忆 |
| 专家级 | `e:<expert>:<session_id>` | 单个专家跨轮次的记忆 |
| 团队级 | `t:<team>:<session_id>` | 专家团协作过程的记忆 |

其上再叠加 `checkpoint_ns`，区分同一张图内部的节点。

专家作为**顶层目标**运行时用自己的 thread（`e:<expert>:<session>` + `expert:<name>` 命名空间）；
作为专家团成员运行时挂在专家团的 thread 下，用 `member__<成员名>` 节点名做命名空间隔离。
两种情况下成员的检查点都不会互相串。

---

## 人工审批（HITL）

高危工具（`write_file` / `edit_file` / `delete_file` / `run_command`）在执行前会挂起，
支持两种粒度：

- **dynamic**（默认）：在工具执行节点内逐个调用 `interrupt()`，逐条通过 / 拒绝，
  被拒的调用记入 `findings` 并继续循环 —— **支持从子图内部断点恢复**
- **static**：整批工具调用共享**一次** `interrupt()`，一次确认放行全部（或全部拒绝）

### 两阶段：先审批，后执行

工具执行节点内部严格分成两个阶段：

1. **审批阶段** —— 逐个（或整批）请求人工放行，可能在此中断；
2. **执行阶段** —— 所有审批都通过后才真正执行工具。

这是必须的：LangGraph 恢复被中断的节点时会把整个节点**从头重跑**，
如果审批与执行交错，已经跑过的工具会被重复执行（重复部署、重复写入…）。
两阶段分离保证副作用只发生一次。

### 中断如何冒泡

专家团成员、专家团、主编排器之间是**原生子图节点**（`add_node("member__x", expert_graph)`）
而不是在节点里手工 `ainvoke`。这一点很关键——只有原生子图节点，
中断才能从成员专家一路冒泡到前端；手工调用时子图的挂起会被静默吞掉，
父图会误以为成员已经完成。

另外还有一个编译期闸门 `ExpertConfig.interrupt_before_tools`：
打开后专家图会以 `interrupt_before=["execute_tools"]` 编译，在工具执行节点之前整体暂停一次。
注意 `interrupt_before` 的暂停**不会**从嵌套子图向上冒泡，
所以它只建议在「专家作为顶层目标」运行时启用。

CLI 在终端里逐条询问；Web 端弹出审批卡片，POST `/api/approvals` 后从断点继续。

---

## 代码助手工具与沙箱

所有**文件类**工具的路径都被限制在 `--workspace` 指定的根目录内：
先 `resolve()`（顺带解析符号链接），再校验是否仍在根目录内，越界一律拒绝——
**即使拿到人工审批也不放行**。`search_code` 也会跳过指向工作区之外的符号链接。

| 工具 | 风险 | 说明 |
|------|------|------|
| `read_file` / `list_files` / `search_code` / `ast_inspect` | low | 读文件、列目录、字面量检索、Python AST 解析 |
| `git_status` / `git_diff` | low | 只读 Git |
| `write_file` / `edit_file` / `delete_file` | high | 写操作，需审批 |
| `run_command` | high | 执行 shell，需审批 |

工具白名单在**执行期**同样生效：模型即使幻觉出白名单外的工具名（比如只读专家想调
`delete_file`），也会被拒绝执行，而不是悄悄放行。

关于 `run_command` 的边界，说清楚一点：

* 工作目录被限制在工作区内；
* 但**命令本身**不受文件系统沙箱约束（`cat /etc/hosts` 是能读到的）——
  这是 shell 的固有性质，靠「必须人工审批」这一层来兜底；
* 另有一层永远禁止的命令黑名单（`rm -rf /`、`mkfs`、fork 炸弹等），
  即便人工审批通过也不放行。但黑名单只能挡住明显写法，无法穷尽所有变形；
* 子进程环境变量里的敏感项（名字含 KEY / TOKEN / SECRET / PASSWORD 等）会被剔除，
  避免 API Key 随命令输出泄露进模型上下文。

---

## 专家包格式

直接复用仓库里已有的 codebuddy 专家包（`.codeassistant/my-experts/plugins/*`），无需改造：

```
.codeassistant/my-experts/plugins/<plugin>/
├── .codebuddy-plugin/plugin.json   # 包元数据：expertType / teamInfo / members / displayName …
├── agents/<agent>.md                # agent 定义：YAML frontmatter + 正文提示词
└── agent-tools.json                 # （可选）工具绑定与审批策略覆盖
```

专家包放在 `.codeassistant/` 应用数据目录里（和状态库 `state.db` 同级），
路径可用 `CODEASSISTANT_EXPERTS_DIR` 覆盖。

`plugin.json` 里 `expertType` 为 `agent` 表示单体专家，为 `team` 表示专家团
（`teamInfo.leadAgent` + `teamInfo.memberAgents`）。

### 工具绑定策略（三级）

1. 包内 `agent-tools.json` 显式声明（优先级最高）
2. 按职位关键词的内置映射表（如「后端开发工程师」→ 文件 + 搜索 + 命令执行）
3. 兜底最小安全集（只读）

```json
{
  "sdt-backend": {
    "tools": ["read_file", "search_code", "write_file", "edit_file", "run_command"],
    "hitl": { "mode": "dynamic", "default_action": "ask" }
  },
  "_team": { "mode": "parallel", "max_rounds": 10 }
}
```

### 内置专家

即使一个专家包都没有，系统也有内置的 `code-assistant`（通用代码助手），
主编排器的 `direct` 路由会用到它。

---

## 技能（Skill）

技能是「可按需载入的能力单元」，采用**渐进式披露**：系统提示里只放技能清单
（名称 + 一句话描述），模型判断需要时再调用 `load_skill` 载入完整说明——
这样技能再多也不会撑爆上下文。

```
.codeassistant/skills/<技能名>/
├── SKILL.md            # 必需：frontmatter + 给模型看的正文
├── scripts/*.py        # 可选：随技能附带的脚本
└── references/*        # 可选：补充资料
```

技能文件带 YAML frontmatter：

```markdown
---
name: code-review
description: 对指定代码做结构化评审，输出按严重级别排序的问题清单
agents: [code-assistant, sdt-backend]   # 可选：绑定到哪些专家
default: false                           # 可选：true = 绑定给所有专家
---

# 代码评审
…给模型看的使用说明…
```

### 绑定规则（按优先级）

1. 专家包 `agent-tools.json` 里显式列出的技能 —— 一定绑定；
2. frontmatter 声明了 `agents` 的技能 —— 只绑给列出的专家；
3. frontmatter 声明了 `default: true` 的技能 —— 绑给所有专家；
4. 都没声明的技能 —— 绑给通用型专家（内置 `code-assistant`）。

没绑定的技能模型看不到也载不到，避免在无关技能之间游走。

### 三个技能工具

| 工具 | 风险 | 作用 |
|------|------|------|
| `list_skills` | low | 列出可用技能 |
| `load_skill` | low | 载入技能完整说明 |
| `run_skill_script` | high | 执行技能自带脚本，**需人工审批** |

`run_skill_script` 的脚本只能来自该技能自己的 `scripts/` 目录（路径二次校验），
执行环境剔除敏感环境变量，与 `run_command` 一样受沙箱与审批约束。

### 用 `/技能名` 显式调用

除了让模型自己判断要不要用技能，还可以在输入框里直接点：

```
/code-review 帮我看看这次改动有没有问题
```

* 网页输入框里打 `/` 会弹出技能清单（↑↓ 选择、Enter 确认、Esc 关闭），选中的技能名会插进输入框；
* 任务文本里的 `/技能名` 会被专家解析出来，**技能正文直接带进给模型的提示**，
  模型不必再判断「要不要载入这个技能」；
* 只在**该专家绑定的技能**里匹配——别的专家的技能点不动；
* `/usr/local/bin` 这种路径不会被误当成技能调用（前是空白或开头、后是空白或结尾才算）。

### 现成的示例技能

`.codeassistant/skills/` 下已经放好三个：

- `code-review` —— 结构化代码评审（绑定 sdt-backend / sdt-frontend / sdt-qa）
- `commit-message` —— 按 Conventional Commits 生成提交信息
- `code-stats` —— 统计各语言代码行数（自带 `scripts/stats.py`）

包内技能放在 `<专家包>/skills/`，同样支持 `agents` 绑定。

---

## Web 服务

| 接口 | 说明 |
|------|------|
| `GET /` | 单页界面（零构建、零前端依赖） |
| `GET /api/catalog` | 专家 / 专家团 / 技能目录 |
| `POST /api/chat` | SSE 流式执行，按专家维度实时吐进度 |
| `POST /api/approvals` | 对挂起的高危操作给出通过 / 拒绝 |
| `POST /api/reload` | 热重载专家包 |
| `GET /api/sessions` | 会话列表（最近更新的在前） |
| `GET /api/sessions/{id}` | 某个会话的完整对话记录 |
| `GET /api/experts/{name}` | 某个专家的完整档案（含系统提示词、工具清单） |
| `GET /api/teams/{name}` | 某个专家团的完整档案（含成员清单） |
| `GET /api/skills/{name}` | 某个技能的完整档案（含 SKILL.md 正文） |

界面结构（单文件 `web/index.html`，无前端依赖）：

* **左侧栏**：新建任务 / 专家·技能 / 更多（热重载、复制工作区路径、服务信息），
  下方是「最近」会话列表（服务端持久化），底部显示工作区路径与当前模型；
* **目录页**（点「专家·技能」在右侧打开）：顶部分页签（专家团 / 专家 / 技能）+ 搜索框，
  下面是标签筛选条与「综合 / 最热 / 最近」排序，主体是卡片网格；
  **点卡片在右侧展开详情**（主从布局）：专家看标签 / 工具 / 审批点 / 绑定技能 / 系统提示词，
  专家团看成员清单（主理人排第一），技能看绑定范围 / 脚本 / SKILL.md 正文；
  详情底部可以直接「用这个专家开始任务」或「在任务中调用」技能；
* **首页**：大标题 + 「专家 / 专家团」分段切换 + 一排快捷 chip（点 chip 即选定目标）+
  大输入框 + 建议卡片；发消息后切换为对话流，输入框吸底，专家进度以状态条呈现；
* **输入框打 `/`**：弹出技能清单，选中的技能名插进输入框（详见「用 `/技能名` 显式调用」）；
* **最近会话**：点开某条即从服务端拉回整段对话记录（`GET /api/sessions/{id}`），
  并把当前上下文切到那个会话——可以接着聊，专家能看到上文。

### 会话记录

每次对话都会写进 `.codeassistant/sessions.json`（原子写入，可直接打开查看/编辑），
记录的是**给人看的对话流水**：谁说了什么、哪个专家产出了什么结论。
它与 LangGraph 的检查点（`.codeassistant/state.db`）是两回事——
检查点存的是图的内部状态（消息、迭代计数），会话记录存的是可回看的历史。

SSE 事件统一为 `{type, expert, team, node, payload, ts}`，
`expert` 字段就是前端按专家维度展示进度的依据。

---

## 项目结构

```
codeassistant/
├── settings.py        # 全局配置（env / .env）
├── contracts.py       # ★ 统一契约层 + 显式映射适配器
├── configs.py         # ★ 声明式 ExpertConfig / TeamConfig / ToolSpec / HitlPolicy
├── loader.py          # 专家包解析（plugin.json + agent md frontmatter）
├── skills.py          # ★ 技能模型与注册表（渐进式披露）
├── registry.py        # 专家注册表、工具/技能绑定策略、热重载
├── llm.py             # LLM 抽象：OpenAI 兼容 / Anthropic / 离线 Mock
├── memory.py          # ★ 三层 thread_id 命名空间 + 混合 SQLite 检查点
├── sessions.py        # 会话记录（可回看的对话流水，JSON 持久化）
├── approval.py        # ★ 人审中心
├── observability.py   # 统一流式事件
├── app.py             # 应用门面（CLI / Web 共用）
├── cli.py / server.py # 命令行 / Web 入口
├── tools/             # 沙箱、代码工具、Git 工具、技能工具、注册表
└── graphs/
    ├── expert.py          # ★ 专家子图（独立 State Schema）
    ├── factory.py         # ★ 图工厂：(name, config_hash) 编译缓存 + 热更新
    ├── supervisor_team.py # ★ Supervisor 专家团
    ├── parallel_team.py   # ★ Map-Reduce 并行专家团
    └── orchestrator.py    # ★ 主编排器
```

---

## 测试

```bash
pytest -q
```

测试用假 LLM（`MockChat` 脚本模式）替换真实模型节点，用临时目录当沙箱，
**不触网、不需要 API Key**。覆盖：专家包加载、工具与技能绑定、编译缓存与热更新、
专家子图契约、HITL 通过 / 拒绝与断点恢复、两种团队模式、记忆隔离、沙箱越界、
技能发现/载入/脚本审批、主编排器路由。

---

## 扩展：加一个自己的专家

```bash
mkdir -p .codeassistant/my-experts/plugins/my-team/.codebuddy-plugin .codeassistant/my-experts/plugins/my-team/agents
```

`plugin.json`：

```json
{
  "name": "my-team",
  "expertType": "team",
  "agentName": "my-team-lead",
  "teamInfo": { "leadAgent": "my-team-lead", "memberAgents": ["my-worker"] },
  "displayName": { "zh": "我的专家团" },
  "description": "示例专家团"
}
```

`agents/my-worker.md`：

```markdown
---
name: my-worker
description: 一位后端工程师。
displayName:
  zh: "小工"
profession:
  zh: "后端工程师"
maxTurns: 50
---

你是一位后端工程师……
```

重新加载即可生效（`codeassistant reload`，或 Web 端侧栏「更多 → 热重载专家包」）。

---

## 扩展：加一个自己的技能

```bash
mkdir -p .codeassistant/skills/my-skill/scripts
```

`.codeassistant/skills/my-skill/SKILL.md`：

```markdown
---
name: my-skill
description: 一句话说清这个技能能干什么（会出现在技能清单里）
agents: [code-assistant, my-worker]   # 不写就只归通用型专家
---

# 我的技能

1. 第一步……
2. 第二步……

需要跑脚本时：
run_skill_script(skill_name="my-skill", script="helper.py", arguments="--flag")
```

脚本放在 `scripts/` 下，只能用 `run_skill_script` 调用（会自动过人工审批），
路径不能越出技能自己的目录。

`codeassistant skills` 可以查看技能及其绑定情况。
