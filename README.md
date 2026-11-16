# 数据服务编排发布平台

面向数据分析结果的API服务化、查询编排、接口限流、服务监控、版本管理与消费方登记的数据服务编排发布平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 服务注册 | `service` | 数据服务 | 服务编号、服务名称、数据来源 |
| 查询编排 | `flow` | 编排流程 | 流程编号、流程名称、步骤序列 |
| 接口限流 | `ratelimit` | 限流规则 | 规则编号、关联服务、每秒上限 |
| 鉴权管理 | `auth` | 鉴权配置 | 配置编号、关联服务、鉴权方式 |
| 服务监控 | `monitor` | 监控指标 | 指标编号、关联服务、QPS阈值 |
| 消费方登记 | `consumer` | 消费应用 | 应用编号、应用名称、订阅服务 |
| 版本管理 | `version` | 版本记录 | 版本号、关联服务、变更描述 |
| 结果缓存 | `cache` | 缓存策略 | 策略编号、关联服务、缓存键模板 |
| 响应转换 | `transform` | 转换模板 | 模板编号、关联服务、输入字段 |
| 网关路由 | `gateway` | 路由规则 | 规则编号、路径前缀、目标服务 |
| 接口文档 | `schema` | 接口定义 | 定义编号、关联服务、请求方法 |
| 数据源配置 | `datasource` | 数据源连接 | 连接编号、连接类型、接入地址 |
| 数据查询 | `query` | 查询定义 | 定义编号、关联数据源、查询模板 |
| 数据清洗 | `cleanse` | 清洗规则 | 规则编号、关联数据集、空值策略 |
| 调用日志 | `log` | 日志记录 | 记录编号、调用服务、请求时间 |
| 模拟数据 | `mock` | 模拟规则 | 规则编号、关联服务、触发条件 |
| 数据备份 | `backup` | 备份策略 | 策略编号、目标服务、备份周期 |
| 事件通知 | `notify` | 通知规则 | 规则编号、触发事件、通知渠道 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 查询编排执行引擎（flow）

查询编排不止保存步骤序列，还能按数据依赖真正执行并产出可复核的合并数据集，
核心逻辑在 `backend/app/services/orchestrator.py`（纯函数引擎，不直接读写仓库）。

- **依赖解析与唯一执行顺序**：把数据依赖解析成 DAG 并做 Kahn 拓扑排序，同波次按步骤
  序列稳定排序；循环依赖、悬空依赖、重复步骤直接拒绝。并行度只限制同波次并发宽度，
  不产生第二套顺序——并行度 1 与 2 的线性执行顺序完全一致。
- **状态单向流转**：批次状态机（待执行→执行中→回退中→已完成/已失败）与步骤状态机
  （待调度→就绪→执行中→已完成/失败→回退中→待调度/已跳过）都只能相邻流转，跳级抛
  `StateTransitionError`；流程本身的 草稿→待校验→已启用→已停用 同样禁止跳级。
- **失败回退**：按显式恢复边界或沿数据依赖就近选择边界回退，旧数据产物标记“已作废”
  但保留历史，因此任一步骤同一时间只有一份有效成功产物（A 只执行一次）。两个恢复边界
  并存时仍在同一套执行顺序内回退。
- **合并与口径优先级**：按执行顺序合并各步骤产物；同名字段多口径并存时，先比显式口径
  优先级（数字小者胜），相同再按执行顺序靠前者胜，每条判定依据写入 `merge_decisions`。
- **一致性闸门**：落库前对账数据依赖、步骤执行记录、合并结果，三者不一致即拒绝写入，
  避免后续步骤读取未完成的数据产物。
- **编排步骤契约（flow_contract）**：执行成功后由归属团队“查询编排团队”把同一套执行
  顺序与字段清单写入契约（`services/contract.py`），响应转换模块经
  `GET /api/transform/contracts/summary` 只读消费；共享执行结果只读，其他团队不能改
  步骤顺序（`POST /api/flow/{id}/steps/reorder` 校验归属团队且只允许同拓扑波次内互换）。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/flow/{id}/execute` | 按数据依赖执行编排（可带 `team`、`fail_steps` 注入失败复测） |
| GET | `/api/flow/{id}/runs` | 执行批次列表，执行顺序与步骤记录可逐项复核 |
| GET | `/api/flow/{id}/runs/{run_no}` | 单批次明细：事件时间线、回退记录、合并数据集、一致性闸门 |
| GET | `/api/flow/contracts/list` | 编排步骤契约（只读） |
| POST | `/api/flow/{id}/steps/reorder` | 归属团队调整步骤顺序（仅同波次，跨依赖层级拒绝并回滚） |
| GET | `/api/transform/contracts/summary` | 转换模块只读消费同一份执行顺序与字段清单 |

### 测试

```bash
cd backend
.venv/bin/python -m unittest app.tests.test_flow_execution -v
```

覆盖依赖解析/循环检测、唯一执行顺序、A 只成功一次、失败回退与作废留痕、双恢复边界、
口径优先级判定、一致性闸门、状态机跳级拦截、归属团队权限与 FLOW-0002（A→B、A→C，
并行度 2）端到端复测。

领域：数据工程
子方向：数据分析与数据应用
