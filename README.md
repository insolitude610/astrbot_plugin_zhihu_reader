# 知乎阅读器

让 AstrBot 读取知乎文章、想法、问题、回答和评论，并把内容安全地提供给当前大模型请求。

仓库：<https://github.com/insolitude610/astrbot_plugin_zhihu_reader>

## 功能

- 在普通消息中识别知乎链接，自动抓取正文与评论。
- 使用 `/知乎阅读 <url>` 抓取后由当前会话的大模型总结；`/知乎读取` 和 `/zhihu_read` 是别名。
- 读取知乎“想法”的文本、图片/视频占位、链接、转发来源和评论。
- 读取问题链接时，在总评论预算内同时抓取问题评论和已选高赞回答的评论。
- 对单篇正文、单轮注入、评论数量和链接数量分别设限。
- 支持可选知乎 Cookie、请求超时和内存缓存。

自动读取和命令读取的资料都会通过 AstrBot 的临时内容标记加入本轮请求，不写入会话历史。插件不会调用独立的总结提供商，而是让当前会话中带人设的大模型只生成一次回复；这条 assistant 总结由 AstrBot 正常保存，可在 WebUI 对话数据中查看并继续追问。插件不注册返回原文的 LLM 工具，避免工具结果被持久化进会话。所有知乎内容都被明确标记为外部不可信资料，正文或评论中的提示词不会被当作系统指令。

## 安装与调试

插件目录应位于 AstrBot 实例的 `data/plugins/astrbot_plugin_zhihu_reader/`，至少包含 `main.py`、`reader.py`、`metadata.yaml`、`_conf_schema.json` 和 `requirements.txt`。安装依赖并在 WebUI 的插件管理页重载插件后即可使用。

支持的链接由 `reader.py` 决定，预期包括：

- `https://zhuanlan.zhihu.com/p/<文章ID>`
- `https://www.zhihu.com/pin/<想法ID>`（兼容 `/appview/pin/` 与 `/mobile/pin/`）
- `https://www.zhihu.com/question/<问题ID>`
- `https://www.zhihu.com/question/<问题ID>/answer/<回答ID>`

## 配置

配置在 AstrBot WebUI 的插件设置中管理：

- `auto_inject`：是否自动处理普通消息中的知乎链接。
- `cookie`：可选登录 Cookie。它是敏感凭据，不应写入日志或公开仓库。
- `include_comments` / `max_comments`：评论开关与单链接评论总数上限。
- `max_content_chars`：每个链接输出的正文与评论字符预算，默认 `8000`。
- `max_inject_chars`：单轮发送给模型的全部知乎资料硬上限，默认 `12000`。
- `timeout_seconds`：网络请求超时。
- `cache_ttl_seconds`：相同链接的缓存有效期，`0` 表示关闭缓存。
- `max_urls`：单轮自动读取的链接数量上限，默认 `1`。

知乎接口可能要求登录、触发风控或随站点更新而变化。评论请求因 Cookie 被拒绝时，插件会对公开评论自动进行一次匿名重试。评论仍然失败时，正文会继续交给大模型，资料中会包含不可用原因，且失败结果不会作为正常内容缓存。可先用 `/知乎阅读 <url>` 检查当前链接和 Cookie 是否可用。
