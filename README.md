# 知乎阅读器

让 AstrBot 读取知乎文章、想法、问题、回答和评论，并把内容安全地提供给当前大模型请求。

仓库：<https://github.com/insolitude610/astrbot_plugin_zhihu_reader>

## 功能

- 在普通消息中识别知乎链接，自动抓取正文与评论。
- 支持纯文本、Markdown/HTML 包装链接，以及引用消息中的知乎链接。
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
- `cookie`：可选登录 Cookie。请从能打开目标内容的浏览器请求中复制完整 Cookie；它是敏感凭据，不应写入日志或公开仓库。
- `authenticated_article_fallback`：是否在文章 API 被拒绝或只返回预览时，使用已登录的知乎文章页面回退。默认关闭。
- `include_comments` / `max_comments`：评论开关与单链接评论总数上限。
- `max_content_chars`：每个链接输出的正文与评论字符预算，默认 `8000`。
- `max_inject_chars`：单轮发送给模型的全部知乎资料硬上限，默认 `12000`。
- `timeout_seconds`：网络请求超时。
- `cache_ttl_seconds`：相同链接的缓存有效期，`0` 表示关闭缓存。
- `max_urls`：单轮自动读取的链接数量上限，默认 `1`。

## 登录态与付费文章

`authenticated_article_fallback` 默认关闭。启用后，插件会先请求知乎文章 API；API 被拒绝或只返回预览时，才使用已配置的 Cookie 请求固定的知乎专栏文章页面，并从页面结构化数据中读取正文。

启用步骤：

1. 在能够正常打开目标文章的浏览器中，从该文章的网络请求复制完整 Cookie。不要只填写单个静态 Token 或某一个 Cookie 字段。
2. 将完整 Cookie 填入插件的 `cookie` 配置，并开启 `authenticated_article_fallback`。
3. 保存配置并重载插件，再用 `/知乎阅读 <url>` 检查读取结果。

> [!WARNING]
> 登录态回退使用的是 Bot 所配置账号的阅读权限。任何能够向 Bot 发送知乎链接的人，都可能间接读取该账号有权访问的文章。不要在触发范围不受信任的公开 Bot 上开启此功能；应限制可用用户或群聊，并使用权限范围合适的知乎账号。

还需要注意：

- Cookie 是完整账号凭据，不能写入日志、截图、聊天记录或公开仓库。怀疑泄露时应立即退出相关登录会话并更新插件配置。
- 配置 Cookie 不等于一定能读取付费文章。知乎仍可能要求 `zh-zse-ck` 等交互式浏览器验证；插件不会执行验证脚本、逆向或生成 `x-zse-*` 动态签名，也不会绕过站点风控。
- 登录态页面回退只访问固定的 `https://zhuanlan.zhihu.com/p/<数字ID>`，不跟随重定向，并继续执行请求超时和响应大小限制。
- 仅获取到预览、购买提示或明确不可读状态时，插件会标记为预览且不写入正常缓存，不会把它误报为完整正文。
- 成功读取且满足正常缓存条件的完整结果，会按照 `cache_ttl_seconds` 暂存在 Bot 进程内存中；预览或评论抓取不完整的结果不会这样缓存。共享 Bot 如不希望后续请求复用登录态结果，可将该值设为 `0`。
- 正文与评论使用不同接口。正文读取成功不代表评论一定可用；登录 Cookie 被评论接口拒绝时，插件会对公开评论匿名重试一次，仍失败则保留正文并附带评论不可用原因。

知乎接口可能随站点更新而变化。建议先用 `/知乎阅读 <url>` 检查当前链接、Cookie 和回退设置，再决定是否在普通消息中自动处理该链接。

## 兼容性说明

- 插件本身不向消息平台发送知乎图片或视频；这些内容会转换为文本占位符，因此不依赖平台的媒体上传接口。
- `requirements.txt` 显式声明了 `socksio`；使用 SOCKS 代理时，HTTPX 会按运行环境的代理设置连接知乎。
- Local Agent Runner 的流式和非流式请求都支持，平台是否真正流式发送由 AstrBot 适配器决定。不支持流式的平台建议使用 `turn_off`，让 AstrBot 在平台侧发送最终结果。
- 当前 AstrBot 的 Dify、Coze、DashScope、DeerFlow 等第三方 Agent Runner 不会读取插件的临时内容段，建议使用 Local Agent Runner；否则知乎资料可能不会进入模型。
- Discord 原生斜杠命令和 KOOK 流式输出属于 AstrBot 核心适配器限制，推荐使用普通消息/唤醒方式和非流式模式。
- 自动读取会检查当前提示词、消息文本和引用文本；平台把链接放在未转换的卡片或 JSON 字段中时，仍可能无法识别。
