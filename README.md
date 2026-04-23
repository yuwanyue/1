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

---

## 内置命令处理器

- `ping` -> 返回 pong
- `echo` -> 回显参数
- `system.info` -> 返回 hostname / utc_time

你可在 `server_worker.py` 中扩展 `CommandHandlers`。

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

---

## 注意

- Issue 队列天然是**至少一次投递**语义，处理器建议保持幂等。
- 生产可加：签名、白名单命令、分片标签、死信标签、重试计数。
