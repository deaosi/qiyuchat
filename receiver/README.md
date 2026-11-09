# 本地会话清洗与展示看板

先把 CSV / XLSX 会话或问答表放进本机 SQLite，完成 **预览 → 确认导入 → 规则清洗 → 筛选分析 → 追溯会话 → 脱敏导出**。
Python 3.10+，纯标准库，无需安装依赖、申请外部账号或下载模型。

在项目目录运行：

```bash
python run.py serve --open
```

默认地址为 **http://127.0.0.1:8848/**。网页中可导入文件，也可点击“先看演示数据”，不会连接七鱼、飞书或模型。

- **本地数据**：默认 `data/qiyu.db`，可用 `DB_PATH` 配置。
- **演示数据**：同目录的 `dashboard_demo.db`，明确标记为模拟数据，与本地数据、旧 `mock.db` 隔离。
- **规则清洗**：抽取问答、过滤寒暄/纯信息、归一化和相似问句聚类；不生成 AI 分类，不判断问题真正是否解决。
- **七鱼接入先预留**：保留现有连接器与 CLI，本轮不要求凭证，不自动取数或推送。原有 AI/飞书能力仍可单独配置使用，与网页本地流程独立。

完整使用说明、支持列名、更新规则和限制见 [本地清洗与看板](docs/本地清洗与看板.md)。

## 当前网页功能

| 视图 | 内容 |
|---|---|
| 会话概览 | 有效问答、问题类别、客户数、未回复数、高频排行、每日趋势、近期问答、清洗状态 |
| 高频问题 / 问题簇 | 相似说法与次数，点击进入相应问答；图表提供读数与数据表 |
| 客户群 / 客户 | 分组统计与下钻，匿名客户按会话区分 |
| 问答明细 | 搜索、回复状态、分页、脱敏原话与回复、右侧完整会话上下文 |
| 数据清洗 | 两步导入、字段识别、错误行提示、重新规则清洗、最近操作记录 |

日期、客户群、客服共用筛选口径；明细导出还会应用搜索、回复状态、客户或问题簇条件。
时间缺失保留未知，不补成今天。原始正文只保存在本地数据库，网页和 CSV 使用规则脱敏；外发前仍需人工检查。

### 离线回归测试

```bash
python -B -m unittest discover -s tests -v
```

83 项 Python 测试覆盖原有 CLI、导入/更新/回滚、脱敏、聚类、筛选、导出、数据隔离与 HTTP 同源保护。测试使用临时数据库，不依赖已有演示数据或外部服务。

界面元素参考 SkillHub 的 Ant Design / ProLayout：蓝色主操作、4px 圆角、白色顶栏与侧栏、紧凑表格、三步导入弹窗和详情抽屉；实现仍为原生 HTML/CSS/JS，不需要安装 React 或 npm 包。

开发时可选运行 8 项前端函数回归（需要 Node.js 20+ 和 Python，无 npm 依赖）：

```bash
node --test tests/frontend.test.cjs
```

该检查使用隔离 Python API 数据和轻量 DOM 替身，覆盖七个视图、HTML 转义、分页参数、导入步骤和图表标记，不代替真实浏览器的交互 / 布局验收。

---

## 原有外部链路（保留，按需配置）

以下介绍七鱼取数、可选 AI 清洗与飞书推送，不是启动本地看板的前置步骤。

```
七鱼接口 / 本地文件 → SQLite 原始数据 → 规则抽取与聚类 → 本地看板
                                            └→ 可选 AI 清洗 / 飞书推送
```

---

## 一、为什么是这个架构

| 决策 | 原因 |
|---|---|
| 两段式取数（既有实现） | 当前连接器分别读取会话元数据和质检正文；官方另提供单会话消息接口，后续接入时再核对权限与覆盖率 |
| 本地 SQLite 做中转 | 七鱼有严格限流（单接口 5 QPS / 单租户 20 QPS）和导出任务互斥，直接透传必然被打回 |
| 规则层放在模型前面 | 单轮会话、无效会话能砍掉 30~50%，这部分 token 完全没必要花 |
| 结果按 `(session_id, prompt_version)` 存 | 换 prompt 可并存对比，出问题能回滚到旧版本 |
| 推送层独立 | 同一份清洗结果可以同时投多维表格、群卡片、文档，互不影响 |

---

## 二、目录结构

```
D:\test3\
├── run.py                     统一 CLI 入口
├── .env.example               配置模板
├── requirements.txt           空的（零依赖，只作说明）
├── src/
│   ├── config.py              配置加载（.env → 不可变 dataclass）
│   ├── log.py                 控制台 + 按天滚动文件日志
│   ├── http.py                urllib 封装：超时/重试/错误归一
│   ├── db.py                  SQLite schema、幂等 upsert、水位线
│   ├── xlsx.py                最小 xlsx/csv 读取器（zipfile + ElementTree）
│   ├── local.py               离线导入、规则清洗、独立演示和脱敏 CSV
│   ├── web/                   本地 HTTP API 与 HTML/CSS/JS 工作台
│   ├── mock.py                内置假数据
│   ├── app.py                 编排层：pull / inspect / extract / report / clean / push / rotate / doctor
│   ├── qiyu/
│   │   ├── sign.py            md5+sha1 签名
│   │   ├── common.py          错误码与中文提示
│   │   ├── session.py         POST /openapi/export/session
│   │   └── inspect.py         POST /openapi/chat/inspect/result/{download,check}
│   ├── extract/               ★ 零模型调用的抽取与聚类
│   │   ├── qa.py              客户问 → 客服答 配对
│   │   ├── normalize.py       去寒暄/语气词、数字归一、噪声与信息过滤
│   │   ├── concepts.py        16 个业务概念词表（可编辑）
│   │   ├── cluster.py         2-gram Jaccard + 概念重合 → 并查集聚类
│   │   └── aggregate.py       客户 / 客户群 / 全局 三层聚合
│   ├── clean/
│   │   ├── rules.py           0 档：脱敏 / 噪声剔除 / 规则评分
│   │   ├── prompts.py         prompt 资产 + 版本号
│   │   ├── llm.py             OpenAI 兼容客户端 + 模型路由 + dry run
│   │   └── pipeline.py        规则 → 分类 → 摘要 → 落库
│   └── feishu/
│       ├── auth.py            tenant_access_token 缓存（提前 20 分钟刷新）
│       ├── bot.py             群自定义机器人卡片（HMAC 签名）
│       ├── bitable.py         多维表格 upsert（以会话ID为唯一键）
│       ├── docx.py            文档创建/覆盖写/授权给人
│       ├── agent.py           ★ Aily 自定义智能体（发起对话 + 轮询，双向）
│       └── check.py           ★ 联通性自检 + 飞书错误码翻译
├── docs/
│   ├── 本地清洗与看板.md       导入模板、统计口径、更新和脱敏边界
│   └── 飞书接入准备清单.md     两条路线、scope 对照表、错误码→怎么修
├── tests/
│   ├── test_smoke.py          27 个用例：签名/脱敏/规则评分/DB/裁剪/闸门/飞书自检/端到端
│   ├── test_extract.py        归一化/概念/聚类/问答配对
│   ├── test_local.py          导入、清洗、状态、回滚、数据隔离、脱敏导出
│   └── test_web.py            HTTP 路由、同源写保护、并发切库和完整导入闭环
├── data/                      运行时生成：qiyu.db
└── logs/                      运行时生成：app.log
```

---

## 三、数据流

### 1. 取数（`pull` / `inspect`）

**会话记录** `POST /openapi/export/session?appKey=&time=&checksum=`

```json
{"start": 1700000000000, "end": 1700432000000, "type": 0}
```

- 跨度上限 **5 天**，代码按窗口自动切片
- `type`：0 会话记录 / 2 留言记录
- 当前 `pull` 连接器解析会话元数据，不解析消息正文。官方文档另有 `/openapi/export/session/one/message`，尚未实现或联调。

**质检导出** 两步异步：

```
POST /openapi/chat/inspect/result/download  →  {"code":200,"message":"<key>"}
POST /openapi/chat/inspect/result/check     →  {"code":200,"message":"https://....xlsx"}
                                            →  {"code":14403,"message":"Wait..."}
```

- 跨度上限 **2 天**
- 现有 `inspect` 连接器从该文件取得「会话内容」列，并非官方唯一的消息读取路径
- **覆盖率是整条链最大的不确定性**：只导出被质检任务扫过的会话

**签名**（官方《通用说明》）

```
md5      = MD5(实际发出的 body 字符串).lower()
checksum = SHA1(appSecret + md5 + time).lower()
time     = UTC 秒，偏差 >5 分钟报 14003
```

> 关键坑：`md5` 必须对**实际发出去的字节**算。`http.py` 里对 `str` 类型的 body 直接原样发送，
> 不做二次序列化，否则字段顺序或空格一变签名就废。

### 2. 抽取与聚类（`extract` / `report`）—— **零模型调用**

这一层回答的是「谁、什么时间、问了什么、原话是什么」，以及「单个客户在纠结什么 / 整个客户群在反馈什么」。
**全程不调用任何大模型 API，成本 0 元。**

```
raw_message ──qa.py──> qa_pair（一问一答一条记录）
                │
                ├─ normalize.py  去寒暄/语气词/标点，数字归一，全角转半角
                │
                └─ cluster.py    字符2-gram Jaccard + 业务概念标签 → 并查集
                                  │
                                  └─> q_cluster（高频问题簇）
```

**三步细节：**

1. **配对**（`src/extract/qa.py`）：连续多条客户消息合并成一问，遇到客服消息闭合；客服没回的问题也记录（`answer` 为空，这类最该看）；地址/单号/纯情绪短句直接丢。
2. **归一**（`src/extract/normalize.py`）：`你好，请问这个多少钱啊？` → `这个多少钱`。数字统一成 `0`（订单号的具体值对「高频问题」没意义）。
3. **聚类**（`src/extract/cluster.py`）：

```
相似度 = 0.5 × 字符2-gram Jaccard  +  0.5 × 业务概念重合度
```

相同归一化问句先合并，之后才对不同问法进行候选剪枝与相似度比较。概念词典补充同义词召回，但并非语义模型：价格、保修等宽泛主题仍可能误合，业务复核需查看簇内原话。质量、使用、参数等泛概念不单独提供合并加分。

**概念词典**（`src/extract/concepts.py`）是可编辑的 16 个业务概念（价格/发货/物流/退货/退款/换货/质量/使用/参数/库存/发票/安装/保修/配件/投诉/退单），
业务方可以直接审词表 —— 这是 embedding 做不到的可控性。

**实测效果**（22 条人工构造的同义句）：

| 输入 | 结果 |
|---|---|
| 这个多少钱 / 多少钱啊 / 价格是多少 / 怎么卖 / 这个怎么卖的 | 合并成 1 簇（7 条） |
| 什么时候发货 / 多久发货 / 发货时间是多久 / 几天能到 | 合并成 1 簇（5 条） |
| 客服态度太差了 / 你们客服态度很差 / 服务态度太差了吧 | 合并成 1 簇（3 条） |
| 滤芯多少钱 ↔ 滤芯多久换一次 | **正确分开**（不同概念） |

**输出三张表：**

| 表 | 内容 |
|---|---|
| `qa_pair` | 每条问答：客户、客户群、客服、时间、原话、回答、所属簇 |
| `q_cluster` | 每个簇：代表原话、出现次数、涉及客户数、涉及群数、首末时间 |
| `report` | 三层聚合：全局 Top N / 按客户群 Top N / 单个客户 Top 5 |

**什么时候才需要升级到 embedding：**
同义不同形严重到词表兜不住（比如「物流太磨叽」vs「发货慢」），或数据量 > 5 万条字符匹配召回开始掉。
那时候用本地 `bge-small-zh` + 余弦，仍然免费，只是要装 100MB 模型。

### 3. 清洗（`clean`）

四档路由，按成本从低到高：

| 档 | 做什么 | 模型 |
|---|---|---|
| 0 | 噪声剔除、PII 脱敏、规则评分 | 纯 Python，免费 |
| 1 | 分类（6 大类 / 29 小类）、情绪、是否解决、风险 | `LLM_MODEL_FAST`，10 条一批 |
| 2 | 诉求、处理结果、产品、是否跟进、一句话 | `LLM_MODEL_MAIN`，并发 |
| 3 | 难例升级（`risk=high` 或 `confidence<0.6`） | `LLM_MODEL_HARD`，留空则复用 2 档 |

**三条硬规矩**：

1. **不用 LLM 脱敏** —— 正则本地做，模型不该看到原始手机号
2. **不用 LLM 打绝对分** —— `quality_score` 只用七鱼的客观字段算（首次响应 35 / 平均响应 25 / 轮数 15 / 满意度 25）
3. **不一次一条** —— 分类走批处理，摘要走并发，省一半时间

**没有正文的会话不硬编摘要**，只落规则分并标记 `category=无正文`，避免模型瞎编。

### 4. 推送（`push`）

| 出口 | 需要的权限 | 适合 |
|---|---|---|
| 群自定义机器人 | 无（只要 webhook） | 日报、异常告警 |
| 多维表格 | `bitable:app` | 台账、筛选、做看板 |
| 文档 | `docx:document`、`docx:document:create` | 周报、汇报 |

多维表格用**会话ID做唯一键**做 upsert（先分页拉已有 record_id 建索引，再分 create/update 两批），
不做这一步重跑就会写重复。

### 5. 裁剪（`rotate`）—— 架构里的最后一环，不是可选优化

原始明细会无限增长，所以每一轮最后都要裁一次。但**裁剪是唯一会丢数据的步骤**，
所以它排在最后，而且带两道闸门：

```
pull ─→ inspect ─→ extract ─→ report ─→ clean ─→ push ─→ rotate
                                                          │
                        ┌─────────────────────────────────┘
                        ├─ 本轮一条会话都没拉到？→ 跳过（凭证失效时别删）
                        ├─ 抽取 0 条但还有没抽的会话？→ 跳过（extract 出问题先别删）
                        └─ 正常 → 删窗口外的 raw_session + raw_message
```

顺序为什么不能调：`rotate` 必须看到本轮的 `extract` 结果，否则会把还没抽过问答的会话当垃圾删掉。

删的边界：`raw_session.end_ts` 早于「今天 00:00 − 60 天」。
保留策略：`rotate` 不按日期删除 `qa_pair` / `q_cluster`，原始正文裁剪后仍保留问答资产。此策略不表示问答内容永远不变：按会话完整快照更新或重新规则清洗时，会移除已不成立的旧问答尾部，并重算问题簇。

---

## 四、怎么跑

### 先跑假数据（不需要任何凭证）

```bash
cd D:\test3
python run.py mock
```

会打印完整链路结果：

```json
{
  "pull":   {"sessions": 24},
  "clean":  {"candidates": 24, "noise": 7, "classified": 17, "escalated": 7},
  "push":   {"bitable": {"created": 24, "updated": 0}, "bot": true, "doc": "dry-run-doc-id"},
  "stats":  {"raw_session": 24, "raw_message": 80, "clean_result": 24}
}
```

### 自检

```bash
python run.py doctor
```

输出每项依赖是否就绪、当前是真实接口还是 dry run、数据库路径和行数。

### 接真实环境

```bash
cp .env.example .env
# 填 QIYU_APP_KEY / QIYU_APP_SECRET / LLM_API_KEY / FEISHU_*
# 把 LLM_DRY_RUN、FEISHU_DRY_RUN 改成 0

python run.py feishu-check       # 飞书通不通，先验这一步（见 docs/飞书接入准备清单.md）
python run.py demo --to bot         # ★ 七鱼没接时：假数据 → 真实抽取 → 真发飞书
python run.py push --source pairs   # 推问答明细（默认，零 API）
python run.py push --source clean   # 推 LLM 清洗结果（需 LLM_API_KEY）
python run.py pull --days 7      # 先拉一周会话记录
python run.py inspect --days 2   # 拉质检正文
python run.py extract            # 抽问答 + 聚类（免费，先跑这个看效果）
python run.py report             # 三层聚合，推飞书文档
python run.py clean --limit 200  # 需要 AI 摘要时再跑
python run.py push --to all
```

分步跑，每步都能单独看日志、单独重跑。

> **只要「谁问了什么、高频问题是什么」，跑到 `report` 就够了，一行 LLM 都不用调。**

飞书侧要准备什么，见 [docs/飞书接入准备清单.md](docs/飞书接入准备清单.md)：两条路线、权限 scope 对照表、错误码 → 该去点哪里。

### 单测

```bash
python -m tests.test_smoke    # 27 个：签名、脱敏、规则评分、DB 幂等、裁剪边界、裁剪闸门、端到端 mock
python -m tests.test_extract  # 22 个：归一化、概念命中、聚类合并/不误合、问答配对
```

83 项用例（含本地文件与 HTTP 回归），全部零外部依赖；完整运行命令见开头。

### 定时任务

Linux cron：

```cron
0 2 * * * cd /opt/qiyu && python3 run.py all --limit 500 >> logs/cron.log 2>&1
```

Windows 任务计划：程序填 `python`，参数填 `run.py all --limit 500`，起始位置填 `D:\test3`。

---

### 数据保留（滑动窗口）

`RETENTION_DAYS=60`（默认）。`run.py all` 最后一步会自动裁剪：

```bash
python run.py rotate --window 60 --dry-run   # 先看会删多少
python run.py rotate --window 60             # 真删
python run.py rotate --window 60 --vacuum    # 顺带回收磁盘（月底跑一次）
```

| 删什么 | 留什么 |
|---|---|
| `raw_message` / `raw_session` 超过 60 个自然日的 | `qa_pair` / `q_cluster` 不按日期裁剪；快照更新和重新聚类仍会修正它们 |

对齐到**本地自然日 00:00**，所以是严格的「进一天、出一天」；不是「当前时刻减 60×24 小时」
（那样窗口会在小时级滑动，同一天的数据可能被劈成两半）。

**为什么是 60 天**：保证任何时候都能完整覆盖**上一个自然月**。45 天在每月 15 号之后，
上月月初的数据就已经出窗了，月度复盘做不了同环比。

两个保护：

- **宽限期 7 天**：过期但还没抽过问答的会话多留一周，等 `extract` 补上。一次抽取失败不会造成永久丢数据。
- **`orphan_session` 计数**：窗口外真有没抽过就被删的会话，日志会 warning —— 说明 extract 漏跑了。

⚠️ 裁剪依赖会话的 `end_ts` 和消息的 `ts`。**服务器时区必须是 `Asia/Shanghai`**，
否则边界会整体偏移 8 小时：

```bash
timedatectl set-timezone Asia/Shanghai
```

### 实时接收（`receiver`）—— 七鱼推事件过来

七鱼后台 `系统管理 → 扩展与集成 → 开发者ID → 消息与事件接收url` 填
`https://<你的域名>/qiyu/event`，本项目起一个常驻接收端把事件收下来：

```bash
python run.py receiver                    # 默认 127.0.0.1:8860/qiyu/event
python run.py receiver --port 8860 --no-verify   # 本机联调临时关验签
python tools/mock_qiyu_push.py            # 造一段假会话，验证链路通不通
```

| 事件 | 处理 |
|---|---|
| `SESSION_START` | 记 `uid → sessionId` 映射，会话落 `raw_session` |
| `MSG` | 客服侧消息正文追加进 `raw_message`（`msgId` 去重）|
| `SESSION_END` | 关闭会话，记 `closeReason` |
| `EVA_INVITATION` / `USER_JOIN_QUEUE` / `QUEUE_TIMEOUT` | 只记账到 `push_event` |

**三个必须知道的硬约束**（官方原文）：

1. **10 秒内必须返回空字符串**。超时或返回非空 → 七鱼判定失败并重发。本项目所有成功响应
   都是 `200` + 空 body；验签失败回 `401`，数据库异常回 `500`（让七鱼重发，不丢消息）。
2. **只对 OpenApi 渠道（消息接口）生效**。网页 / 微信 / 飞书 / 企微等七鱼原生渠道的会话
   **不会推**。所以这条通道只能配合「自建渠道」用。
3. **只推客服侧消息**。客户说的话是你自己转发进七鱼的，所以必须在转发的同时镜像一份到
   `POST /qiyu/customer`（header `X-Receiver-Token: <AppSecret>`），否则永远凑不成问答对。

```bash
# 客户侧镜像（你的转发器里调这一句）
curl -X POST http://127.0.0.1:8860/qiyu/customer   -H 'Content-Type: application/json' -H "X-Receiver-Token: $QIYU_APP_SECRET"   -d '{"uid":"visitor-001","content":"滤芯多久换一次","msgId":"c-1"}'
```

推送数据的闭环：

```
七鱼 MSG ──┐
           ├─→ raw_message ─→ extract ─→ qa_pair ─→ report / 看板
客户镜像 ──┘
```

> 注意 `MSG` 只带 `uid` 不带 `sessionId`。如果消息早于 `SESSION_START` 到达，会先临时挂到
> `live-<uid>` 会话并打 warning，收到会话开始通知后自动改绑 —— 不会丢消息。

部署：`deploy/qiyu-receiver.service`（systemd，`Restart=always`）+ `deploy/Caddyfile.receiver`
（反代 + 自动 HTTPS，`/qiyu/customer` 默认只允许内网来源）。

### 看板鉴权（单管理员密钥）

看板一旦挂到公网，就必须过一道门。这里用**单管理员密钥**，不设用户名、不存密码库：

```bash
# 1) 生成一把密钥（32 位 URL 安全随机串）
python -c "import secrets; print(secrets.token_urlsafe(24))"

# 2) 写进 .env
echo "ADMIN_KEY=<刚生成的串>" >> .env

# 3) 重启服务
sudo systemctl restart qiyu-console
```

填了 `ADMIN_KEY` 之后，**整站**（页面、静态资源、`/api/*`）都必须先过密钥；留空 = 不做应用层鉴权，
只适合本机开发。

三种进站方式，效果等价：

| 方式 | 用法 | 适合 |
|---|---|---|
| 地址栏带一次 | `https://<域名>/?key=<ADMIN_KEY>` → 302 换成 Cookie | 首次进站，最省事 |
| 登录页 | 打开任意页面 → 输入密钥 → POST `/login` | 日常使用 |
| 请求头 | `X-Admin-Key: <ADMIN_KEY>` | 脚本 / curl / 自动化 |

实现要点：

- Cookie 名 `qy_admin`，`HttpOnly` + `SameSite=Lax` + `Max-Age=30 天`；走 HTTPS 时自动加 `Secure`
  （依据反代传来的 `X-Forwarded-Proto`）。
- 用 `?key=` 进站会立刻 302 把密钥从地址栏换掉，避免留在浏览器历史和反代访问日志里。
- 密钥比对用 `hmac.compare_digest`，不做明文 `==`。
- 同一 IP 10 分钟内连续失败 10 次 → `429`；但**正确密钥永远放行**，不会把管理员自己锁在门外。
- 退出登录：`GET /logout` 清 Cookie 后跳回登录页。
- 鉴权在应用层做，Caddy 只做反代 —— 所以 `deploy/Caddyfile.console` 里没有 `basic_auth`。
- 写保护照旧：`POST /api/*` 仍需同源 `Origin` + `X-Local-Request: 1`，鉴权通过不等于可以跨站写。

---

## 五、幂等与容错

| 机制 | 实现 |
|---|---|
| 会话去重 | `raw_session.session_id` 主键 + `ON CONFLICT DO UPDATE` |
| 消息去重 | `raw_message(session_id, seq)` 复合主键 |
| 清洗去重 | `clean_result(session_id, prompt_version)` 复合主键 |
| 增量 | `watermark` 表存 `session_last_end_ms`，每拉完一个窗口推进一次 |
| HTTP 重试 | 指数退避，4xx 除 429 外不重试（避免把鉴权错误刷成频控） |
| 模型失败 | 单条失败不中断整批，落空结果继续 |
| 推送失败 | 飞书接口 `code != 0` 记日志并抛错，由上层决定是否重跑 |
| 裁剪安全 | 宽限期 7 天 + `orphan_session` 告警；`rotate` 排在 `extract` 之后，先抽后删 |

---

## 六、成本对照：哪些环节要花钱

| 环节 | 命令 | 要不要调模型 | 成本 |
|---|---|---|---|
| 取数（元数据） | `pull` | 否 | 0 |
| 取数（正文） | `inspect` | 否 | 0 |
| **抽问答 + 高频聚类** | **`extract`** | **否** | **0** |
| **三层聚合报告** | **`report`** | **否** | **0** |
| AI 分类/摘要 | `clean` | 是 | 按量 |
| 推送飞书 | `push` | 否 | 0 |

**结论：用户这次要的「记录 + 高频问题」，整条链路 0 元。**

真要用模型时，按 1000 条会话（约 300 万 token）估：

| 方案 | 价格量级 | 说明 |
|---|---|---|
| 本地 Ollama（qwen2.5:3b / 7b） | 0 元 | 一次性下模型，吃自己 CPU/GPU；质量够分类和抽取 |
| 本地 embedding（bge-small-zh） | 0 元 | 100MB 模型，纯 CPU，只做相似度 |
| 智谱 GLM-4-Flash | 免费额度 | 分类/抽取够用，长文本稍弱 |
| 通义 Qwen-Turbo / 阿里百炼 | 有免费 token 额度 | 中文客服场景表现稳 |
| DeepSeek-chat | 十几元 | 便宜且质量高，夜间有折扣 |
| 豆包 Lite / 火山 | 极低价 | 批量分类性价比高 |

价格随时在变，以各家官网为准；这里只给量级。

**降本三招**（已在代码里）：规则层先砍 30~50% 噪声；分类 10 条一批；只有 `risk=high` 或 `confidence<0.6` 才升级到贵模型。

## 七、已知风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 质检覆盖率不足 | 大部分会话没有正文，只能出元数据 | 先跑 `inspect` 看覆盖率；不足就先只做分类不做摘要 |
| 导出任务互斥 | 同一企业同一时间只能 1 个批量导出 | 串行 + 单实例锁（部署时用 systemd 或 flock） |
| `tenant_access_token` 2 小时过期 | 长任务中途 401 | 已做提前 20 分钟自动刷新 |
| 数据出境/合规 | 会话里有手机号、地址 | 进模型前强制正则脱敏；敏感租户建议内网 vLLM |
| 分类体系漂移 | 标签越用越乱 | 固定 `prompts.CATEGORIES`，改体系必须 bump `PROMPT_VERSION` |

---

## 八、下一步可以加什么

1. **gold set 回归**：挑 100 条人工标好，每次改 prompt 自动跑一遍算准确率
2. **外部看板**：本地工作台已实现；后续可按需补飞书多维表格透视或仪表盘
3. **告警**：`risk=high` 的会话实时推群卡片，不等日报
4. **工单联动**：清洗结果里的 `worksheetIds` 可以反查工单，跟 `qiyu-ticket-bridge` 打通
5. **多租户**：现在是一租户一套 `.env`，要做 SaaS 需要把配置挪进 DB
