STATUS_META = {
    "queued": ("排队中", "neutral"),
    "preparing": ("准备工作区", "active"),
    "developer_working": ("开发分析与修复", "active"),
    "reviewing": ("双 Reviewer 检视", "active"),
    "developer_revising": ("开发集中修正", "active"),
    "sealing": ("确认提交内容", "active"),
    "publishing": ("发布 PR", "active"),
    "awaiting_human_review": ("等待人工审核", "warning"),
    "completed": ("已完成", "ok"),
    "closed": ("PR 已关闭", "neutral"),
    "dismissed": ("已关闭", "neutral"),
    "failed": ("失败", "danger"),
    "cancelled": ("已取消", "neutral"),
    "cancelling": ("正在取消", "warning"),
    "manual_intervention": ("需要人工处理", "danger"),
    "discovered": ("待处理", "neutral"),
    "dispatched": ("已派发", "active"),
    "ignored": ("已忽略", "neutral"),
    "idle": ("未同步", "neutral"),
    "running": ("执行中", "active"),
    "succeeded": ("成功", "ok"),
    "timed_out": ("超时", "danger"),
    "approve": ("通过", "ok"),
    "changes_requested": ("建议修改", "warning"),
    "local": ("本地访问", "neutral"),
    "public": ("公网访问", "warning"),
}

PR_REVIEW_STATUS_META = {
    "queued": ("排队中", "warning"),
    "preparing": ("准备代码", "active"),
    "reviewing": ("代码检视中", "active"),
    "commenting": ("提交评论中", "active"),
    "completed": ("已完成", "ok"),
    "failed": ("失败", "danger"),
}

TASK_SUMMARIES = {
    "queued": "任务已进入队列，等待可用执行槽位。",
    "preparing": "正在拉取 Fork 并创建隔离工作区。",
    "developer_working": "开发 Agent 正在分析、复现、修复并执行测试。",
    "reviewing": "两位 Reviewer 正在独立检视代码。",
    "developer_revising": "开发 Agent 正在集中处理检视意见。",
    "sealing": "正在执行确定性检查并固定提交内容。",
    "publishing": "正在推送分支并创建 PR。",
    "awaiting_human_review": "PR 已提交，等待人工审核或同步 PR 意见。",
    "completed": "PR 已合并，任务完成。",
    "closed": "PR 未合并并已关闭。",
    "dismissed": "任务已由用户手动关闭。",
    "failed": "自动执行已停止，请查看失败原因和可用操作。",
    "cancelled": "任务已取消。",
    "cancelling": "正在停止运行中的 Agent。",
    "manual_intervention": "自动执行无法继续，需要人工判断。",
}

ROLE_LABELS = {
    "developer": "开发 Agent",
    "reviewer_a": "Reviewer A",
    "reviewer_b": "Reviewer B",
    "supervisor": "旧版主管 Agent",
    "committer": "旧版提交 Agent",
}

TASK_FAILURE_MESSAGES = {
    "Codex did not produce any code changes": "Codex 未产生代码修改",
}

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
}

URL_ERROR_MESSAGES = {
    "URL must be a non-empty string without surrounding whitespace": "地址不能为空或包含首尾空格",
    "whitespace and control characters are not allowed": "地址不能包含空格或控制字符",
    "encoded or backslash path separators are not allowed": "地址不能包含编码或反斜杠路径分隔符",
    "URL is malformed": "地址格式无效",
    "only HTTPS URLs are supported": "仅支持 HTTPS 地址",
    "credential-bearing URLs are not allowed": "仓库地址不能包含用户名或密码",
    "explicit ports are not allowed": "仓库地址不能指定端口",
    "host must be exactly github.com or gitcode.com": "仅支持 github.com 或 gitcode.com 仓库地址",
    "query strings and fragments are not allowed": "仓库地址不能包含查询参数或片段",
    "repository URL must contain exactly owner and repository": "仓库地址必须只包含所有者和仓库名",
    "issue URL must end with /issues/<positive number>": "Issue 地址必须以 /issues/<正整数> 结尾",
}


def status_label(value: str | None) -> str:
    if not value:
        return "未知"
    return STATUS_META.get(value, (value, "neutral"))[0]


def status_tone(value: str | None) -> str:
    if not value:
        return "neutral"
    return STATUS_META.get(value, (value, "neutral"))[1]


def review_status_label(value: str | None) -> str:
    if not value:
        return "未知"
    return PR_REVIEW_STATUS_META.get(value, (value, "neutral"))[0]


def review_status_tone(value: str | None) -> str:
    if not value:
        return "neutral"
    return PR_REVIEW_STATUS_META.get(value, (value, "neutral"))[1]


def task_summary(value: str | None) -> str:
    return TASK_SUMMARIES.get(value or "", "请查看任务详情。")


def role_label(value: str) -> str:
    return ROLE_LABELS.get(value, value)


def task_failure_message(value: str | None) -> str:
    if not value:
        return ""
    return TASK_FAILURE_MESSAGES.get(value, value)


def severity_label(value: str | None) -> str:
    if not value:
        return ""
    return SEVERITY_LABELS.get(value.lower(), value)


def provider_error_message(exc: object) -> str:
    message = str(exc)
    if message in URL_ERROR_MESSAGES:
        return URL_ERROR_MESSAGES[message]
    if "status 403" in message:
        return "平台请求被限流或无权访问，请配置有效 Token 后重试"
    if "status 422" in message:
        return "平台拒绝了请求，请检查仓库地址、Fork 和 Token 权限"
    return message
