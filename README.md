# 七鱼会话分析

基于现有七鱼会话接收、问答抽取和数据分析服务，使用 React、TypeScript 和 shadcn/ui 官方 Dashboard 组件重构管理界面。

## 目录

- `frontend/`：React + Vite + Tailwind CSS 前端。使用真实 Radix 控件、Recharts 图表与 Lucide 图标。
- `console/`：现有 Python 看板服务，对应服务器 `/opt/qiyu-console`。
- `receiver/`：现有 Python 消息接收服务，对应 `/opt/qiyu-receiver`。两个代码副本包含各自的兼容修正，不能互相整包覆盖。
- `console/src/web/static/`：前端生产构建结果，由现有 Python 服务提供。

## 开发

```sh
cd frontend
npm ci
npm run dev
```

前端生产使用同域 `/api/*` 接口。开发时可通过 Vite 代理连接本机看板服务；不在源码中保存管理员密钥、Cookie 或服务器环境文件。

```sh
npm run build
```

## 登录与数据

登录保留原有独立 `/login` 页面、管理员密钥和 `qy_admin` Cookie。前端遇到 401 跳回原登录页，不引入新的鉴权服务。

默认读取本地业务库；演示库独立存在。问答列表由服务端搜索和分页，详情使用真实脱敏上下文。未发现回复只表示抽取答案为空，不表示问题已确认未解决。

导入沿用原接口的文件预览、摘要核验、事务确认与同源校验；重新清洗需要明确确认。仓库不包含生产数据、环境文件或密钥。

## 更新服务

1. 备份当前 `/opt/qiyu-console` 与 SQLite 一致性快照。
2. 执行前端 TypeScript 检查、生产构建与现有后端测试。
3. 将前端 `dist/` 放入新的静态资源目录，原子切换到 `console/src/web/static/`。
4. 保留上一版目录用于回退，核验登录页、API、静态资源与接收服务健康状态。

仅静态前端更新时无需重启消息接收端，不变更原鉴权、Caddy 路由或数据库结构。

## 开源来源

界面使用 [shadcn/ui 官方 dashboard-01](https://ui.shadcn.com/blocks/dashboard) 及其基础组件，保留 MIT 许可证。具体来源与组件清单见 `frontend/UPSTREAM.json` 和 `frontend/LICENSE.shadcn.md`。
