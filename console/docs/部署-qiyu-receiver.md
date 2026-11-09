# 七鱼平台服务器部署记录（receiver + console）

> 部署时间：2026-11-07
> 目标机：`159.75.3.3`（Ubuntu 26.04 LTS / 4 核 / 7.5 GB）
> 对外域名：`https://qiyu.fanwulink.cn`
> 结论：**两个独立项目已上线，均通过外网端到端验证**

## 两个项目的关系

| | `qiyu-receiver` | `qiyu-console` |
|---|---|---|
| 目录 | `/opt/qiyu-receiver` | `/opt/qiyu-console` |
| 职责 | 收七鱼事件、写库 | 分析看板、读库 |
| 端口 | `127.0.0.1:8860` | `127.0.0.1:8848` |
| 服务 | `qiyu-receiver.service` | `qiyu-console.service` |
| 对外路径 | `/qiyu/event`、`/qiyu/customer`、`/health` | `/`（带基础认证） |
| 数据库 | 共用 `/var/lib/qiyu-receiver/qiyu.db` | 同左 |

两个目录各有一份代码副本、各自的 `.env`、各自的 systemd 单元。
**没有覆盖任何已有项目**（`/opt/skilldesk-platform` 等原样未动）。

---

## 一、部署结果一览

| 项 | 值 |
|---|---|
| 项目目录 | `/opt/qiyu-receiver`（744 KB，独立项目，不碰 `skilldesk-platform`） |
| 数据目录 | `/var/lib/qiyu-receiver/qiyu.db` |
| 日志目录 | `/var/log/qiyu-receiver` |
| 运行用户 | `qiyu`（系统用户，`/usr/sbin/nologin`，无登录权限） |
| 监听地址 | `127.0.0.1:8860`（**只监听回环**，外网一律经 Caddy 进来） |
| 服务单元 | `qiyu-receiver.service`（`Restart=always`，`MemoryMax=512M`） |
| 定时任务 | `qiyu-extract.timer`（每天 06:30 跑 `extract` + `rotate --vacuum`） |
| 反向代理 | 追加到 `/etc/caddy/Caddyfile` 的 `qiyu.fanwulink.cn {}` 段 |
| 证书 | Let's Encrypt，`CN=qiyu.fanwulink.cn`，有效期至 2027-02-05，自动续期 |

### 分析看板（qiyu-console）

| 项 | 值 |
|---|---|
| 项目目录 | `/opt/qiyu-console`（756 KB） |
| 数据目录 | 共用 `/var/lib/qiyu-receiver/qiyu.db` |
| 日志目录 | `/var/log/qiyu-console` |
| 监听地址 | `127.0.0.1:8848` |
| 访问地址 | `https://qiyu.fanwulink.cn/` |
| 账号 | `admin` |
| 密码 | 见下方「看板登录」一节（部署时生成，不在文档里写死） |

看板挂在站点根路径，receiver 的三个路径用 `handle` 优先匹配，互不干扰。

### 对外暴露的三个路径

| 路径 | 用途 | 来源限制 |
|---|---|---|
| `POST /qiyu/event` | 七鱼推送消息与事件 | 验签（AppSecret） |
| `POST /qiyu/customer` | 我们自己的转发器补客户侧消息 | **仅本机/内网**，外网 403 |
| `GET /health` | 存活检查 | 无 |

---

## 二、七鱼后台要填的唯一一个值

```
https://qiyu.fanwulink.cn/qiyu/event
```

填在：`管理后台 → 系统管理 → 扩展与集成 → 开发者ID → 消息与事件接收url`。

> ⚠️ 三条硬限制（官方原文，必须确认）：
> 1. 这个通道**只在 OpenApi 渠道下的会话生效**——网页、微信、飞书、企微原生渠道不会推。
> 2. 只推**客服侧**消息，客户说的话要靠 `/qiyu/customer` 镜像补进来。
> 3. 收到事件后必须 **10 秒内返回空字符串**，否则七鱼会重发。本服务一律 `200` + 空 body。

---

## 三、实测记录（全部真机跑过）

### 1. 外网推送（从开发机 → 公网 → Caddy → 服务 → SQLite）

```
[OK  ] SESSION_START  uid=mock-uid-1   75.5ms  status=200
[OK  ] MSG            uid=mock-uid-1   46.3ms  status=200
[OK  ] SESSION_END    uid=mock-uid-1   61.8ms  status=200
```

落库：`raw_session=2`、`raw_message=11`、`push_event=10`、`uid_session` 绑定 2 条。

### 2. 客户侧镜像（内网放行 / 外网拒绝，两者都验证了）

```
本机 loopback  → customer 1..4  → 200 200 200 200
外网直接打      → customer      → 403 forbidden
```

### 3. 抽取与聚类

```
python run.py extract  → 抽取出 1 条问答对（覆盖 2 个会话），聚类 1 个问题簇
python run.py stats    → raw_session 2 / raw_message 11 / qa_pair 1 / q_cluster 1
```

### 4. 证书与 TLS

```
health=200
subject=CN=qiyu.fanwulink.cn
notBefore=Nov  7 08:39:22 2026 GMT
notAfter=Feb  5 08:39:21 2027 GMT
issuer=C=US, O=Let's Encrypt, CN=YE2
```

---

## 四、部署过程中踩到并修掉的坑

### 坑 1：`.env` 在服务器上变成了 CRLF
Windows 上 `pathlib.write_text()` 会把 `\n` 翻译成 `\r\n`。上传后 29 行全部带 `\r`，
`RECEIVER_VERIFY=1\r` 这类值解析会出问题。
**修法**：`sed -i 's/\r$//' .env`，然后 `systemctl restart qiyu-receiver`。
以后凡是往 Linux 传 `.env` / `.sh`，先过一遍这个。

### 坑 2：`/opt/qiyu-receiver` 属主是 `qiyu`，`ubuntu` 解包会 Permission denied
**修法**：解包那一步就要 `sudo tar xzf -`，解完再 `chown -R qiyu:qiyu`。

### 坑 3：证书签发有几十秒窗口期
Caddy reload 后立刻 `curl https://` 会拿到 `000`。日志里能看到 `tls-alpn-01` 挑战在跑，
大约 10 秒后才 `authorization finalized`。**等 10 秒再验**，不是配置错了。

### 坑 4：客户侧镜像从外网打不通是设计如此
`/qiyu/customer` 挂了 `@notLocal not remote_ip 127.0.0.1 10.0.0.0/8 ...`，
外网直接 403。要测这条得在服务器上 `curl 127.0.0.1:8860`。

### 坑 5（严重）：rotate 会把「进行中」的会话全删掉
`_on_session_start` 写的是 `endTime: 0`，`_on_session_end` 只更新了 `uid_session.ended_at`，
**没写回 `raw_session.end_ts`**。而 `rotate` 直接拿 `end_ts < cutoff` 比 ——
`0 < cutoff` 恒真，于是所有还没结束的会话都被判成「最老的数据」删掉，
连带消息变成孤儿。

修法两处：
1. `db.close_uid_session()` 同时 `UPDATE raw_session SET end_ts=?`；
2. `db.rotate()` 的有效时间改成
   `COALESCE(NULLIF(s.end_ts,0), NULLIF(s.start_ts,0), 0)`，
   并且要求 `> 0` 才参与裁剪 —— 两个时间戳都是 0 的来路不明会话一律不删。

新增两个回归测试：`test_rotate_keeps_open_session`、`test_close_uid_session_writes_end_ts`。
修完实测：`run.py rotate --dry-run` → 删 0 条，进行中的会话保住了。

### 坑 6：看板反代后 403「请通过本机服务地址访问」
`src/web/server.py` 的 `_host_ok()` 只认 `localhost/127.0.0.1/::1` 且端口必须等于绑定端口，
过了 Caddy 之后 Host 变成 `qiyu.fanwulink.cn`、端口是 443，直接 403。

修法：新增两个环境变量，默认空（行为不变，仍然只认本机）：
```
WEB_ALLOWED_HOSTS=qiyu.fanwulink.cn
WEB_ALLOWED_ORIGINS=https://qiyu.fanwulink.cn
```
`_host_ok()` 命中放行名单时只比 hostname、不比端口；
`_write_ok()` 的 Origin 比对同时接受 `http://` 和 `https://` 两种 scheme。

实测三种情况：带正确 Origin + `X-Local-Request: 1` → 通过（404 路由不存在）；
缺 header → 403；伪造 Origin → 403。


---

## 五、看板登录

```
地址   https://qiyu.fanwulink.cn/
账号   admin
密码   EFvsUVJIbMFeTKN6
```

密码由 Caddy `basic_auth` 校验，bcrypt 哈希写在 `/etc/caddy/Caddyfile` 里，
明文不落在服务器任何文件上。改密码：

```bash
caddy hash-password --plaintext '新密码'
# 把输出替换 Caddyfile 里 qiyu.fanwulink.cn 段 basic_auth 下面那一行
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

> ⚠️ 这份文档里有明文密码，别直接发给客户或提交到公开仓库。

---

## 六、日常运维命令

```bash
# 两个服务一起看
systemctl status qiyu-receiver qiyu-console
sudo journalctl -u qiyu-receiver -f
sudo journalctl -u qiyu-console -f

# 重启 / 停用
sudo systemctl restart qiyu-receiver qiyu-console
sudo systemctl disable --now qiyu-receiver qiyu-console

# 看定时任务下次执行
systemctl list-timers qiyu-extract.timer

# 手动跑一次抽取
sudo systemctl start qiyu-extract.service

# 看库里有多少数据
cd /opt/qiyu-receiver
sudo -u qiyu env $(sudo grep -vE '^\s*#|^\s*$' .env | xargs) python3 run.py stats

# 备份数据库（走 sqlite3 在线备份 API，不需要 sqlite3 命令行）
sudo mkdir -p /var/backups/qiyu-receiver
sudo chown qiyu:qiyu /var/backups/qiyu-receiver
sudo -u qiyu env $(sudo grep -vE '^\s*#|^\s*$' .env | xargs) \
  python3 run.py backup --out /var/backups/qiyu-receiver
```

---

## 七、更新代码的流程

两个项目各有一份代码副本，**必须一起更新**，否则行为会分叉。

```bash
cd /d/test3
for DEST in /opt/qiyu-receiver /opt/qiyu-console; do
  tar czf - --exclude='./data' --exclude='./logs' --exclude='__pycache__' \
    --exclude='*.db*' --exclude='.git' --exclude='./.claude' \
    --exclude='./.env' --exclude='./.env.server' --exclude='./.env.console' . \
    | ssh -i ~/.ssh/token_panel_server_rsa ubuntu@159.75.3.3 \
      "sudo tar xzf - -C $DEST && sudo chown -R qiyu:qiyu $DEST"
done
ssh -i ~/.ssh/token_panel_server_rsa ubuntu@159.75.3.3 \
  'sudo systemctl restart qiyu-receiver qiyu-console'
```

> 上面的 tar 已经 `--exclude='./.env'`，服务器上的 `.env` 不会被覆盖。
> 如果哪天手工传过 `.env`，记得先 `sed -i 's/\r$//' .env` 再重启。

---

## 八、回滚

```bash
# 下线服务（保留数据）
sudo systemctl disable --now qiyu-receiver qiyu-extract.timer
sudo rm -f /etc/systemd/system/qiyu-receiver.service /etc/systemd/system/qiyu-extract.{service,timer}
sudo systemctl daemon-reload

# 从 Caddyfile 里删掉 qiyu.fanwulink.cn {} 整段，然后
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Caddyfile 每次改动前都会自动备份到 `/etc/caddy/Caddyfile.bak-<时间戳>-before-qiyu`。

---

## 九、下一步（需要用户操作）

1. 七鱼后台填接收 URL：`https://qiyu.fanwulink.cn/qiyu/event`
2. 确认租户是否走 **OpenApi 渠道**（不走这条通道收不到任何事件）
3. 如果要在七鱼后台之外补客户侧消息，需要写「OpenApi 渠道转发器」——它调七鱼发消息接口，
   同时 POST 一份到 `/qiyu/customer`（内网地址 `http://127.0.0.1:8860/qiyu/customer`）
4. 白名单：`159.75.3.3`（主动调七鱼接口时才需要；事件推送是七鱼推给我们，不需要）
