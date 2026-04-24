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
- 仓库已通过 `.gitignore` 忽略 `out_*`、`egress_archive/`、`__pycache__/` 等运行产物
- 已提供 `make clean-egress` 和 `scripts/clean-egress.sh` 清理本地历史产物与结构化归档目录
- egress 浏览器模式已增加 npm 与 Playwright 缓存，减少重复下载与安装
- `egress.fetch` 的本地归档已按日期与 request_id/run_id 结构化保存，并追加索引文件
- `terminal_cmd` 默认带有一层危险命令拦截，明显高风险命令会直接拒绝执行
- 远端 shell 执行已增加超时保护，避免 GitHub runner 无限挂起

---

## 网络访问约定

建议把这套仓库当成统一的外网中转入口来使用：

- GitHub 相关域名可以直接访问
- 非 GitHub 外网域名、地址和公网下载动作，优先走 `egress.fetch`
- `curl`、`wget`、`apt`、`npm`、浏览器截图、页面抓取等任务，都适合通过 GitHub Actions 中转再回传本地结果

常见例子：

```bash
python3 controller_cli.py enqueue egress.fetch --args '{"url":"https://example.com","method":"GET"}'
```

```bash
python3 controller_cli.py enqueue egress.fetch --args '{"url":"https://github.com","mode":"shell","terminal_cmd":"mkdir -p out/curl && curl -L https://l2.io/ip > out/curl/ip.txt"}'
```

```bash
python3 controller_cli.py enqueue egress.fetch --args '{"url":"https://github.com","mode":"shell","terminal_cmd":"mkdir -p out/pkg && cd out/pkg && sudo apt update && apt-get download ripgrep"}'
```

如果确实需要执行高风险远端命令，建议显式评估后再通过环境变量放开，而不是默认裸跑。

---

## 真人化浏览器模板

仓库现在内置了一份可复用的“更像真人的浏览器访问”模板，适合搜索页、输入框和简单交互场景。

模板文件：

- `templates/browser-human-search.js.tmpl`

渲染工具：

- `scripts/render_browser_template.py`

快速生成脚本：

```bash
python3 ./scripts/render_browser_template.py \
  --start-url "https://www.baidu.com" \
  --search-term "周树人"
```

也可以使用 Make 入口：

```bash
make human-browser-template START_URL="https://www.baidu.com" SEARCH_TERM="周树人"
```

如果想把生成后的脚本直接嵌进 `egress.fetch` 的 `browser_script`，可先输出为 JSON 字符串：

```bash
python3 ./scripts/render_browser_template.py \
  --start-url "https://www.baidu.com" \
  --search-term "周树人" \
  --json
```

模板默认包含这些“更像真人”的动作：

- 打开页面后停顿
- 鼠标移动
- 聚焦输入框
- 逐字输入关键词
- 回车触发搜索
- 等待结果页加载
- 轻微滚动页面

注意：

- 这是通用模板，不保证所有站点都兼容
- 百度、Google、部分电商和社交网站仍可能触发风控
- 遇到复杂站点时，建议从这个模板出发，再按页面结构微调 selector 和等待时间

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

### 2) 继续增强结果归档与保留策略

当前已经实现了“先下载到本地，再自动清理 release/tag”，并且本地归档已经具备日期目录与索引文件。

建议：

- 增加保留天数与自动清理策略
- 明确哪些文件是核心结果，哪些是调试附件
- 给索引增加更多字段，例如耗时、状态码、是否超时

价值：

- 更适合长期运行
- 本地结果更容易检索
- 降低磁盘占用失控的概率

### 3) 增加更贴近真实场景的测试

当前单元测试已经覆盖了协议解析、回放、死信、egress 基本流程，这很好。下一步更推荐补：

- `cleanup_release=false` 的测试
- 本地输出目录结构测试
- release/tag 删除失败但主流程成功的测试
- `terminal_cmd`、`browser_script`、大 body 返回的边界测试

价值：

- 让这套通道在持续演进时更稳
- 降低“功能能跑但边界条件出错”的概率

### 4) 增加更完整的运维入口

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
- `make recent-egress`：查看最近的 egress 本地归档索引
- `make env-check`：快速检查关键环境变量是否已设置

建议：

- 定期执行 `make clean-egress`
- 仅在确实需要排障时保留 `out_*` 或 `egress_archive/` 目录

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
