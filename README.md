# Async Bi-directional Channel Template (Server + GitHub Issue Queue + External Controller)

这是一个可直接落地的**异步双向通道模板**：

- **GitHub Issues 作为队列层**（命令队列 + 事件队列）
- **Server 端**轮询命令 issue、执行处理器、回写响应
- **External Controller**提交命令并异步等待结果

> 适合内网受限、仅可出站到 GitHub API 或需要“低耦合异步控制通道”的场景。

---

## 通道协议（v1）

### 命令队列（Controller -> Server）

- 新建 Issue
- Labels: `channel:cmd`, `channel:pending`
- Title: `[cmd] <command> (<request_id>)`
- Body: JSON

```json
{
  "version": "v1",
  "request_id": "req_xxx",
  "command": "ping",
  "args": {}
}
```

### 响应（Server -> Controller）

Server 处理后：

1. 在命令 Issue 下写评论（主响应通道）
2. 可选创建事件 Issue（辅助反向异步通道）

评论格式：

```text
<!-- channel-response-v1 -->
{ ...json... }
```

响应 JSON：

```json
{
  "version": "v1",
  "request_id": "req_xxx",
  "status": "ok",
  "result": {"pong": true}
}
```

---

## 目录

- `channel_common.py` - GitHub API 封装与协议解析
- `server_worker.py` - 服务端 Worker（轮询 + 执行 + 回写）
- `controller_cli.py` - 外部控制端 CLI（enqueue/wait/call）
- `tests/` - 单元测试

---

## 环境变量

```bash
export GITHUB_TOKEN=ghp_xxx
export CHANNEL_OWNER=yuwanyue
export CHANNEL_REPO=1
```

可选：

```bash
export CHANNEL_POLL_SECONDS=2
```

---

## 快速开始

### 1) 提交命令

```bash
python3 controller_cli.py enqueue ping --args '{}'
```

### 2) Server 处理一轮

```bash
python3 server_worker.py once
```

### 3) 等待响应

```bash
python3 controller_cli.py wait <request_id> --timeout 120
```

### 一步调用（推荐）

```bash
python3 controller_cli.py call ping --args '{}'
```

另一个终端跑 worker：

```bash
python3 server_worker.py loop --interval 3
```

### 受限端“仅 GitHub 出站”访问外网（HTTP 中转）

前提：仓库已存在工作流 `.github/workflows/egress-fetch.yml`（本仓库已内置）。

控制端入队：

```bash
python3 controller_cli.py enqueue egress.fetch --args '{"url":"https://example.com","method":"GET"}'
```

worker 执行后，响应里会包含：
- `status_code`
- `headers_preview`
- `body_preview`
- `body_b64_head`（body 前 32KB 的 base64）
- `local_output_dir`（本地解压目录）
- `local_archive_path`（本地保存的归档路径）

这样受限服务器无需直连目标站点，只需访问 GitHub API 与 Actions。

### 死信回放

重新把原 issue 放回队列：

```bash
python3 controller_cli.py requeue --issue 123
```

按原命令内容新建一次回放：

```bash
python3 controller_cli.py replay --issue 123
```

也可指定新的 request_id：

```bash
python3 controller_cli.py replay --issue 123 --request-id req_manual_replay_1
```

---

## 内置命令处理器

- `ping` -> 返回 pong
- `echo` -> 回显参数
- `system.info` -> 返回 hostname / utc_time
- `egress.fetch` -> 通过 GitHub Actions 工作流代发起 HTTP 请求（受限端仅访问 GitHub）

你可在 `server_worker.py` 中扩展 `CommandHandlers`。

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

也可以使用：

```bash
make test
```

---

## 当前实现补充

- Worker 领取任务时会先把 issue 从 `channel:pending` 切到 `channel:processing`
- 领取时会写入唯一 lease label，避免多个 worker 同时把同一条任务当成自己的
- 如果响应评论写入失败，worker 会把任务回滚到 `channel:pending + channel:retry`
- 如果评论已经存在，worker 会直接复用已有响应并收尾，避免重复执行
- 回滚时会累计 `channel:failures:N`，超过 `CHANNEL_MAX_FAILURES` 后进入 `channel:dead`
- 控制端重复提交同一个 `request_id` 时会复用已有命令 issue，不会重复创建任务
- `controller_cli.py requeue` 可把死信或失败任务重新放回队列
- `controller_cli.py replay` 可复制原命令为一条新任务，方便人工回放
- worker 每轮输出一条 JSON，包含 `seen/processed/replayed/retried/dead_lettered/errors`
- 仓库已通过 `.gitignore` 忽略 `out_*`、`__pycache__/` 等运行产物
- 已提供 `make clean-egress` 和 `scripts/clean-egress.sh` 清理本地历史产物
- egress 浏览器模式已增加 npm 与 Playwright 缓存，减少重复下载与安装

---

## 建议增强点

结合当前代码与仓库现状，下一步最值得做的增强点有这些：

### 1) 继续增强 egress 工作流缓存

当前 `.github/workflows/egress-fetch.yml` 在浏览器模式下会重复执行：

- `apt-get update`
- 字体安装
- `npm install playwright`
- `npx playwright install chromium`

建议：

- 使用 `actions/cache` 缓存 `~/.npm`
- 复用 Playwright 浏览器缓存目录
- 将浏览器依赖拆成更稳定的预热步骤或独立工作流

价值：

- 明显缩短 `screenshot/browser` 模式耗时
- 减少网络抖动导致的失败
- 降低 GitHub Actions 分钟消耗

### 2) 给远端 shell 命令增加安全边界

现在 `terminal_cmd_b64` 可以直接执行任意 `bash -lc` 命令，实战上很好用，但生产环境建议再收口。

建议：

- 增加命令白名单或模式白名单
- 对高风险命令做显式拦截，例如删除系统目录、改 SSH、改防火墙
- 增加最大执行时长、最大输出体积、最大归档体积限制
- 在响应里补充 `timed_out`、`truncated` 之类的状态字段

价值：

- 更适合团队共用
- 降低误操作和仓库凭据被滥用的风险
- 让失败原因更可观测

### 3) 增强结果归档与保留策略

当前已经实现了“先下载到本地，再自动清理 release/tag”，这是很实用的一步，但本地结果目录还可以进一步规范。

建议：

- 增加按日期或 request_id 的目录层级
- 增加保留天数与自动清理策略
- 给 `out_*` 增加索引文件，方便快速查找某次请求
- 明确哪些文件是核心结果，哪些是调试附件

价值：

- 更适合长期运行
- 本地结果更容易检索
- 降低磁盘占用失控的概率

### 4) 补一份“网络路由策略”说明

现在仓库已经实际承担了“非 GitHub 外网访问统一经仓库中转”的角色，但 README 里还没有把这条策略单独讲清楚。

建议：

- 增加一节“网络访问约定”
- 明确 GitHub 域名可直连，其他外网建议走 egress workflow
- 给出 `curl`、`wget`、`apt`、`npm`、截图这几类常见示例

价值：

- 团队成员更容易理解什么时候该直连，什么时候该走中转
- 能减少重复沟通和误用

### 5) 增加更贴近真实场景的测试

当前单元测试已经覆盖了协议解析、回放、死信、egress 基本流程，这很好。下一步更推荐补：

- `cleanup_release=false` 的测试
- 本地输出目录结构测试
- release/tag 删除失败但主流程成功的测试
- `terminal_cmd`、`browser_script`、大 body 返回的边界测试

价值：

- 让这套通道在持续演进时更稳
- 降低“功能能跑但边界条件出错”的概率

### 6) 增加更完整的运维入口

建议补一个轻量入口，例如：

- `Makefile`
- `scripts/dev.sh`
- `scripts/run_worker_once.sh`

把这些常用动作封起来：

- 跑测试
- 单次 worker 处理
- 发起 egress 请求
- 清理本地产物
- 查看最近一次 egress 结果
- 一键检查必要环境变量

价值：

- 新接手的人更快上手
- 日常排障命令更统一

---

## 运维入口

仓库现在已经内置了一组轻量入口：

```bash
make test
make worker-once
make worker-loop
make clean-egress
```

说明：

- `make test`：跑单元测试
- `make worker-once`：处理当前待执行命令一次
- `make worker-loop`：持续轮询处理命令
- `make clean-egress`：清理本地 `out_*` 结果目录和 Python 缓存

建议：

- 定期执行 `make clean-egress`
- 仅在确实需要排障时保留 `out_*` 目录

如需通过脚本直接清理，也可以执行：

```bash
bash ./scripts/clean-egress.sh
```

---

## 注意

- Issue 队列天然是**至少一次投递**语义，处理器应继续保持幂等。
- lease 基于 GitHub issue labels 做“最后写入者获胜”的乐观锁，已经比直接轮询安全很多，但仍不等于数据库事务锁。
- `CHANNEL_MAX_FAILURES` 默认值是 `3`。
- 生产可继续加：签名、白名单命令、分片标签、死信人工回放、响应完整性校验。
