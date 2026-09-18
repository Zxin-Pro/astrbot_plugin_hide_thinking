# astrbot_plugin_hide_thinking

隐藏模型思考内容、结束符和泄漏的工具调用，并折叠整段复读。QQ / 群聊只发正文。

## 挡什么

AstrBot 会把接口返回的 `reasoning_content` 挂到事件 extra，发送前拼成：

```text
🤔 思考: ……

────
正文
```

Grok / DeepSeek 等模型还会在正文里塞 `<think>…</think>`，以及漏出来的结束符 `<|eos|>`。

本插件做六件事：

1. 清掉 `reasoning_content` 和 `_llm_reasoning_content`，发送前不再拼接思考
2. 删掉 `<think>` / `<thinking>` / `<reasoning>` 等标签及内部内容
3. 如果思考已经被拼进正文开头（`🤔 思考:`），整段去掉
4. 删掉漏进正文的结束符：`<|eos|>` `<|im_end|>` `</s>` 等
5. 同一句话被粘两遍时只留一句；分段发出后，后一条把前一条接在尾巴上也剪掉（空格不同也算同一句）
6. 模型把 `<tool_call>` / `send_message_to_user` 当正文发出时，抽出里面的人话，外壳丢掉

不改模型请求，不影响历史里的签名字段。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| strip_tags | true | 删思维链标签 |
| strip_injected | true | 删已拼进正文的「🤔 思考:」块 |
| strip_special | true | 删 `<|eos|>` 等结束符 |
| collapse_duplicate | true | 整段复读只留一句，跨条尾巴重复也剪（忽略空格） |
| strip_tool_call | true | 抽出工具调用里的人话，丢掉 XML 外壳 |
| dedup_window | 6 | 去重等待窗口（秒）：合并同句不同写法的重复发送；正常回复在本轮发送结束时立刻冲刷，不被拖慢；0 关闭缓冲 |
| dedup_prefer | second | 同句重复时保留哪条：second=第二遍（默认，留正文空格版）/ first=第一遍 / spaced=空格更多的那条 |

## 为什么会发两遍

模型有时会中途调 `send_message_to_user` 工具发一遍消息（走 `Context.send_message`），结尾又把同样的话当正文发一遍。AstrBot 自带的防重复是逐字精确比对，两种写法空格不同就当成两条，全发出去。

本插件把 `Context.send_message` 和 `event.send` 两条路都接管进同一个去重缓冲：同句不同空格只发空格更自然的那条。

## 安装

插件市场填仓库地址，或把本目录放到 `data/plugins/` 后重载：

```text
https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking
```

## 说明

- 非流式（AstrBot 默认）即可挡住 QQ 里的思考
- 流式 / 插件直发也会在真正 send 前再剥一层结束符和思考标签
- 开启去重窗口后，纯文本消息会缓冲最多 N 秒再发（等待可能更自然的空格版本），最后一条回复略有延迟
- 飞书折叠思考卡片一并丢掉
