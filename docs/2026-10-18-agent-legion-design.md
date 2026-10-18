# 产品设计方案:军团 Legion —— AI Agent 军团管理平台

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-18 |
| 状态 | **待评审(评审通过后才进入开发)** |
| 作者 | ZCode 起草,发起人评审 |
| 下一步 | 评审 → 修订 → 技术实施计划 → 开发 |

---

## 0. 一句话定位

一个用软件管理的「AI Agent 军团」指挥系统:把**定目标、招兵、调兵、兵工厂、练兵、复盘**六件事闭环在一个本地优先的 Web 控制台里。先用 10 个 agent 跑通一条真实的赚钱闭环,再逐步扩到 500 个;整套打法本身沉淀为课程内容。

---

## 1. 背景与意图(Why)

发起人原始构想归纳:

- **管理对象是 AI Agent**:给 agent 定目标、补充编制、扩充硬件、按能力分配不同档次的大模型、训练技能、编成小团队。
- **目标来源有两条**:指挥官主动提出;或由 AI 从小红书等内容平台自动捕捉有引流价值的热点,转成候选目标。
- **目标分两类**:💰 赚钱目标(内容引流、自动化变现)、🧠 认知进化目标(把管理体系本身做成课程)。
- **规模目标**:调度系统管理 500 个「士兵」。
- **兵工厂**:把上游各家(Workbuddy / Trae / Opencode 等)的额度中转提炼成一条通用 API;自建云设备(Mac mini、小米安卓等 Linux/Android 设备);接入第三方(Agent37)。
- **训练**:Composio 一键开通 1000+ 应用连接(Gmail、Slack、Notion、GitHub、GCal、Telegram、WhatsApp 等);Composio Provider 适配 OpenAI / Anthropic / LangChain / CrewAI / MCP;Perflo marketplace 按次购买付费工具;自定义 skills / system prompt / 插件,harness 封装成模板复用。

**为什么是现在**:模型能力已越过临界点——瓶颈不在模型,而在**指挥系统**。同一个模型,有没有目标管理、成本记账、绩效淘汰,产出效率差一个数量级。

**成功长相(3 个月后)**:
1. 每天早上打开控制台,3 分钟看完:昨日军团产出、花了多少钱、每个目标推进多少。
2. 至少一条赚钱目标由军团自动半自动运转(热点捕捉 → 内容生产 → 发布 → 数据回流)。
3. 淘汰机制真实跑过:低绩效 agent 被降档/退役,好技能沉淀为模板复用。
4. 整套打法能整理成课程章节。

**失败长相(反模式)**:每个模块都能演示、但没有一条端到端闭环真实产生收入;成本失控不知道钱花在哪;做着做着变成通用 Agent 框架(LangChain 竞品)而不是指挥系统。

---

## 2. 目标 / 非目标 / 反目标

### Goals(本期要达成)
- **G1** 单一控制台管理 agent 全生命周期:招募 → 分配 → 考核 → 晋升/降档/淘汰。
- **G2** 目标闭环:目标可拆解为任务树,任务分配给小队执行,产出数据可回流评分。
- **G3** 统一 API 网关:N 个上游渠道 → 1 条 OpenAI 兼容 API,带路由、熔断、逐请求成本核算。
- **G4** 定时调度:cron 驱动常态化任务(全网扫描、定时报告、夜间备份),支持错峰与夜间窗口。
- **G5** 技能资产化:skills / prompt / harness 模板可安装、版本化、批量装卸、跨 agent 复用。
- **G6** 设备编队:Mac mini / Android / Linux 设备注册、心跳健康检查、按能力标签调度。
- **G7** 全程计量:每个 agent / 任务 / 目标的 token 数与人民币成本,可出账单。
- **G8** 课程导出:复盘记录 + 流程模板可一键整理为课程素材。

### Non-goals(本期明确不做)
- **N1** 不做对外多租户 SaaS(课程学员消费课程内容,不直接使用系统)。
- **N2** 不自研/微调大模型,「训练」仅指提示词、技能、工具配置层面的迭代。
- **N3** 不做移动端 App(Web 控制台即可)。
- **N4** 不做通用工作流编排平台,只覆盖 agent 任务场景。

### Anti-goals(出现即判失败)
- **A1** 没有任何一条真实赚钱闭环在跑。
- **A2** 单月 API/设备成本超出预算且无人收到告警。
- **A3** 自动发布导致账号封禁且没有风控预案。

---

## 3. 用户与核心场景

**主用户**:发起人本人(指挥官),单人使用,本地部署。
**次用户(Phase 3)**:课程学员(只读消费导出的课程,不接触系统)。

| # | 场景 | 描述 |
|---|------|------|
| S1 | 早间战报 | 打开控制台:昨日产出清单、成本、异常、目标进度 |
| S2 | 主动立目标 | 「小红书粉丝 1 万」→ 自动拆成选题/写作/排版/发布/回流任务 → 组建内容小队 |
| S3 | 情报站 | 夜间 cron 抓小红书热点 → 评分 → 早晨推送 5 条候选 → 一键立项 |
| S4 | 扩军入编 | 新 Mac mini 到手 → 装 runner 注册 → 分配 50 个 agent → 健康检查通过 |
| S5 | 分层调兵 | 「高价值客户回复」派 S 级(高档模型),「全网扫描」派 C 级(便宜量大) |
| S6 | 练兵考核 | 写「爆款标题」skill → 装到 20 个 agent → 用历史爆款数据跑 eval → 优秀者入库 |
| S7 | 月末淘汰 | 绩效结算:C 级降档便宜模型、连续 C 级退役、装备(skills)回收 |
| S8 | 课程输出 | 把「情报→选题→生产→发布」链路的复盘导出为一章课程草稿 |

---

## 4. 产品结构:六大模块

一个本地 Web 控制台,六个一级导航:

```
指挥部 Dashboard │ 目标 Goal Studio │ 兵营 Barracks
作战室 Dispatch  │ 兵工厂 Arsenal   │ 讲武堂 Academy
```

### M1 指挥部(战报总览)
- 今日卡片:活跃目标数、在编 agent 数、今日任务数/成功率、今日成本(¥)、今日产出清单(内容链接/报告)。
- 异常面板:失败任务 Top、上游渠道熔断告警、设备离线、预算余量。
- 目标进度:每个目标完成度 + 近 7 日趋势。

### M2 目标 Goal Studio
- 目标 = objective + 类型(💰/🧠)+ KPI + deadline + 预算上限 + 绑定小队。
- **目标拆解**:由指挥档模型把目标拆成任务树(mission → task),人工可增删改。
- **情报站 Radar**(M2 的灵魂):
  - Phase 1 数据源:小红书;预留抖音/B站/即刻。
  - 抓取跑在「斥候 agent」上(低档模型 + 爬虫工具),夜间 cron 低频执行。
  - 评分卡(0-10 × 权重):引流价值 / 竞争密度 / 与现有目标匹配度 / 可执行性。
  - 产出「线索 lead」列表,早晨推送;**线索必须人工审批才能立项成 goal**(默认防 AI 自作主张花钱)。
- KPI 回流:手动填报 + 自动采集(发布平台数据接口/爬虫)。

### M3 兵营 Barracks
- **Agent 档案**:代号/编号、军衔、绑定模型档、绑定设备、技能清单、战功(任务数/成功率/质量分)、累计成本。
- **军衔 × 模型分层**(本产品的调度灵魂):

| 军衔 | 定位 | 模型档(示例) | 典型任务 |
|---|---|---|---|
| S 将官 | 规划/创意/复杂决策 | Opus / GPT 高档 | 目标拆解、策略、复盘 |
| A 校官 | 任务主力 | Sonnet / GPT 中档 | 选题、写长文、代码 |
| B 尉官 | 常规批量 | Mini / Flash / GLM-Flash | 改写、排版、分类打标 |
| C 士官 | 机械执行 | 最便宜 + 缓存优先 | 全网扫描、格式转换、备份 |
| D 预备役 | 休眠/待淘汰 | 不分配 | — |

  军衔是「任务准入等级」;agent 建档时绑定默认模型档,晋升/降档时改绑。任务按所需军衔路由到对应档模型。
- **生命周期**:`new → active → probation(绩效差观察) → retired(退役,装备回收)`。
- **批量操作**:按模板批量招募(「再来 50 个 B 级内容兵」)、批量装卸技能、批量退役。
- **绩效公式**(Phase 1 初值,做成可配置):
  `score = 0.4×任务成功率 + 0.3×产出质量分(eval/人工抽检) + 0.2×成本效率(同类任务成本分位) + 0.1×出勤率(cron 按时完成)`
  月度结算:C 级降档;连续两月 C 级退役;S 级获得优先派单权。

### M4 作战室 Dispatch
- **小队 Squad**:leader(S/A 级)+ 若干 soldier,绑定一个目标。内置模板:内容小队(1 策划 + 2 写手 + 1 排版 + 1 数据)、情报小队、运维小队。
- **工作流 Workflow**:节点 = 任务类型 + 所需军衔 + 工具/技能;边 = 顺序/并行/依赖;触发器 = cron / webhook / 事件(如「情报站发现高分线索」)。
- **调度器语义**:
  - 分级队列:目标优先级 × 任务军衔。
  - 并发控制:全局最大并发(= 设备容量之和)、单 agent 串行(一次只干一件事)。
  - cron:标准表达式,本地时区;大批量扫描任务随机抖动 0-30 分钟错峰。
  - 重试:指数退避,3 次失败进「伤兵营」人工介入队列。
  - 夜间窗口:重型任务(全网扫描/备份)限定 00:00-08:00。
- **Run(执行留痕)**:run = agent × task 的一次执行,记录输入、输出、token、成本、耗时、质量评分。

### M5 兵工厂 Arsenal

**5.1 API 网关(核心基础设施)**
- 统一入口:OpenAI 兼容 `/v1/chat/completions` + Anthropic 兼容 `/v1/messages`,SSE 流式透传。军团内所有 agent 只认这一条 API。
- **渠道 Channel** = 供应商 + 接入方式 + 账号池,三类:
  - A. 标准 API key(OpenAI / Anthropic / DeepSeek / GLM …)
  - B. CLI 订阅中转(Workbuddy / Trae / Opencode 等订阅额度封装成 API —— Phase 2,需逐家验证 ToS)
  - C. 第三方聚合(Agent37 等)
- 路由:模型档位 → 渠道优先级 → 权重负载均衡;失败自动 failover 下一渠道;熔断(5 分钟错误率 > 50% 摘除,定时探活恢复)。
- 计量:逐请求记录 渠道/agent/任务/目标 四个维度的 token 与成本;**成本价/售价双账本**(为课程与未来售卖预留)。
- 预算护栏三级:agent 日预算 → 目标总预算 → 全局月预算;超限动作:告警 → 限流 → 停机。
- **选型建议(我的决定,可改)**:自研轻网关(Node/TS,约 2k 行),不魔改 one-api 系开源——渠道适配、双账本、军衔路由都是强定制逻辑。

**5.2 设备农场**
- 设备类型:macOS(Mac mini 主力,跑 agent harness 与浏览器)、Android(小米等,App 操作/内容发布)、Linux VPS(爬虫/备份)。
- 接入:设备装常驻 runner → WebSocket 长连接控制面 → 上报在线状态/负载/磁盘。
- 能力标签:内容发布类任务只调度到带 android 标签的设备。
- 健康检查:心跳 30s;失联 5 分钟告警,在跑任务自动迁移到同标签设备。

**5.3 第三方接入**
- **Composio**:统一 OAuth 授权管理 1000+ 应用;其 Provider 可转 OpenAI/Anthropic/LangChain/CrewAI 格式。**我们的接入方式定为 MCP endpoint 为主**,自研工具也统一封装成 MCP server。
- **Perflo marketplace**:爬虫、社媒数据、人物/公司检索、图片视频生成、邮件外呼等按次付费工具,在技能市场作为「付费技能」上架,调用即计费。
- **Agent37**:Phase 2 作为上游渠道接入(具体能力待资料验证,先按通用 API 聚合占位)。

### M6 讲武堂 Academy
- **技能库 Skill** = 提示词 + 工具清单 + 使用说明 + 版本号;来源:自制 / Perflo 市场 / 从成功 run 自动提炼。
- **安装**:单个/批量/按军衔批量绑定到 agent。
- **考核 Eval**:「训练 agent」= 换 prompt / 换技能 / 跑 eval 的循环(不做模型微调)。技能装上后跑基准集(历史成功案例),出分对比,达标入库。
- **Harness 模板**:执行环境封装 = runtime(claude code / opencode CLI headless)+ 系统提示词 + 默认技能包 + 工具权限。新兵入伍套模板,一分钟成军。
- **复盘 Debrief**:每个 goal 结束自动生成:KPI 实际 vs 目标、成本、成败原因、可复用经验。
- **课程工坊**:复盘 + 流程截图 + 模板打包导出 Markdown / PPT 草稿,进课程素材库。

---

## 5. 系统架构

### 5.1 部署形态(Phase 0-2:本地优先、单指挥机)

```
┌──────────────────────────────────────────────────────┐
│ 指挥机 Mac mini #1                                    │
│  ┌─────────── Web 控制台 (Next.js) ───────────┐       │
│  ├─────────── Control Plane API (Hono) ──────┤       │
│  │   ├─ Dispatch 调度器 (cron + 分级队列)     │       │
│  │   ├─ Gateway 统一 API 网关 (路由+计量)     │       │
│  │   └─ SQLite(WAL)+ 本地对象存储(产出)   │       │
│  └────────────────────────────────────────────┘       │
└───────────┬──────────────────┬───────────────────────┘
            │ HTTPS/WSS         │ HTTPS
   ┌────────▼──────┐  ┌────────▼──────┐   ┌─────────────────┐
   │ Runner        │  │ Runner        │   │ 上游            │
   │ Mac mini #2   │  │ Android 池    │   │ OpenAI/Anthropic│
   │ agent harness │  │ (小米等)      │   │ GLM/DeepSeek    │
   │ ×N (mac 标签) │  │ (android 标签)│   │ Agent37/Composio│
   ├───────────────┤  └───────────────┘   │ (MCP endpoint)  │
   │ Runner        │                      └─────────────────┘
   │ Linux VPS     │
   │ 爬虫/备份      │
   └───────────────┘
```

### 5.2 技术栈(建议,评审可改)
- Monorepo:pnpm + TypeScript。
- 控制面 API:Hono(Node),轻、快、TS 一等。
- 前端:Next.js + shadcn/ui + Tailwind。
- 数据库:SQLite(better-sqlite3 + WAL),单指挥机管 500 agent 足够;用 Drizzle ORM 预留迁 Postgres 的路径。
- 队列:Phase 0 用 SQLite 任务表 + 轮询;≥200 agent 后引入 BullMQ + Redis。
- Runner:Node 常驻进程,WebSocket 连控制面;agent 进程 = headless CLI harness(claude code / opencode `exec` 模式),runner 负责起停、注入提示词/技能/环境变量、回传结果与用量。
- 工具协议:MCP 为标准;Composio 走 MCP endpoint。
- 可观测:结构化 JSONL 日志 + 内置 metrics 页(Phase 0 不上 Prometheus)。

### 5.3 核心数据模型(建表级,节选)

```
goals(id, type[money|cognition], title, objective, kpi_json, deadline,
      budget_cap, status[lead|active|done|dropped], squad_id,
      source[manual|radar], created_at)
leads(id, platform, raw_json, scores_json, recommendation,
      status[new|approved|rejected], goal_id, captured_at)
agents(id, codename, rank[S|A|B|C|D], status[new|active|probation|retired],
       model_tier, device_id, harness_template_id, joined_at)
agent_skills(agent_id, skill_id, installed_at)
squads(id, name, goal_id, leader_agent_id, template, created_at)
workflows(id, name, dag_json, trigger[cron|event|webhook], cron_expr, enabled)
tasks(id, goal_id, workflow_id, title, rank_required[S|A|B|C], payload_json,
      status[queued|running|done|failed|infirmary], priority,
      scheduled_at, device_tag, retry_count)
runs(id, task_id, agent_id, device_id, model, channel_id, input_tokens,
     output_tokens, cost, duration_ms, status, output_ref, quality_score,
     started_at)
devices(id, kind[mac|linux|android], hostname, capacity, load, status,
        last_heartbeat, tags_json)
channels(id, name, type[api_key|cli_relay|aggregator], provider, base_url,
         auth_json, models_json, weight, priority, status[active|tripped],
         cost_rate, probe_at)
usage_logs(id, ts, run_id, agent_id, goal_id, channel_id, model,
           input_tokens, output_tokens, cost, billable_cost)
skills(id, name, version, body, tools_json, origin[self|market|mined],
       eval_score, created_at)
harness_templates(id, name, runtime, system_prompt_ref, skills_json,
                  permissions_json)
evals(id, skill_id, agent_id, benchmark_ref, score, ran_at)
debriefs(id, goal_id, kpi_actual_json, total_cost, summary, lessons_json,
         created_at)
course_items(id, debrief_id, title, content_ref, status[draft|ready])
```

### 5.4 端到端旅程(一条任务的完整一生)
夜间:情报站 cron 触发 → 斥候 agent(B 级,mac runner)经爬虫工具抓 100 条热点 → 评分卡打分 → 高分线索入库待审。
早晨:指挥官审线索,一键立项(goal)→ S 级指挥模型拆任务树 → 绑定「内容小队」→ 调度器按军衔路由:选题(A)、写文(B×2)、排版(B)、**发布任务路由到 android runner** → 发布 agent 经 Composio/自动化工具发布(Phase 1 人工一键确认)→ 48h 后数据回流任务定时执行 → 全程 usage_logs 记账 → goal 结束自动复盘 → 优质 prompt 提炼为 skill 入库 → 课程工坊可导出。

---

## 6. 分期路线图(每期带验收标准)

### Phase 0 · MVP「先跑通一个班」(约 2 周)
**范围**:兵营(≤10 agent)、网关(2 个标准 API key 渠道)、调度(cron + 串行队列,无小队 UI)、极简战报、SQLite;**含 Spike S1**(headless harness 用量回传与自建网关指向验证)。
**验收**:3 个 agent 每天 08:00 自动产出 3 篇小红书草稿存本地;战报页能看到篇数与成本;全部模型流量走网关并有逐请求账单;S1 结论落档(决定 G7 计量路径)。

### Phase 1 · 「一个排 + 一条赚钱闭环」(约 4 周)
**范围**:Goal Studio 全量(目标/拆解/情报站-小红书)、小队与工作流、Composio MCP 接入、绩效淘汰 v1(质量分按 D11:LLM 评判为主)、预算护栏、发布半自动化(生成后人工一键确认);**含 Spike S2**(Composio 小红书覆盖度验证)。
**验收**:端到端闭环(热点→内容→发布→数据回流→复盘)连续跑 7 天;月度绩效结算至少淘汰过 1 个 agent;能出每个 goal 的成本账单(影子成本口径,见 D9)。

### Phase 2 · 「一个营」(约 6 周)
**范围**:设备农场(Mac mini × N + Android 池)、CLI 订阅中转渠道、Agent37 接入、BullMQ 扩容、eval 考核闭环、harness 模板库。
**验收**:≥3 台设备混编调度;单日活跃 agent ≥100、峰值并发 ≥30;至少 1 个技能经 eval 验证使产出质量分提升 ≥15%。

### Phase 3 · 「学院」(持续)
**范围**:Perflo 付费工具上架、课程工坊导出、(可选)学员只读门户。
**验收**:第一门课程的完整章节草稿产出。

> 500 agent 是方向不是门槛:按 10 → 100 → 500 递进,Phase 2 结束时用真实任务量与成本决定是否继续扩编(见 R4)。

---

## 7. 风险与对策

| # | 风险 | 对策 |
|---|------|------|
| R1 | 小红书抓取/自动发布违反平台 ToS,账号封禁 | 抓取只用自建账号、低频轮询;Phase 1 发布强制人工确认;Android 设备池与主账号隔离;预留官方开放平台接口 |
| R2 | 成本失控 | 三级预算护栏(告警→限流→熔断)+ 双账本 + 月度硬顶 |
| R3 | CLI 订阅中转随时可能失效 | 渠道熔断 + 探活 + 标准 API 兜底渠道,任何档位至少一条非订阅渠道 |
| R4 | 500 并发是伪需求 | 路线图 10→100→500 递进,每期用真实任务量校准 |
| R5 | 范围蔓延成通用 Agent 框架 | 反目标 A1 锁死:不服务于「赚钱/认知闭环」的模块不进路线图 |
| R6 | 指挥机单点故障 | 每日 SQLite 备份上云;runner 无状态可重连,任务断点续跑 |

---

## 8. 我替你拍板的默认值(评审时可逐条改)

- **D1** 产品代号:军团 Legion(占位,可换)。
- **D2** 技术栈:TS monorepo + Hono + Next.js + SQLite + 自研轻网关。
- **D3** 军衔 5 级(S/A/B/C/D)与模型映射方案。
- **D4** 情报站 Phase 1 只接小红书。
- **D5** 情报站线索必须人工审批才能立项(防 AI 自主花钱)。
- **D6** Phase 1 发布为半自动(人工一键确认),全自动发布放 Phase 2。
- **D7** 单机 SQLite 起步,≥200 agent 再引入 Redis/Postgres。
- **D8** 绩效公式权重(40/30/20/10)为初值,做成可配置。
- **D9** 订阅流量记账口径:按官方牌价折算的**影子成本**入账(账单同时显示订阅摊销口径);绩效公式中的成本效率一律按影子成本计算,避免包月流量边际成本为 0 导致淘汰机制失真。
- **D10** 记忆模型:Phase 0-1 的 agent **无状态**,每次执行上下文 = 任务载荷 + 技能注入 + goal 级共享简报(goal brief,随任务下发);「同一个兵」的连续性靠 prompt 拼装实现。跨任务长期记忆作为后期增强(情境注入层),不改 runner 核心。
- **D11** Phase 1 质量分来源:**LLM 自动评判(LLM-as-judge)为主**,人工抽检仅做校准(每天抽 3-5 条);若抽检样本不足,先按成功率+成本结算,质量分留空。
- **D12** 网络接入:所有 runner **主动外连**(WebSocket dial-out)到指挥机,指挥机不暴露入站端口;跨公网部署时可选 Tailscale 组网。同局域网/异地均适用。

## 9. 开放问题(需你在评审时拍板,按重要性排序)

- **Q1 赚钱闭环「三件套」的落地手段与账号资产**(扩展自原 Q3/Q5,最优先):
  1. **抓取**:小红书热点抓取用自建爬虫(要登录态、顶风控)还是买数据服务(新红/千瓜等)?
  2. **发布**:Phase 1 的「人工一键确认」由你在控制台点掉后,实际发布动作在哪儿执行(手机上人工贴?安卓自动化?)?Composio 对小红书的覆盖度待验证,若不覆盖,Phase 2 安卓自动化走 ADB/UI 自动化还是别的路线?
  3. **回流**:48h 后的阅读/涨粉数据自动采集还是人工填报?Phase 1 验收里「回流」算不算人工?
  4. **账号资产**:现有几个小红书号、敢不敢动主号?这决定设备池隔离设计与 R1 的真实风险等级。
- **Q2 上游模型资产清单**(扩展自原 Q1):手上现有哪些官方标准 API key(哪家、什么档位)?哪些能力只在订阅额度(Workbuddy/Trae/Opencode)里?**若 S 级军衔需要的「高档模型」只存在于订阅里,Phase 0 要么提前做订阅中转适配,要么 S 级任务先降档跑**——这决定 Phase 0 首批渠道清单与工期。
- **Q3 预算硬顶**(原 Q2):首期 3 个月 API + 设备预算的具体数字?设备采购占多少?这决定 500 编制落在 Phase 2 还是 Phase 3,以及月度结算的初始参数。
- **Q4 每日注意力预算**(新):系统跑起来后你每天愿意投入多少分钟(审线索、确认发布、抽检、看战报)?若只有 10-15 分钟,线索推送量、可并行闭环数、抽检比例都要按此压小,UI 按批量审批设计。
- **Q5 第一条赚钱闭环的赛道**(原 Q3):小红书内容引流带货 / 数据服务 / 其他?决定 Phase 1 验收标准如何具体化。
- **Q6 Agent37**(原 Q4):有资料吗?没有则按「通用 API 聚合渠道」占位,Phase 2 再验证。
- **Q7 安卓设备**(原 Q5):现有几台、什么型号?决定 android runner 的开发顺序。
- **Q8 课程化**(原 Q6):课程纯内部沉淀还是计划对外售卖?影响 Phase 3 是否做学员门户与内容脱敏。

## 10. 待验证 Spike(实现计划中安排,不阻塞评审)

- **S1(Phase 0)**:验证 claude code / opencode headless 模式——用量数据(token 数)能否可靠回传、能否把 API 端点指向自建网关。这决定 G7 计量的实现路径;若不可行,计量退到网关侧按请求估算。
- **S2(Phase 1)**:验证 Composio 对小红书的实际覆盖度;不覆盖则发布环节改走安卓自动化或人工半自动兜底(与 Q1 联动)。
