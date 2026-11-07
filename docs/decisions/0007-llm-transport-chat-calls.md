# 0007 对话调用走 llm_transport；流式输出、embedding 和 Agent 仍走 SDK

- 状态：已实现，评审通过并合入 `integration/rebuild-2026-11`（2026-11-07；原分支 `r2/fabric-transport`）。
- 日期：2026-11-07

## 决定

- `core_engine/cloud_api_client.py` 的两个非流式对话调用，HTTP 一侧换成共享的 `llm_transport.send`：
  `qwen_extract_structured`（`stream=False`，抽取流程唯一的用法）和 `qwen_chat`（消歧 LLM 兜底）。函数签名、返回值、
  失败时返回 `{}` / `""`、日志文字都不变。
- SDK 原来替这两个调用做的事照旧。`tests/test_cloud_api_client.py` 先在 SDK 上通过，切换后原样通过：
  - 请求体逐字段相同。不用 `build_body`，因为它会加 `max_tokens`，并把温度定为 0。
  - 每次调用新建客户端，跟随重定向。
  - 读写超时 600 秒，连接超时 5 秒。
  - 超时、连接失败、408/409/429/5xx 之后最多再发两次，先等 0.5 秒，再等 1 秒。
  - 失败时异常文字与 SDK 相同：`Error code: 503 - {...}`、`Connection error.`、`Request timed out.`。
- 重发由 Fabric 自己的循环做，每次是一个新的 `send`，不用 `Resend`。原因：`Resend` 不会在超时或连接失败之后重发，SDK 会。

## 有意不保留的

- SDK 的等待抖动（每次最多缩短四分之一）。
- SDK 读 `Retry-After`（0–60 秒时按它等）和 `x-should-retry` 这两个响应头；`Attempt` 不带响应头，所以不读。
- SDK 附加的请求头：`User-Agent: OpenAI/Python …`、`X-Stainless-*`、幂等键。
- 模型名为空，或 base URL 不是带主机的 http(s) 地址时，现在在本地就失败（`Provider` 拒绝），返回值照旧是 `{}` / `""`。
  SDK 在这种情况下会先把请求发出去，或改用 `OPENAI_BASE_URL`。
- 返回 200 但不是对话补全（不是 JSON，或没有 choices）时，返回值不变，日志文字不同。

## 仍走 SDK 的，以及原因

- `qwen_extract_structured(stream=True)`：逐个 token 打印到终端，而 transport 只发非流式请求。代码里没有调用方传
  `stream=True`，它留作命令行调试。
- `generate_embedding`：调用 `/embeddings` 端点，而 transport 只发 `/chat/completions`。
- `core_engine/agents/` 的 5 个 Agent（entity_merge、relation_review、entity_review、semantic_matcher、
  conflict_resolver）：
  - 它们不自己建 SDK 客户端，用的是 LangChain 的 `ChatOpenAI` 加 `bind_tools`，在 LangGraph 里跑工具调用循环。
  - 换到 transport 要自己写一个 LangChain 聊天模型，包括消息转换、带 tools 的请求体、tool_calls 解析。这是新的设计层，
    本轮不做。
- 所以 `openai`（`cloud_api_client` 直接用）和 `langchain-openai` 仍是依赖，`requirements*.txt` 和锁文件都不变。
