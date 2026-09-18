# astrbot_plugin_hide_thinking

隐藏模型思考内容。QQ / 群聊只发正文，不把思维链露出去。

## 挡什么

AstrBot 会把接口返回的 `reasoning_content` 挂到事件 extra，发送前拼成：

```text
🤔 思考: ……

────
正文
```

Grok / DeepSeek 等模型还会在正文里塞 `<think>…</think>`。

本插件做三件事：

1. 清掉 `reasoning_content` 和 `_llm_reasoning_content`，发送前不再拼接思考
2. 删掉 `<think>` / `<thinking>` / `<reasoning>` 等标签及内部内容
3. 如果思考已经被拼进正文开头（`🤔 思考:`），整段去掉

不改模型请求，不影响历史里的签名字段。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| strip_tags | true | 删思维链标签 |
| strip_injected | true | 删已拼进正文的「🤔 思考:」块 |

## 安装

插件市场填仓库地址，或把本目录放到 `data/plugins/` 后重载：

```text
https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking
```

## 说明

- 非流式（AstrBot 默认）即可挡住 QQ 里的思考
- 若同时开了「流式输出」和「显示思考内容」，思考 delta 会直接发出，插件拦不住；把面板里的「显示思考内容」关掉即可
- 飞书折叠思考卡片一并丢掉
