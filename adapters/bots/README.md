# 双机器人适配（EXTERNAL_REQUIRED）

实现官方长连接 SDK：飞书 `@larksuiteoapi/node-sdk`，企微 `@wecom/aibot-node-sdk`。
参考官方文档（2026-10-24读取）：
- https://github.com/larksuite/node-sdk （WSClient / EventDispatcher / im.message.create）
- https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-
- https://github.com/WecomTeam/aibot-node-sdk （WSClient / message.text / sendMessage）

在本目录用 `npm ci` 按lock安装官方依赖（核心Python CLI无需Node），运行 `node index.mjs /本人私密配置.json feishu` 或 wecom。
从开始菜单的“机器人私密设置”隐藏输入并创建权限600配置；高级模式也可在包外自行创建，勿提交Git、发聊天或写普通日志：
```
{"enabled":false,"python":"/path/to/python3","bridge":"/path/to/bot_bridge.py","root":"/path/to/发行包","owners":{"feishu":{"本人open_id":"owner-local"},"wecom":{"本人userid":"owner-local"}},"feishu":{"appId":"本人填写","appSecret":"<TOKEN>"},"wecom":{"botId":"本人填写","secret":"<TOKEN>"}}
```
将 enabled 改 true 是本人实际接入步骤，本轮不做。需要新应用、事件订阅/接收消息与发送权限；自行按官方后台配置，不改现有机器人。飞书自建应用开启长连接订阅 im.message.receive_v1，身份用 open_id；企微后台获取智能机器人长连接 botId/secret，身份用 userid。默认只给本人身份，不能给整个群放行。

自然语言支持“查看项目”“选择项目 id”“下一步”“查询任务 task-id”；仅对白名单本人直接请求答复，不视作主动进度通知。实施须“请实施：”开头并分行填写用户/问题/核心流程/首版范围/验收；未绑定执行身份只保存MVP，不派工。其他普通消息进入可续接需求访谈；明确实施意图后由后台通过同一正式战队进行只读审阅，再在独立候选中派工。本人可发“批准 批准id”，绑定预先存在的完整上下文，无需手写JSON；仍支持明确JSON `{"command":"approve","id":"批准id","binding":{完整六字段}}` 才消费预先存在的批准，task/action/target/scope/version/owner完全匹配、24小时内且跨渠道一次消费。批准消费不自动执行外部高风险动作。

发送仅接受本地 stdin 每行 JSON：id/kind/target/text，kind 仅 completed/approval/blocked/scope_changed，target 必须本人白名单。先持久化发送标记，超时结果未知不自动重试。不自动发欢迎或回声；消息内容应由调用者先脱敏。

测试包括实际官方SDK加载/构造（无连接）、SDK注入模拟与本地Python持久化集成，不代表平台登录/消息真实发送已验证。需要本人绑定后单独验收；本轮零真实外部消息。
