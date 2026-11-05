# DeepAudit 第三方 API 对接文档

面向第三方系统（SOC、DevSecOps 流水线、工单平台、数据看板等）的 REST API 对接说明，核心场景是**通过 API Key 拉取审计报告**，并覆盖触发审计、轮询进度、批量取报告等常见集成需求。

- 认证方式：`X-API-Key` 请求头（也兼容 Web 登录使用的 `Authorization: Bearer <JWT>`）
- 数据格式：JSON（报告额外支持 Markdown 附件下载、旧版任务支持 PDF）
- 接口风格：RESTful，全局前缀 `/api/v1`

> 本文档所有字段名、路径、枚举值均取自后端代码，与当前 `main` 分支一致。若你的实例做过定制，请以 `/api/v1/openapi.json` 为准。

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 通用约定](#2-通用约定)
- [3. API Key 管理](#3-api-key-管理)
- [4. 获取审计报告（核心）](#4-获取审计报告核心)
- [5. 定位 task_id：项目与任务查询](#5-定位-task_id项目与任务查询)
- [6. 通过 API 触发审计](#6-通过-api-触发审计)
- [7. 批量场景](#7-批量场景)
- [8. 旧版接口与 PDF 报告](#8-旧版接口与-pdf-报告)
- [9. 枚举字典](#9-枚举字典)
- [10. 完整对接示例](#10-完整对接示例)
- [11. 限制与注意事项](#11-限制与注意事项)
- [12. 排错对照表](#12-排错对照表)

---

## 1. 快速开始

三步拉取一份审计报告：

**第 1 步：生成 API Key**（需先用浏览器登录 DeepAudit Web 端，在「账户设置 → API Key」生成；或用 JWT 调接口生成，见 [3.2](#32-生成--重新生成-api-key)）。完整 Key 只展示一次，请妥善保存。

**第 2 步：找到目标审计任务 ID**

```bash
BASE="http://<your-host>:3000"      # 见 2.1 服务地址
KEY="da_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"

curl -H "X-API-Key: $KEY" "$BASE/api/v1/agent-tasks/?status=completed&limit=5"
```

**第 3 步：下载报告**

```bash
# JSON（推荐程序化消费）
curl -H "X-API-Key: $KEY" \
  "$BASE/api/v1/agent-tasks/<task_id>/report?format=json" -o report.json

# Markdown（附件下载，适合归档/转 PDF）
curl -H "X-API-Key: $KEY" \
  "$BASE/api/v1/agent-tasks/<task_id>/report?format=markdown" -o report.md
```

---

## 2. 通用约定

### 2.1 服务地址

所有接口统一前缀 `/api/v1`，可通过以下任一入口访问：

| 入口 | 地址示例 | 说明 |
|------|----------|------|
| 前端网关（推荐） | `http://<host>:3000/api/v1` | 生产环境 Nginx 反代（`docker-compose.prod.yml`），dev 环境为 `:5173` |
| 后端直连 | `http://<host>:8000/api/v1` | uvicorn 默认端口 8000，仅建议内网调用 |

健康检查（无需认证，**仅后端直连可用**）：

```bash
curl http://<host>:8000/health
# {"status":"ok"}
```

> 前端网关只反代 `/api/` 路径，`/health` 与 `/docs` 走网关会被 SPA 路由兜底返回 `index.html`。若需通过网关探活，请调用任一业务接口（如 `GET /api/v1/users/me/api-key`）观察状态码。

### 2.2 认证方式

| 方式 | 请求头 | 适用场景 |
|------|--------|----------|
| API Key（推荐三方使用） | `X-API-Key: da_xxx` | 服务端到服务端调用，长期有效（默认 365 天） |
| JWT Bearer | `Authorization: Bearer <access_token>` | Web 登录会话，有效期 8 天；**部分敏感接口只接受此方式** |

认证优先级：请求同时携带两者时，**先按 API Key 校验**。

> 注意：Key 直接放在 `X-API-Key` 头的值里，**不要**加 `Bearer ` 前缀。

### 2.3 请求与响应格式

- 请求体：`Content-Type: application/json`（登录接口除外，为 `application/x-www-form-urlencoded`）
- 响应体：JSON；报告 Markdown 为 `text/markdown`，旧版报告为 `application/pdf`
- 时间字段：ISO 8601（UTC，如 `2026-11-04T08:12:33.456789+00:00`）
- **无统一响应包装**：成功时直接返回业务数据（对象或数组），不包裹在 `{code, message, data}` 中
- 分页：查询参数 `skip`（偏移量，从 0 开始）+ `limit`（每页条数）；仅批量类接口返回 `{items, total}`，其余列表接口直接返回数组（无 total，需按 `limit` 判断是否还有下一页）

### 2.4 错误响应与状态码

错误统一为 FastAPI 默认结构：

```json
{ "detail": "无权访问此任务" }
```

参数校验错误（422）为：

```json
{ "detail": [ { "loc": ["query", "format"], "msg": "...", "type": "..." } ] }
```

| 状态码 | 含义 | 常见 `detail` |
|--------|------|---------------|
| 400 | 请求非法 / 状态冲突 | `用户已被禁用`、`有效期必须在 1 ~ 3650 天之间`、任务已结束不可取消 |
| 401 | 认证失败 | `未提供认证凭据`、`无效的 API Key`、`API Key 已过期，请重新生成` |
| 403 | 权限不足 | `无权访问此任务`、`无权访问此项目`、`此操作不允许使用 API Key，请使用登录会话（Bearer Token）` |
| 404 | 资源不存在 | `任务不存在`、`项目不存在` |
| 409 | 冲突 | 批量审计已有批次在运行 |
| 422 | 参数校验失败 | 见上方结构 |

### 2.5 在线 API 文档

| 地址 | 说明 |
|------|------|
| `http://<host>:8000/docs` | Swagger UI，可直接调试 |
| `http://<host>:8000/redoc` | ReDoc |
| `http://<host>:8000/api/v1/openapi.json` | OpenAPI 3 规范，可用于生成客户端 SDK |

以上地址需通过**后端端口**访问（前端网关不反代这些路径，见 [11.11](#11-限制与注意事项)）。

---

## 3. API Key 管理

### 3.1 关键事实

| 项目 | 说明 |
|------|------|
| Key 格式 | `da_` + 43 位随机串（`secrets.token_urlsafe(32)`），例：`da_9fKq2...` |
| 存储方式 | 服务端仅保存 SHA-256 哈希，**明文只在生成时返回一次**，丢失只能重新生成 |
| 展示前缀 | Key 前 10 位（如 `da_9fKq2x`），用于界面识别 |
| 数量限制 | 每个用户 **1 把** Key，重新生成后旧 Key 立即失效 |
| 默认有效期 | 365 天（后端配置项 `API_KEY_EXPIRE_DAYS`），生成时可指定 1~3650 天 |
| 权限范围 | 无 scope 概念，Key 权限 **等同该用户的全部权限**；仍受资源属主校验约束（见 [11](#11-限制与注意事项)） |
| 联动吊销 | 用户修改密码、管理员重置密码、账号被禁用，Key 均立即失效 |

### 3.2 生成 / 重新生成 API Key

**仅接受登录会话（JWT）**，用 API Key 调用会返回 403。登录认证通过即可生成，无需再次输入密码（兼容 SSO 用户）。

```
POST /api/v1/users/me/api-key
Authorization: Bearer <access_token>
```

请求体（可为空对象 `{}`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `expires_days` | int | 否 | 有效期天数，1~3650，缺省取服务端默认值（365） |

响应 `200`：

```json
{
  "api_key": "da_9fKq2xAbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
  "api_key_prefix": "da_9fKq2xA",
  "api_key_created_at": "2026-11-04T08:12:33.456789+00:00",
  "api_key_expires_at": "2027-11-04T08:12:33.456789+00:00"
}
```

获取 JWT（OAuth2 密码模式，`username` 传邮箱）：

```bash
curl -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=******"
# {"access_token":"<JWT>","token_type":"bearer"}
```

### 3.3 查询 API Key 状态

```
GET /api/v1/users/me/api-key
X-API-Key: da_xxx          # 或 Bearer JWT
```

响应（**不含 Key 本体**）：

```json
{
  "has_api_key": true,
  "api_key_prefix": "da_9fKq2xA",
  "api_key_created_at": "2026-11-04T08:12:33.456789+00:00",
  "api_key_expires_at": "2027-11-04T08:12:33.456789+00:00",
  "is_expired": false
}
```

建议第三方系统定期调用此接口做 Key 健康巡检（`is_expired=true` 时提醒重新生成）。

### 3.4 吊销 API Key

```
DELETE /api/v1/users/me/api-key     # 仅 JWT，立即失效
```

---

## 4. 获取审计报告（核心）

DeepAudit 的报告以**审计任务（Agent Task）**为单位组织：一次任务对应一份报告，报告内容包含安全评分、漏洞分布、逐条漏洞详情（含代码片段、修复建议、PoC）。

### 4.1 下载报告

```
GET /api/v1/agent-tasks/{task_id}/report?format=markdown|json
X-API-Key: da_xxx
```

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `task_id` | path | string | 是 | 审计任务 ID（UUID） |
| `format` | query | string | 否 | `markdown`（默认）或 `json`；其他值返回 422 |

**权限**：仅任务所属项目的 **owner** 可访问，否则 403 `无权访问此任务`。

**`format=json`** — `Content-Type: application/json`，结构如下：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `report_metadata.task_id` | string | 任务 ID |
| `report_metadata.project_id` | string | 项目 ID |
| `report_metadata.project_name` | string | 项目名称 |
| `report_metadata.generated_at` | string | 报告生成时间（UTC ISO 8601） |
| `report_metadata.task_status` | string | 任务状态，见 [9.1](#91-审计任务状态agenttaskstatus) |
| `report_metadata.duration_seconds` | int \| null | 审计耗时（秒），任务未结束时为 null |
| `summary.security_score` | float \| null | 安全评分（0~100，越高越安全），未计算时为 null |
| `summary.total_files_analyzed` | int | 已分析文件数 |
| `summary.total_findings` | int | 漏洞总数 |
| `summary.verified_findings` | int | 已验证（沙箱/PoC 确认）漏洞数 |
| `summary.severity_distribution` | object | `{critical, high, medium, low}` 各档数量 |
| `summary.agent_metrics.total_iterations` | int | Agent 迭代次数 |
| `summary.agent_metrics.tool_calls` | int | 工具调用次数 |
| `summary.agent_metrics.tokens_used` | int | LLM Token 消耗 |
| `findings[]` | array | 漏洞列表，按 critical→high→medium→low、同级按创建时间倒序 |
| `findings[].id` | string | 漏洞 ID |
| `findings[].title` | string | 漏洞标题 |
| `findings[].severity` | string | 严重程度，见 [9.2](#92-漏洞严重程度vulnerabilityseverity) |
| `findings[].vulnerability_type` | string | 漏洞类型，见 [9.3](#93-漏洞类型vulnerabilitytype) |
| `findings[].description` | string \| null | 漏洞描述 |
| `findings[].file_path` | string \| null | 文件路径（相对项目根） |
| `findings[].line_start` / `line_end` | int \| null | 起止行号 |
| `findings[].code_snippet` | string \| null | 漏洞代码片段 |
| `findings[].is_verified` | bool | 是否已验证 |
| `findings[].has_poc` | bool | 是否含 PoC |
| `findings[].poc_code` | string \| null | PoC 代码 |
| `findings[].poc_description` | string \| null | PoC 说明 |
| `findings[].poc_steps` | array \| null | 复现步骤（字符串数组） |
| `findings[].confidence` | float \| null | AI 置信度（0~1） |
| `findings[].suggestion` | string \| null | 修复建议 |
| `findings[].fix_code` | string \| null | 参考修复代码 |
| `findings[].created_at` | string \| null | 发现时间 |

示例：

```json
{
  "report_metadata": {
    "task_id": "3f2b1c9e-8a7d-4e6f-9b1a-2c3d4e5f6a7b",
    "project_id": "a1b2c3d4-0000-1111-2222-333344445555",
    "project_name": "order-service",
    "generated_at": "2026-11-04T08:12:33.456789+00:00",
    "task_status": "completed",
    "duration_seconds": 1264
  },
  "summary": {
    "security_score": 72.0,
    "total_files_analyzed": 318,
    "total_findings": 9,
    "verified_findings": 4,
    "severity_distribution": { "critical": 1, "high": 3, "medium": 4, "low": 1 },
    "agent_metrics": { "total_iterations": 42, "tool_calls": 187, "tokens_used": 512340 }
  },
  "findings": [
    {
      "id": "f0e1d2c3-4b5a-6978-8796-a5b4c3d2e1f0",
      "title": "订单查询接口存在 SQL 注入",
      "severity": "critical",
      "vulnerability_type": "sql_injection",
      "description": "用户可控的 order_id 直接拼接进 SQL 语句...",
      "file_path": "src/api/order.py",
      "line_start": 88,
      "line_end": 91,
      "code_snippet": "cursor.execute(\"SELECT * FROM orders WHERE id = '%s'\" % order_id)",
      "is_verified": true,
      "has_poc": true,
      "poc_code": "sqlmap -u 'https://target/api/order?id=1' ...",
      "poc_description": "基于时间盲注验证成功",
      "poc_steps": ["构造 payload", "观察响应延时", "确认注入点"],
      "confidence": 0.93,
      "suggestion": "改用参数化查询...",
      "fix_code": "cursor.execute(\"SELECT * FROM orders WHERE id = %s\", (order_id,))",
      "created_at": "2026-11-04T07:58:02.123456+00:00"
    }
  ]
}
```

**`format=markdown`** — 返回附件下载：

```
Content-Type: text/markdown
Content-Disposition: attachment; filename=audit_report_3f2b1c9e_20261104.md
```

Markdown 报告章节：报告信息（项目名、任务 ID、生成时间、任务状态、耗时）→ 执行摘要（安全评分与评级）→ 漏洞发现概览（各档数量与已验证数）→ 审计指标（分析文件数、迭代次数、工具调用、Token、PoC 数）→ 按严重程度分组的漏洞详情（位置、置信度、描述、漏洞代码、修复建议、参考修复代码、PoC 与复现步骤）→ 修复优先级建议。

> 报告接口**不校验任务状态**：任务运行中也可调用，此时返回的是已产出的部分结果（`report_metadata.task_status` 会体现真实状态）。建议在 `status=completed` 后再取报告。

### 4.2 任务摘要（轻量统计，适合看板）

```
GET /api/v1/agent-tasks/{task_id}/summary
```

```json
{
  "task_id": "3f2b1c9e-...",
  "status": "completed",
  "security_score": 72,
  "total_findings": 9,
  "verified_findings": 4,
  "severity_distribution": { "critical": 1, "high": 3, "medium": 4, "low": 1 },
  "vulnerability_types": { "sql_injection": 2, "xss": 3, "hardcoded_secret": 4 },
  "duration_seconds": 1264,
  "phases_completed": ["planning", "indexing", "analysis", "verification", "reporting"]
}
```

> `severity_distribution` / `vulnerability_types` 为动态 key 字典，只包含计数大于 0 的项；`info` 档可能出现在此接口，但不出现在 4.1 的 `severity_distribution` 中。

### 4.3 漏洞明细分页查询

```
GET /api/v1/agent-tasks/{task_id}/findings?severity=high&verified_only=true&skip=0&limit=50
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `severity` | string | 全部 | 过滤严重程度；非法值会被忽略（不报错） |
| `verified_only` | bool | `false` | 仅返回已验证漏洞 |
| `skip` | int ≥0 | `0` | 偏移量 |
| `limit` | int 1~200 | `50` | 每页条数 |

响应为数组，每项字段：`id`、`task_id`、`vulnerability_type`、`severity`、`title`、`description`、`file_path`、`line_start`、`line_end`、`code_snippet`、`is_verified`、`confidence`、`status`、`suggestion`、`poc`、`created_at`。

> 与 4.1 JSON 报告的差异：此处 `poc` 为聚合对象（而非 `poc_code`/`poc_steps` 等平铺字段），且额外返回处置状态 `status`。

### 4.4 更新漏洞处置状态（回写工单结论）

```
PATCH /api/v1/agent-tasks/{task_id}/findings/{finding_id}
Content-Type: application/json

{ "status": "false_positive" }
```

`status` 合法值：`new`、`analyzing`、`verified`、`false_positive`、`needs_review`、`fixed`、`wont_fix`、`duplicate`。

响应：`{ "message": "...", "finding_id": "...", "status": "false_positive" }`

---

## 5. 定位 task_id：项目与任务查询

第三方通常按「项目 → 任务 → 报告」的链路取数。

### 5.1 项目列表

```
GET /api/v1/projects/?skip=0&limit=100&include_deleted=false
```

仅返回**当前 API Key 所属用户拥有**的项目。响应数组每项：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 项目 ID |
| `name` | string | 项目名 |
| `description` | string \| null | 描述 |
| `source_type` | string | `repository`（Git 仓库）/ `zip`（上传压缩包） |
| `repository_url` | string \| null | 仓库地址 |
| `repository_type` | string \| null | `github` / `gitlab` / `other` |
| `default_branch` | string \| null | 默认分支 |
| `programming_languages` | array \| null | 语言列表 |
| `owner_id` | string | 属主用户 ID |
| `is_active` | bool | 是否有效（软删除标记） |
| `created_at` / `updated_at` | string | 时间戳 |
| `owner` | object \| null | 属主信息 `{id, email, full_name, avatar_url, role}` |

配套接口：`GET /api/v1/projects/{project_id}`（详情）、`GET /api/v1/projects/stats`（全局统计：项目数、任务数、漏洞数、平均分）、`POST /api/v1/projects/`（创建项目）。

### 5.2 任务列表

```
GET /api/v1/agent-tasks/?project_id=<pid>&status=completed&skip=0&limit=20
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `project_id` | string | 全部 | 按项目过滤 |
| `status` | string | 全部 | 按状态过滤，非法值被忽略 |
| `skip` | int ≥0 | `0` | 偏移量 |
| `limit` | int 1~100 | `20` | 每页条数 |

按创建时间倒序返回数组，每项为任务对象（见 5.3）。

### 5.3 任务详情与状态轮询

```
GET /api/v1/agent-tasks/{task_id}
```

常用字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` / `project_id` / `name` / `description` | string | 基本信息 |
| `status` | string | 任务状态，见 [9.1](#91-审计任务状态agenttaskstatus) |
| `current_phase` / `current_step` | string \| null | 当前阶段与步骤 |
| `progress_percentage` | float | 进度百分比（0~100） |
| `total_files` / `indexed_files` / `analyzed_files` | int | 文件处理进度 |
| `total_iterations` / `tool_calls_count` / `tokens_used` | int | Agent 运行指标（运行中会取实时值） |
| `findings_count` / `total_findings` | int | 漏洞总数（两个字段等价，兼容命名） |
| `verified_count` / `verified_findings` | int | 已验证数（等价） |
| `false_positive_count` | int | 误报数 |
| `critical_count` / `high_count` / `medium_count` / `low_count` | int | 各档数量 |
| `security_score` | float \| null | 安全评分 |
| `quality_score` | float | 代码质量分 |
| `created_at` / `started_at` / `completed_at` | string \| null | 时间戳 |
| `target_vulnerabilities` / `verification_level` / `branch_name` / `exclude_patterns` / `target_files` | - | 任务配置回显 |
| `error_message` | string \| null | 失败原因 |

**轮询建议**：间隔 15~30 秒；终态为 `completed` / `failed` / `cancelled`；`pending`、`initializing`、`running`、`planning`、`indexing`、`analyzing`、`verifying`、`reporting`、`paused` 均为非终态。也可改用 SSE 事件流（见 [6.3](#63-实时事件流sse)）。

---

## 6. 通过 API 触发审计

### 6.1 创建审计任务

```
POST /api/v1/agent-tasks/
X-API-Key: da_xxx
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `project_id` | string | 是 | - | 项目 ID，必须为当前用户所属项目 |
| `name` | string | 否 | `Agent Audit - <时间戳>` | 任务名 |
| `description` | string | 否 | null | 描述 |
| `audit_scope` | object | 否 | null | 审计范围配置 |
| `target_vulnerabilities` | array | 否 | `["sql_injection","xss","command_injection","path_traversal","ssrf"]` | 目标漏洞类型，取值见 [9.3](#93-漏洞类型vulnerabilitytype) |
| `verification_level` | string | 否 | `sandbox` | `analysis_only`（仅静态分析）/ `sandbox`（沙箱验证）/ `generate_poc`（生成 PoC） |
| `branch_name` | string | 否 | null | 分支，缺省用项目默认分支 |
| `exclude_patterns` | array | 否 | `["node_modules","__pycache__",".git","*.min.js"]` | 排除规则 |
| `target_files` | array | 否 | null | 仅审计指定文件 |
| `max_iterations` | int 1~200 | 否 | `50` | Agent 最大迭代次数 |
| `timeout_seconds` | int 60~7200 | 否 | `1800` | 超时时间 |

响应 `200` 返回任务对象（`status` 通常为 `pending`），任务在后台异步执行。示例：

```bash
curl -X POST "$BASE/api/v1/agent-tasks/" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
        "project_id": "a1b2c3d4-0000-1111-2222-333344445555",
        "name": "CI 流水线审计 #1024",
        "verification_level": "sandbox",
        "branch_name": "release/1.8",
        "timeout_seconds": 3600
      }'
```

### 6.2 取消任务

```
POST /api/v1/agent-tasks/{task_id}/cancel
```

任务已进入终态时返回 400。

### 6.3 实时事件流（SSE）

| 接口 | 说明 |
|------|------|
| `GET /api/v1/agent-tasks/{task_id}/events?after_sequence=0` | 基础事件流 |
| `GET /api/v1/agent-tasks/{task_id}/stream?include_thinking=true&include_tool_calls=true&after_sequence=0` | 增强事件流（含推理与工具调用） |
| `GET /api/v1/agent-tasks/{task_id}/events/list` | 一次性拉取历史事件数组（非流式，适合断点补齐） |

事件对象字段：`id`、`task_id`、`event_type`、`phase`、`message`、`sequence`（单调递增，用于断点续读）、`timestamp`、`tool_name`、`tool_input`、`tool_output`、`tool_duration_ms`、`progress_percent`、`finding_id`、`tokens_used`、`metadata`。

---

## 7. 批量场景

### 7.1 定时批量审计

用于「一次审计 N 个项目」，第三方按批次维度拉取结果。

| 接口 | 说明 |
|------|------|
| `POST /api/v1/batch-audit/schedules` | 创建调度（`schedule_type`: `once`/`daily`/`weekly`/`interval`/`manual`，`project_scope`: `all`/`selected`，并发 ≤10） |
| `GET /api/v1/batch-audit/schedules?skip&limit` | 调度列表，返回 `{items, total}` |
| `PATCH /api/v1/batch-audit/schedules/{schedule_id}` | 更新（含启停 `enabled`） |
| `DELETE /api/v1/batch-audit/schedules/{schedule_id}` | 删除调度（历史批次保留） |
| `POST /api/v1/batch-audit/schedules/{schedule_id}/trigger` | 手动触发一次批次；已有批次在跑返回 409 |
| `GET /api/v1/batch-audit/runs?schedule_id&status&skip&limit` | 批次列表 |
| `GET /api/v1/batch-audit/runs/{run_id}` | 批次详情与进度：`total_projects`、`queued_count`、`running_count`、`completed_count`、`failed_count`、`skipped_count`、`progress` |
| `GET /api/v1/batch-audit/runs/{run_id}/items?status&skip&limit` | 批次明细，每项含 **`agent_task_id`** |
| `POST /api/v1/batch-audit/runs/{run_id}/cancel` | 取消批次 |

**取报告链路**：`runs/{run_id}/items` 拿到每个项目的 `agent_task_id` → 逐个调用 `GET /api/v1/agent-tasks/{agent_task_id}/report?format=json`。

批次状态：`pending`/`running`/`completed`/`partially_failed`/`failed`/`cancelled`；明细状态：`queued`/`running`/`completed`/`failed`/`skipped`/`cancelled`。

### 7.2 批量报告下载

`/api/v1/report-downloads` 系列接口用于把多个项目「最近一次已完成审计」的 Markdown 报告批量导出：

| 接口 | 说明 |
|------|------|
| `POST /api/v1/report-downloads/gitlab/projects` | 按 GitLab 地址/分组检索项目并匹配本地项目 |
| `POST /api/v1/report-downloads` | 创建导出任务（`project_ids` 1~5000、`concurrency` ≤20、`filename_pattern`: `id_name`/`name`/`id`） |
| `GET /api/v1/report-downloads?skip&limit` | 任务列表 `{items, total}` |
| `GET /api/v1/report-downloads/{task_id}` | 任务详情与进度 |
| `GET /api/v1/report-downloads/{task_id}/items?status&skip&limit` | 明细（成功/失败/跳过） |
| `POST /api/v1/report-downloads/{task_id}/retry` \| `/resume` \| `/cancel` | 失败重试 / 断点续传 / 取消 |
| `GET /api/v1/report-downloads/{task_id}/logs?limit` | 日志 |
| `GET /api/v1/report-downloads/{task_id}/summary` | 汇总 Markdown（附件下载） |

> **重要**：该功能产出的 `.md` 文件写入**服务端本地目录**（默认 `./data/report_downloads/{task_id}`，配置项 `REPORT_DOWNLOAD_OUTPUT_DIR`），**不提供按文件名的 HTTP 下载接口**（仅 `summary` 可通过 HTTP 获取）。第三方若需程序化取报告，请使用 [4.1](#41-下载报告) 逐任务下载，或从服务端共享存储读取导出目录。

---

## 8. 旧版接口与 PDF 报告

系统保留了一套旧版扫描任务接口（表 `audit_tasks` / `audit_issues`），与 Agent 任务相互独立。**新对接建议统一使用 `/api/v1/agent-tasks`**；仅在需要 PDF 产物时使用以下接口。

| 接口 | 说明 |
|------|------|
| `GET /api/v1/tasks/?project_id=<pid>` | 旧版任务列表（无分页参数） |
| `GET /api/v1/tasks/{task_id}` | 旧版任务详情 |
| `POST /api/v1/tasks/{task_id}/cancel` | 取消（仅 `pending`/`running`） |
| `GET /api/v1/tasks/{task_id}/issues` | 问题列表：`file_path`、`line_number`、`column_number`、`issue_type`、`severity`、`title`、`message`、`description`、`suggestion`、`code_snippet`、`ai_explanation`、`status`、`resolved_by`、`resolved_at` |
| `PATCH /api/v1/tasks/{task_id}/issues/{issue_id}` | 更新状态：`open`/`resolved`/`false_positive` |
| **`GET /api/v1/tasks/{task_id}/report/pdf`** | 下载 PDF 报告，`application/pdf`，文件名 `audit-report-<id前8位>-<YYYYMMDD>.pdf`，仅任务创建者可访问 |
| `POST /api/v1/scan/instant` | 即时分析代码片段：`{code, language, prompt_template_id?}` |
| `GET /api/v1/scan/instant/history?limit=20` | 即时分析历史 |
| **`GET /api/v1/scan/instant/history/{analysis_id}/report/pdf`** | 即时分析 PDF，文件名 `instant-analysis-<language>-<id前8位>.pdf` |

旧版任务状态：`pending`/`running`/`completed`/`failed`/`cancelled`。

---

## 9. 枚举字典

### 9.1 审计任务状态（AgentTaskStatus）

| 值 | 含义 | 终态 |
|----|------|------|
| `pending` | 等待执行 | 否 |
| `initializing` | 初始化中 | 否 |
| `running` | 运行中 | 否 |
| `planning` | 规划阶段 | 否 |
| `indexing` | 代码索引阶段 | 否 |
| `analyzing` | 漏洞分析阶段 | 否 |
| `verifying` | 验证阶段（沙箱/PoC） | 否 |
| `reporting` | 报告生成中 | 否 |
| `paused` | 已暂停 | 否 |
| `completed` | 已完成 | **是** |
| `failed` | 失败 | **是** |
| `cancelled` | 已取消 | **是** |

执行阶段（`current_phase` / `phases_completed`）：`planning`、`indexing`、`reconnaissance`、`analysis`、`verification`、`reporting`。

### 9.2 漏洞严重程度（VulnerabilitySeverity）

`critical`（严重）、`high`（高危）、`medium`（中危）、`low`（低危）、`info`（提示）

> JSON 报告的 `severity_distribution` 只统计前四档；`info` 会计入 `total_findings`，需从 `findings[]` 自行统计。

### 9.3 漏洞类型（VulnerabilityType）

`sql_injection`、`nosql_injection`、`xss`、`command_injection`、`code_injection`、`path_traversal`、`file_inclusion`、`ssrf`、`xxe`、`deserialization`、`auth_bypass`、`idor`、`sensitive_data_exposure`、`hardcoded_secret`、`weak_crypto`、`race_condition`、`business_logic`、`memory_corruption`、`other`

### 9.4 漏洞处置状态（FindingStatus）

`new`、`analyzing`、`verified`、`false_positive`、`needs_review`、`fixed`、`wont_fix`、`duplicate`

### 9.5 验证级别（verification_level）

| 值 | 说明 |
|----|------|
| `analysis_only` | 仅静态分析，速度最快，不产出 PoC |
| `sandbox` | 沙箱验证（默认），`is_verified` 更可信 |
| `generate_poc` | 验证并生成可复现 PoC，耗时最长 |

---

## 10. 完整对接示例

### 10.1 Shell：导出某项目所有已完成任务的报告

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="http://<your-host>:3000"
KEY="da_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
PROJECT_ID="a1b2c3d4-0000-1111-2222-333344445555"
OUT="./reports"; mkdir -p "$OUT"

# 分页拉取已完成任务
skip=0
while :; do
  page=$(curl -sf -H "X-API-Key: $KEY" \
    "$BASE/api/v1/agent-tasks/?project_id=$PROJECT_ID&status=completed&skip=$skip&limit=20")
  ids=$(echo "$page" | jq -r '.[].id')
  [ -z "$ids" ] && break
  for id in $ids; do
    curl -sf -H "X-API-Key: $KEY" \
      "$BASE/api/v1/agent-tasks/$id/report?format=json" -o "$OUT/$id.json"
    curl -sf -H "X-API-Key: $KEY" \
      "$BASE/api/v1/agent-tasks/$id/report?format=markdown" -o "$OUT/$id.md"
    echo "saved $id"
  done
  skip=$((skip + 20))
done
```

### 10.2 Python：触发审计 → 轮询 → 取报告

```python
"""DeepAudit 第三方对接示例：触发审计、轮询进度、下载报告。"""
import time
import requests

BASE_URL = "http://<your-host>:3000/api/v1"
HEADERS = {"X-API-Key": "da_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
TERMINAL = {"completed", "failed", "cancelled"}


def create_task(project_id: str, branch: str | None = None) -> str:
    payload = {
        "project_id": project_id,
        "name": f"CI Audit {int(time.time())}",
        "verification_level": "sandbox",
        "branch_name": branch,
        "timeout_seconds": 3600,
    }
    r = requests.post(f"{BASE_URL}/agent-tasks/", json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def wait_task(task_id: str, interval: int = 20, timeout: int = 7200) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{BASE_URL}/agent-tasks/{task_id}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        task = r.json()
        print(f"[{task['status']}] {task['progress_percentage']:.1f}% - {task.get('current_step')}")
        if task["status"] in TERMINAL:
            return task
        time.sleep(interval)
    raise TimeoutError(f"task {task_id} timeout")


def fetch_report(task_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/agent-tasks/{task_id}/report",
        params={"format": "json"},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    tid = create_task("a1b2c3d4-0000-1111-2222-333344445555", branch="main")
    task = wait_task(tid)
    if task["status"] != "completed":
        raise SystemExit(f"审计未成功完成: {task.get('error_message')}")

    report = fetch_report(tid)
    s = report["summary"]
    print(f"安全评分 {s['security_score']}，漏洞 {s['total_findings']} 个，"
          f"已验证 {s['verified_findings']} 个，分布 {s['severity_distribution']}")

    # 仅推送高危及以上到工单系统
    for f in report["findings"]:
        if f["severity"] in ("critical", "high"):
            print(f"-> [{f['severity']}] {f['title']} @ {f['file_path']}:{f['line_start']}")
```

### 10.3 DevSecOps 流水线卡点（Jenkins / GitLab CI 思路）

```bash
SCORE=$(curl -sf -H "X-API-Key: $KEY" \
  "$BASE/api/v1/agent-tasks/$TASK_ID/summary" | jq '.security_score')
CRIT=$(curl -sf -H "X-API-Key: $KEY" \
  "$BASE/api/v1/agent-tasks/$TASK_ID/report?format=json" \
  | jq '.summary.severity_distribution.critical')

if [ "$(echo "$CRIT > 0" | bc)" -eq 1 ] || [ "$(echo "$SCORE < 60" | bc)" -eq 1 ]; then
  echo "安全卡点未通过：critical=$CRIT, score=$SCORE"; exit 1
fi
```

---

## 11. 限制与注意事项

1. **资源属主校验（最常见的 403 原因）**：报告、任务、项目接口均校验「项目 `owner_id` == API Key 所属用户」。即使把该用户加为项目成员（member），也**无法**通过 API 读取他人项目的报告。三方集成请使用**项目属主账号**生成的 API Key，或为三方单独建号并把项目建在该账号下。
2. **单用户单 Key**：一个账号只能有 1 把 Key，重新生成会立即作废旧 Key。若有多个下游系统，请分别使用不同账号，避免互相顶掉。
3. **无权限分级（scope）**：Key 拥有该账号的全部权限（superuser 账号的 Key 即管理员权限）。请勿把管理员账号的 Key 交给三方，建议创建**只用于集成的普通账号**。
4. **无接口速率限制**：后端未做限流，请在调用方自行控制并发与频率（建议报告拉取并发 ≤5，轮询间隔 ≥15 秒），避免影响审计任务执行。
5. **报告格式仅 Markdown / JSON**：Agent 任务报告**不支持** HTML、SARIF、PDF；如需 PDF，请用旧版接口 `GET /api/v1/tasks/{id}/report/pdf`，或自行将 Markdown 转换（如 pandoc）。
6. **CVSS 字段未输出**：数据模型中存在 `cvss_score` / `cvss_vector`，但报告与漏洞列表接口**未返回**这两个字段，请勿依赖。
7. **Key 生命周期**：修改密码、管理员重置密码、账号禁用都会吊销 Key；Key 过期后接口返回 401 `API Key 已过期，请重新生成`，而重新生成**必须使用登录会话**（API Key 无法自我续期），请在运维流程中预留人工介入环节。
8. **列表接口无 total**：项目、任务、漏洞列表直接返回数组，需以 `len(结果) < limit` 作为翻页终止条件；批量审计与批量下载接口才返回 `{items, total}`。
9. **传输安全**：生产环境请仅在 HTTPS 下传输 API Key；Key 应存放在密钥管理系统或 CI 变量中，禁止硬编码进代码仓库。
10. **SSE 长连接**：事件流接口为长连接，经 Nginx 反代时已关闭缓冲（`proxy_buffering off`，`proxy_read_timeout 300s`，见 `frontend/nginx.conf`）；如自建网关需同样关闭缓冲，并把读超时设为不小于 5 分钟，否则长任务会被中途断开。断开后可用 `after_sequence` 参数续读。
11. **在线文档路径**：`/docs`、`/redoc`、`/api/v1/openapi.json` 挂在后端服务上，经前端网关访问 `/docs` 会命中 SPA 兜底页面；请通过后端端口（默认 8000）访问。

---

## 12. 排错对照表

| 现象 | 原因 | 处理 |
|------|------|------|
| 401 `未提供认证凭据` | 未带 `X-API-Key`，或头名写错（如写成 `Authorization: ApiKey`） | 使用 `X-API-Key: da_xxx` |
| 401 `无效的 API Key` | Key 拼写错误 / 已被重新生成作废 / 用户改过密码 | 重新登录 Web 端生成并更新配置 |
| 401 `API Key 已过期，请重新生成` | 超过有效期 | 用 JWT 调 `POST /users/me/api-key` 重新生成 |
| 400 `用户已被禁用` | 账号 `is_active=false` | 联系管理员启用账号 |
| 403 `此操作不允许使用 API Key...` | 用 Key 调用了仅登录会话可用的接口（生成/吊销 Key、改密码） | 改用 `Authorization: Bearer <JWT>` |
| 403 `无权访问此任务` / `无权访问此项目` | 资源属主不是 Key 所属用户 | 见 [11.1](#11-限制与注意事项)，改用属主账号的 Key |
| 404 `任务不存在` | `task_id` 错误，或把旧版 `tasks` 的 ID 用在了 `agent-tasks` 上 | 用 `GET /api/v1/agent-tasks/` 确认 ID |
| 422 参数校验失败 | `format` 非 `markdown`/`json`，或 `limit`、`expires_days` 越界 | 按文档取值范围修正 |
| 报告 `findings` 为空但任务在跑 | 报告接口不校验状态，返回当前已产出结果 | 等 `status=completed` 再取 |
| 报告 `security_score` 为 null | 任务未产出评分（提前失败/取消） | 检查 `error_message` 与任务状态 |
| 拉不到批量导出的 .md 文件 | 产物落在服务端磁盘目录，无 HTTP 下载接口 | 改用逐任务报告接口，或从服务器目录取件 |

---

## 附：接口速查

| 场景 | 接口 |
|------|------|
| 健康检查 | `GET /health` |
| 获取 JWT | `POST /api/v1/auth/login` |
| 生成 API Key | `POST /api/v1/users/me/api-key`（仅 JWT） |
| 查询 Key 状态 | `GET /api/v1/users/me/api-key` |
| 吊销 Key | `DELETE /api/v1/users/me/api-key`（仅 JWT） |
| **下载报告** | **`GET /api/v1/agent-tasks/{task_id}/report?format=json\|markdown`** |
| 报告摘要 | `GET /api/v1/agent-tasks/{task_id}/summary` |
| 漏洞明细 | `GET /api/v1/agent-tasks/{task_id}/findings` |
| 回写处置状态 | `PATCH /api/v1/agent-tasks/{task_id}/findings/{finding_id}` |
| 项目列表 | `GET /api/v1/projects/` |
| 任务列表 / 详情 | `GET /api/v1/agent-tasks/` / `GET /api/v1/agent-tasks/{task_id}` |
| 触发审计 / 取消 | `POST /api/v1/agent-tasks/` / `POST /api/v1/agent-tasks/{task_id}/cancel` |
| 事件流 | `GET /api/v1/agent-tasks/{task_id}/events` \| `/stream` |
| 批量审计批次 | `GET /api/v1/batch-audit/runs/{run_id}/items` |
| 批量报告导出 | `POST /api/v1/report-downloads` |
| 旧版 PDF 报告 | `GET /api/v1/tasks/{task_id}/report/pdf` |
| OpenAPI 规范 | `GET /api/v1/openapi.json` |
