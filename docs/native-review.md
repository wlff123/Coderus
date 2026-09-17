# 原生 Codex 检视与结果适配

PR 检视使用两个独立的 Codex 调用：

1. `codex exec --json ... review` 执行原生静态检视。开发者指令指定固定的
   Git 比较基准、检查范围和安全边界；此阶段不传 Coderus 的输出 Schema。
2. `codex exec --json ... --output-schema ... -` 将原生最终结论转换为现有
   `ReviewOutput`。原文通过标准输入传递，按不可信数据处理；此阶段不能调用工具。

两阶段共用任务超时、取消信号、代理凭据及总输出额度。Linux 上继续使用原有
Landlock 隔离，禁止修改或执行检视工作区内容。

## 发布条件

- 原生阶段必须成功完成，并保留成功读取固定基准 Git diff 的执行证据。
- 适配器按原生输出中的意见标题建立清单，逐条核对转换后的优先级、标题、
  文件、行号和 LEFT/RIGHT，不允许丢失、合并或新增意见。
- 无意见时，原生阶段必须明确输出完成标记
  `CODERUS_REVIEW_COMPLETE_NO_FINDINGS`；该内部标记不会发布到 PR。
- 转换结果仍需通过原有 Schema、路径、中文字段及变更行范围校验。
- 原生或转换失败、输出截断、结论不明确时不能发布空意见或 `/lgtm`。
- PR 评论渲染、飞书通知及 `/lgtm` 规则不变；审计模式记录为
  `native_review_with_schema_adapter`。

适配器不再依靠自然语言“问题/影响/建议”段落的排版拆分完整结果，但仍需识别
Codex 原生渲染的意见标题和位置。遇到未知格式时保守失败，不能据此认定没有问题。
已用 Codex CLI 0.154.0 验证；升级 CLI 时应再次验证单意见、多意见及无意见三种情况。
额外的格式转换会增加一次模型调用和相应延迟、用量。

相关回归测试位于 `tests/runner/test_review_adapter.py`、
`tests/runner/test_local.py` 和 `tests/pr_review/`。
