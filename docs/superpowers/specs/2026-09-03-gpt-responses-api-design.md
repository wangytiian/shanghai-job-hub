# GPT Responses API 支持设计

## 目标

让“GPT / OpenAI 兼容中转 API”可在普通 Chat Completions 和 Responses API 两种协议之间明确选择；连接测试必须验证可解析的模型文本，而非仅验证 HTTP 200。

## 已确认的上下文

当前 xfx.plus 配置使用 `wire_api = "responses"`。系统却向 `/chat/completions` 发送请求；该地址返回 HTML 首页，导致预填阶段解析 JSON 失败。用户已授权按此方案调整并以其 Terra 配置实测。

## 方案

在 GPT 配置中增加“接口模式”下拉框：`chat_completions`（默认，Base URL 通常填写到 `/v1`）和 `responses`（Base URL 填服务商给出的根地址）。该设置保存至本地数据库，旧设置默认迁移为 `chat_completions`，不影响已配置的普通中转。

客户端按模式构建请求：Chat 使用 `/chat/completions` 并读取 `choices[0].message.content`；Responses 使用 `/responses`、`instructions` 和 `input`，优先读取 `output_text`，必要时从 `output[].content[].text` 读取。两种模式都要求返回非空文本。

连接测试复用一次极短的真实模型文本请求（`OK`），因此只有模型请求和输出解析均成功才展示“测试成功”。失败时保存脱敏错误摘要，页面不显示 Key。

## 边界与验收

- API Key 继续只存 Windows 凭据管理器，不能进入数据库、页面、URL、日志反馈。
- 仅支持这两种协议；不猜测服务商的私有接口。
- 自动化测试覆盖模式保存、Responses 请求/解析、空输出拒绝和 UI 模式选项。
- 最终用当前 `gpt-5.6-terra`、`https://xfx.plus` 的 Responses 模式进行一次真实测试；若上游仍不兼容，如实说明响应状态和正确的服务商地址要求。
