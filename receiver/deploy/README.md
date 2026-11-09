# 服务器部署

目标机器：Linux（Ubuntu 20.04+/Debian 11+/CentOS 8+/Alma/Rocky 均可），x86_64 或 arm64。
不需要 GPU，不需要 Docker，不需要联网装依赖。

---

## 一、三档方案，先选档再部署

| 档 | 装什么 | 内存 | 磁盘 | 适用 |
|---|---|---|---|---|
| **A 规则版（推荐起步）** | 什么都不装，纯标准库 | < 250 MB | < 200 MB | 每天几千～2 万条问答 |
| **B 语义版** | `pip install text2vec scikit-learn` | ~900 MB | ~1.5 GB | 同义句召回不够，或 > 5 万条 |
| C 主题报告版 | `pip install bertopic`（含 torch） | ~2.5 GB | ~4 GB | 要给老板出主题聚类报告 |

**先上 A。** 它不是"简化版"，是当前数据量下的正确选择：实测 1 万条问答聚类 11 秒、峰值 217 MB。
B 和 C 只在 A 的召回率真的不够时才加，别提前装。

---

## 二、资源实测（Windows 本机，Python 3.14，纯规则版）

| 问答条数 | 聚类耗时 | 峰值内存 |
|---|---|---|
| 2,000 | 0.44 s | — |
| 5,000 | 2.89 s | — |
| 10,000 | 10.89 s | 217 MB |

超线性但不是平方爆炸——因为候选对靠倒排索引生成，不是两两比较。
按每天 1 万条、每晚跑一次算，**CPU 占用不到 1 分钟**，1 核 1G 的云主机够用。

要压测自己的量级：

```bash
python3 - <<'PY'
import random, time, sys
sys.path.insert(0, ".")
from src.extract.normalize import normalize
from src.extract.cluster import cluster_questions
# 换成你自己导出的问题列表
qs = [f"这个商品{i%500}多少钱" for i in range(20000)]
items = [(i, normalize(q)) for i, q in enumerate(qs)]
t = time.time(); cl = cluster_questions(items); print(f"{len(qs)} 条 {time.time()-t:.1f}s {len(cl)} 簇")
PY
```

---

## 三、部署步骤

```bash
# 1. 代码拷到服务器（任意方式：git / scp / rsync）
scp -r D:/test3 user@server:/tmp/qiyu-analysis

# 2. 自检（不联网、不写文件，先看机器够不够）
cd /tmp/qiyu-analysis && python3 deploy/preflight.py

# 3. 一键装（建用户、拷到 /opt、装 systemd）
sudo bash deploy/install.sh

# 4. 填凭证
sudo -u qiyu nano /opt/qiyu-analysis/.env

# 5. 先手工跑通
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py doctor'
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py pull --days 7'
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py extract'

# 6. 开定时（每天 06:30）
sudo systemctl enable --now qiyu-analysis.timer
systemctl list-timers qiyu-analysis.timer
```

不用 systemd 的话，crontab 等价写法：

```cron
30 6 * * * cd /opt/qiyu-analysis && /usr/bin/python3 run.py all --limit 500 >> logs/cron.log 2>&1
```

---

## 四、必须确认的三件事

### 1) 服务器 IP 要进七鱼白名单
主动调七鱼接口必须加白名单，否则报 `14008 请求来源 IP 不在被允许的范围内`。
位置：七鱼后台 → 系统管理 → 扩展与集成 → 开发者ID → IP 白名单。
**云服务器用弹性公网 IP 的，重启换 IP 后会失效**——要么绑固定 EIP，要么把网段加进去。

**注意 IPv6 陷阱**：服务器如果同时有公网 IPv6，glibc 会优先选 AAAA 记录，
请求就从 IPv6 出去，而七鱼白名单只认 IPv4，怎么加都不对。
程序默认 `IPV4_ONLY=1`，强制只解析 IPv4。确认方法：

```bash
python3 -c "import socket;print({i[4][0] for i in socket.getaddrinfo('qiyukf.com',443)})"
# 只应看到 IPv4；出现 240e: / 2402: 开头就是没生效
```

要关闭（比如上游只支持 IPv6）：`.env` 里设 `IPV4_ONLY=0`。

### 2) 时间必须同步
七鱼签名 `checksum` 有效期 5 分钟，时间偏差 > 5 分钟直接报 `14003`。

```bash
timedatectl status          # 看 System clock synchronized: yes
sudo timedatectl set-ntp true
```

### 3) 内网服务器要离线装
A 档零依赖，直接把代码拷过去就能跑。只有 B/C 档才需要离线包：

```bash
# 有网机器上（架构、Python 版本要和服务器一致）
pip download text2vec scikit-learn -d wheels/ --only-binary=:all:

# 服务器上
pip install --no-index --find-links wheels/ text2vec scikit-learn
```

模型文件（BGE）也要提前下好放本地，然后设 `TEXT2VEC_MODEL=/opt/models/bge-small-zh-v1.5`。

---

## 五、Docker（可选，不推荐做首选）

A 档没有依赖，Docker 只会增加运维面。真要容器化：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN useradd -m qiyu && chown -R qiyu /app
USER qiyu
CMD ["python3", "run.py", "all", "--limit", "500"]
```

```yaml
services:
  job:
    build: .
    env_file: .env
    volumes: ["./data:/app/data", "./logs:/app/logs"]
    mem_limit: 512m
    restart: "no"
```

配 `docker compose run --rm job` 由宿主机 cron 调，别在容器里跑 cron。

---

## 六、日志与排错

```bash
journalctl -u qiyu-analysis -n 100 --no-pager   # 最近一次
journalctl -u qiyu-analysis -f                  # 跟踪
journalctl -u qiyu-analysis --since today       # 今天
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `14008` | IP 不在白名单 | 后台加 IP |
| `14003` | 服务器时间偏了 | 开 NTP |
| `14009` | 超频（单接口 5 QPS / 租户 20 QPS） | 降低并发，加大间隔 |
| `14403` | 质检导出任务没跑完 | 正常，等下次轮询 |
| `Permission denied: data/` | 目录属主不对 | `chown -R qiyu:qiyu /opt/qiyu-analysis` |
| 内存被 OOM kill | `MemoryMax` 设太小 | 调到 512M 以上 |

数据库备份（就一个文件）：

```bash
sqlite3 /opt/qiyu-analysis/data/qiyu.db ".backup '/backup/qiyu-$(date +%F).db'"
```

---

## 七、存储规划：要不要上 MySQL

**不需要。** 实测数据（1,000,000 问答对 + 2,000,000 消息，含全部索引）：

| 指标 | 实测值 |
|---|---|
| 库文件大小 | 755 MB / 300 万行 = **264 字节/行** |
| 写入吞吐 | 问答对 68,000 行/秒，消息 257,000 行/秒 |
| 单客户全部问答 | 5.4 ms |
| 最近 1 天问答（5000 行） | 11 ms |
| 全量 GROUP BY Top20（最重） | 95 ms |
| 单会话取消息 | 0.1 ms |

按峰值 1600 客户/天推算（1 会话/客户、10 条消息/会话、2 条问答/会话）：

| 时间 | 行数 | 体积 |
|---|---|---|
| 每天新增 | ~20,800 行 | ~5.5 MB |
| 一年（不清理） | ~760 万行 | ~2.0 GB |
| 一年（60 天滑动窗口） | ~300 万行 | ~0.72 GB，之后不再涨 |
| 三年（4%/月增长 + 60 天窗口） | — | ~1.2 GB |

60 天窗口下：消息 96 万行 ≈ 253 MB、会话 9.6 万行 ≈ 38 MB，合计 ~290 MB，恒定不涨。
问答对永久保留（1 年 117 万行 ≈ 310 MB，3 年 ~900 MB）—— 它才是长期增长的那部分，但它同时是最有价值的分析资产。

**SQLite 的上限是 281 TB。** 你的场景连它的千分之一都用不到。

### 什么时候才真的该换 MySQL

四条，命中任一才考虑：

1. 多台服务器**同时写**同一个库（SQLite 只有单写者，虽然有 WAL 也不适合）
2. 需要**远程连库**（BI 工具、DBeaver 直连）
3. 需要行级锁的**高并发事务**（你这里是单进程批处理，用不上）
4. 公司 IT 有规定，数据必须进集中式数据库

只要没有这四条，MySQL 带来的全是成本：多一个要打补丁的服务、多一套账号权限、多一份备份策略、多 400 MB～1 GB 常驻内存。

### 现在就该做的三件事

**1) 开滑动窗口裁剪**（已实现 `run.py rotate`，`all` 会自动带上）

```bash
# 留最近 60 个自然日，删掉更早的原始明细；问答对永久保留
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py rotate --window 60'

# 先演练，不删
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py rotate --window 60 --dry-run'

# 月底回收一次磁盘
sudo -u qiyu sh -c 'cd /opt/qiyu-analysis && python3 run.py rotate --window 60 --vacuum'
```

`RETENTION_DAYS=45` 写进 `.env` 后，每天的 `run.py all` 会在最后自动裁一次，不需要额外加 cron。

**为什么是 rotate 不是 prune**：prune 按「当前时刻减 N×24 小时」，窗口在小时级滑动，
同一天的数据可能被劈成两半；rotate 对齐到**本地自然日 00:00**，是严格的「进一天、出一天」。

**两个保护**：
- 宽限期 7 天——过期但还没抽过问答的会话会多留一周等 extract 补上，不会因为一次抽取失败就永久丢数据；
- `orphan_session` 计数——如果窗口外真有没抽过就被删的会话，日志会 warning，说明 extract 漏跑了。

实测：200 万条消息库里删掉 100 万条耗时 2.9 秒，VACUUM 1.7 秒，481 MB → 233 MB。

**2) 别给表加多余索引**
每加一个索引，大约多 20～30 字节/行，还要拖慢写入。现在的索引是按查询路径配的：
`raw_session(start_ts)`、`raw_message(ts)`、`qa_pair(customer_id, asked_at)`、`qa_pair(q_norm)`、`qa_pair(asked_at)`。
不要为了"可能有用"再加。

顺带说明：`raw_session.payload` 列现在**不写入**（留作排查扩展位，一直是 NULL），不占体积。

**3) 备份就是拷文件**

```bash
python3 run.py backup --out /var/backups/qiyu-analysis --keep 30
```

走的是 Python 的 `sqlite3` 在线备份 API，**不需要 `sqlite3` 命令行工具**（服务器上经常没装）。
`--keep 30` 自动轮转，只留最近 30 个。`cp` 别用——可能拷到写了一半的 WAL。

### 真要迁 MySQL 时怎么迁

现在写的是标准 SQL（`ON CONFLICT ... DO UPDATE` 是标准 upsert），字段全是整数时间戳，没有用 SQLite 特有的类型。迁移只要：

```bash
sqlite3 data/qiyu.db .dump | mysql -u root -p qiyu
```

但先问自己：上面四条命中了吗？没命中就别迁。

