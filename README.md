# astrbot_plugin_hide_thinking

隐藏模型思考内容、结束符、垃圾标签（如 `</div>`）和泄漏的工具调用，并折叠整段复读。QQ / 群聊只发正文。

## 挡什么

AstrBot 会把接口返回的 `reasoning_content` 挂到事件 extra，发送前拼成：

```text
🤔 思考: ……

────
正文
```

Grok / DeepSeek 等模型还会在正文里塞 `<think>…</think>`，以及漏出来的结束符 `<|eos|>`。

本插件做七件事：

1. 清掉 `reasoning_content` 和 `_llm_reasoning_content`，发送前不再拼接思考
2. 删掉 `<think>` / `<thinking>` / `<reasoning>` 等标签及内部内容
3. 如果思考已经被拼进正文开头（`🤔 思考:`），整段去掉
4. 删掉漏进正文的结束符：`<|eos|>` `<|im_end|>` `</s>` 等
5. 同一句话被粘两遍时只留一句；工具先发一遍、正文又发一遍（或正文把前一条接在尾巴上）时只留一条（空格不同也算同一句）。同一条出口重复说同样的话不会被吞
6. 模型把 `<tool_call>` / `send_message_to_user` 当正文发出时，抽出里面的人话，外壳丢掉
7. 删掉混进正文的垃圾标签（默认 `</div>`；要删别的可在配置里加，如 `</p>`）

不改模型请求，不影响历史里的签名字段。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| strip_tags | true | 删思维链标签 |
| strip_injected | true | 删已拼进正文的「🤔 思考:」块 |
| strip_special | true | 删 `<|eos|>` 等结束符 |
| collapse_duplicate | true | 整段复读只留一句；跨出口重复（工具 vs 正文）也剪（忽略空格）；同一条出口的重复会保留 |
| strip_tool_call | true | 抽出工具调用里的人话，丢掉 XML 外壳 |
| strip_junk_tags | true | 删垃圾标签（默认 `</div>`，出现即删） |
| junk_tags | `["</div>"]` | 要删除的垃圾标签列表；大小写不敏感，标签内空白也识别（`</ div >`）；非标签内容按字面量删 |
| dedup_window | 6 | 工具消息的等待窗口（秒）：工具主动发的纯文本先缓冲，等正文来替代；正常回复不缓冲、不延迟；0 关闭缓冲 |
| dedup_prefer | second | 同句重复时保留哪条：second=第二遍（默认，留正文空格版）/ first=第一遍 / spaced=空格更多的那条 |

## 为什么会发两遍

模型有时会中途调 `send_message_to_user` 工具发一遍消息（走 `Context.send_message`），结尾又把同样的话当正文发一遍。AstrBot 自带的防重复是逐字精确比对，两种写法空格不同就当成两条，全发出去。

本插件把 `Context.send_message` 和 `event.send` 两条路都接管进同一个去重缓冲：同句不同空格只发空格更自然的那条。

去重只跨这两条路生效——同一条出口连续说两次同样的话（群里两个人都问同一个问题、机器人重复口头禅）会照常发出去，不会被吞掉。

## 安装

插件市场填仓库地址，或把本目录放到 `data/plugins/` 后重载：

```text
https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking
```

## 说明

- 非流式（AstrBot 默认）即可挡住 QQ 里的思考
- 流式 / 插件直发也会在真正 send 前再剥一层结束符和思考标签；思考标签被切片切断时不会漏出来
- 开启去重窗口后，只有工具主动发送的纯文本会缓冲最多 N 秒（等正文来替代）；正常回复立即发送，不延迟
- 飞书折叠思考卡片一并丢掉
