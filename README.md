# astrbot_plugin_hide_thinking

隐藏模型思考内容和结束符，并折叠整段复读。QQ / 群聊只发正文。

## 挡什么

AstrBot 会把接口返回的 `reasoning_content` 挂到事件 extra，发送前拼成：

```text
🤔 思考: ……

────
正文
```

Grok / DeepSeek 等模型还会在正文里塞 `<think>…</think>`，以及漏出来的结束符 `<|eos|>`。

本插件做五件事：

1. 清掉 `reasoning_content` 和 `_llm_reasoning_content`，发送前不再拼接思考
2. 删掉 `<think>` / `<thinking>` / `<reasoning>` 等标签及内部内容
3. 如果思考已经被拼进正文开头（`🤔 思考:`），整段去掉
4. 删掉漏进正文的结束符：`<|eos|>` `<|im_end|>` `</s>` 等
5. 同一句话被粘两遍时只留一句；分段发出后，后一条把前一条接在尾巴上也剪掉（空格不同也算同一句）

不改模型请求，不影响历史里的签名字段。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| strip_tags | true | 删思维链标签 |
| strip_injected | true | 删已拼进正文的「🤔 思考:」块 |
| strip_special | true | 删 `<|eos|>` 等结束符 |
| collapse_duplicate | true | 整段复读只留一句，跨条尾巴重复也剪（忽略空格） |

## 安装

插件市场填仓库地址，或把本目录放到 `data/plugins/` 后重载：

```text
https://github.com/Zxin-Pro/astrbot_plugin_hide_thinking
```

## 说明

- 非流式（AstrBot 默认）即可挡住 QQ 里的思考
- 流式 / 插件直发也会在真正 send 前再剥一层结束符和思考标签
- 飞书折叠思考卡片一并丢掉
