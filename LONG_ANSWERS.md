# Long Telegram answers

The default `XDSH_TELEGRAM_MAX_ANSWER_CHARS` is now **200000** (formerly 60000).
This is a character buffer/display limit, not a model token limit or task runtime
budget. Existing explicitly configured values remain authoritative. Supported
values are 1000–200000; unlimited buffering is not implemented.

Terminal Markdown is split into durable parts no larger than 16000 characters,
then split by the Telegram transport. Strict plain/JSON responses use smaller
1900-code-point parts, safe even for supplementary Unicode characters. Joining
the ordered JSON parts reconstructs the document; each part is not necessarily
a standalone JSON document. The durable queue accepts at most 256 parts per
reply, enough for the supported answer/reasoning buffers and notices.

Large replies can still take time to send and remain subject to Telegram rate
limits. For very large reports, ask the agent to write a complete workspace
artifact and return a concise summary. Automatic full-answer file spillover is
not implemented by this change. A truncation notice still appears if the chosen
buffer is exceeded. Already-sent truncated messages are not repaired by raising
the limit; recovery requires inspecting retained native logs/artifacts.

简体中文：默认答案上限已从 6 万提高到 20 万字符；没有取消所有保护，也不改变
模型生成上限。配置文件若显式填写旧值，应改为 `XDSH_TELEGRAM_MAX_ANSWER_CHARS=200000`
后在空闲时重启。更大的报告建议保存完整文件，再发送摘要，避免大量消息刷屏。
