# 更新日志

本项目的重要变更记录在此文件中。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

## [0.1.1] - 2026-07-22

### 修复

- 读取问题链接时，在单链接评论总预算内抓取已选高赞回答的评论，而不再只读取问题本身的评论。
- 默认排序没有返回评论但知乎仍报告存在评论时，自动改按最新排序重试一次。
- 按 `paging.next` 中的不透明游标读取根评论和子评论后续页，避免数字 offset 导致重复首屏或漏读。
- 配置 Cookie 导致公开评论接口返回 401/403 时，自动清空 Cookie 匿名重试一次。
- 评论接口被拒绝、限流或返回异常结构时保留已抓取的正文与部分评论，并向大模型提供明确的不可用原因。
- 不再把评论读取失败的结果缓存为正常的“无评论”内容。

## [0.1.0] - 2026-07-22

### 新增

- 读取知乎专栏文章、问题、回答及其评论。
- 自动识别消息中的知乎链接，并提供 `/知乎阅读`、`/知乎读取` 和 `/zhihu_read` 命令。
- 将抓取内容临时注入当前大模型请求，使总结能够保留在会话中，而原始资料不写入会话历史。
- 支持配置知乎 Cookie、抓取超时、内存缓存、评论数量以及正文和单轮注入字符上限。
- 为链接解析、内容清洗、评论抓取和 AstrBot 事件处理提供单元测试。

### 安全

- 将知乎内容标记为外部不可信资料，降低其中提示词影响模型指令的风险。
- 避免在日志和公开仓库中记录可选的知乎 Cookie。

[未发布]: https://github.com/insolitude610/astrbot_plugin_zhihu_reader/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/insolitude610/astrbot_plugin_zhihu_reader/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/insolitude610/astrbot_plugin_zhihu_reader/releases/tag/v0.1.0
