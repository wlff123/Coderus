# 阶段 1：架构收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变数据库、页面 URL、任务流程和平台行为的前提下，拆分 Web 单体、统一入口用例、分离 Issue 工作流阶段并收紧 Forge 发布接口。

**Architecture:** 保留单进程 FastAPI、SQLite 调度器和固定状态机。路由模块通过显式构造参数获得依赖；应用服务拥有事务边界；TaskOrchestrator 继续控制状态和租约，但将提示词、Agent 执行、Reviewer 周期和发布封装委托给聚焦组件。

**Tech Stack:** Python 3.12、FastAPI、Jinja2、SQLAlchemy 2、SQLite、httpx、pytest。

## Global Constraints

- 保持单机部署、SQLite 和本地 Agent 子进程。
- 不改变现有 URL、HTTP 方法、表单字段、任务键、任务状态和中文文案。
- 不迁移数据库，不引入新依赖，不建立通用工作流引擎。
- GitHub 和 GitCode 的 Issue 同步实现本阶段不重写。
- 每次只迁移一个边界；调用者迁移完成后立即删除旧实现。
- 所有任务先写特征或失败测试，再实现最小改动并独立提交。

---

## 文件结构

Web 与应用服务：

- Create: coderus/application/__init__.py
- Create: coderus/application/issues.py
- Create: coderus/application/reviews.py
- Create: coderus/application/tasks.py
- Create: coderus/web/ui.py
- Create: coderus/web/routes/__init__.py
- Create: coderus/web/routes/auth.py
- Create: coderus/web/routes/dashboard.py
- Create: coderus/web/routes/users.py
- Create: coderus/web/routes/system.py
- Create: coderus/web/routes/repositories.py
- Create: coderus/web/routes/issues.py
- Create: coderus/web/routes/tasks.py
- Create: coderus/web/routes/reviews.py
- Create: coderus/web/runtime.py
- Modify: coderus/web/app.py
- Modify: coderus/integrations/feishu/service.py

工作流与 Forge：

- Create: coderus/workflow/prompts.py
- Create: coderus/workflow/agent_stage.py
- Create: coderus/workflow/review_cycle.py
- Create: coderus/workflow/publication.py
- Modify: coderus/workflow/orchestrator.py
- Modify: coderus/forge/protocols.py
- Modify: coderus/forge/github.py
- Modify: coderus/forge/gitcode.py

### Task 1: 固定 Web 外部行为契约

**Files:**
- Create: tests/web/test_route_contract.py
- Modify: tests/test_web.py only when sharing the existing app fixture.

**Interfaces:**
- Produces: route method/path/name snapshot.
- Consumes: create_app(..., start_scheduler=False).

- [ ] **Step 1: 写活动模式路由快照**

~~~python
EXPECTED_ROUTES = {
    ("GET", "/login", "login_page"),
    ("POST", "/login", "login"),
    ("POST", "/logout", "logout"),
    ("GET", "/account", "account_page"),
    ("POST", "/account/password", "change_own_password"),
    ("GET", "/", "dashboard"),
    ("GET", "/users", "users_page"),
    ("POST", "/users", "add_user"),
    ("POST", "/users/{user_id}/toggle", "toggle_user"),
    ("POST", "/users/{user_id}/reset-password", "reset_user_password"),
    ("GET", "/system", "system_page"),
    ("POST", "/system/github-credential", "save_github_credential"),
    ("POST", "/system/gitcode-credential", "save_gitcode_credential"),
    ("POST", "/system/feishu-bot", "save_feishu_bot_settings"),
    ("POST", "/system/feishu-bot/test", "test_feishu_bot"),
    ("GET", "/repositories", "repositories_page"),
    ("POST", "/repositories", "add_repository"),
    ("POST", "/repositories/{repository_id}/sync", "force_sync"),
    ("POST", "/repositories/{repository_id}/toggle", "toggle_repository"),
    ("POST", "/repositories/sync-all", "force_sync_all"),
    ("GET", "/issues", "issues_page"),
    ("POST", "/issues/manual", "add_issue_manually"),
    ("POST", "/issues/{issue_id}/dispatch", "dispatch"),
    ("POST", "/issues/{issue_id}/ignore", "ignore_issue"),
    ("POST", "/issues/{issue_id}/restore", "restore_issue"),
    ("GET", "/tasks", "tasks_page"),
    ("GET", "/tasks/{task_id}", "task_detail"),
    ("POST", "/tasks/{task_id}/cancel", "cancel_task"),
    ("POST", "/tasks/{task_id}/close", "close_task"),
    ("POST", "/tasks/{task_id}/feedback/sync", "sync_task_feedback"),
    ("POST", "/tasks/{task_id}/publish-wip", "publish_existing_wip"),
    ("POST", "/tasks/{task_id}/feedback/handle", "handle_task_feedback"),
    ("GET", "/reviews", "reviews_page"),
    ("GET", "/reviews/{review_id}", "review_detail"),
    ("POST", "/reviews", "create_review"),
}
~~~

过滤 FastAPI 自动路由，断言实际集合完全相等。

- [ ] **Step 2: 固定关键响应行为**

对未登录重定向、普通用户访问管理页、CSRF 失败、发布排空拒绝写操作、404 和成功表单重定向各保留一条特征测试。

- [ ] **Step 3: 运行并提交**

~~~bash
uv run pytest tests/web/test_route_contract.py tests/test_web.py -q
git add tests/web/test_route_contract.py tests/test_web.py
git commit -m "test: lock web route behavior"
~~~

### Task 2: 建立共享应用服务

**Files:**
- Create: coderus/application/__init__.py
- Create: coderus/application/issues.py
- Create: coderus/application/reviews.py
- Create: coderus/application/tasks.py
- Test: tests/application/test_issues.py
- Test: tests/application/test_reviews.py
- Test: tests/application/test_tasks.py
- Modify: coderus/integrations/feishu/service.py

**Interfaces:**
- Produces: IssueCommands.add_and_dispatch(issue_url: str, actor_id: int) -> int
- Produces: IssueCommands.dispatch(issue_id: int, actor_id: int, instructions: str = "") -> int
- Produces: IssueCommands.dispatch_in_session(session: Session, issue_id: int, actor_id: int, instructions: str = "") -> int
- Produces: ReviewCommands.enqueue(url: str, source: ReviewSource) -> int
- Produces: ReviewCommands.enqueue_in_session(session: Session, url: str, source: ReviewSource) -> int
- Produces: TaskCommands.request_cancel(task_id: int, actor_id: int) -> CancelResult
- Produces: TaskCommands.close(task_id: int, actor_id: int) -> None
- Produces: TaskCommands.sync_feedback(task_id: int, actor_id: int) -> Awaitable[int]
- Produces: TaskCommands.queue_existing_publish(task_id: int, actor_id: int) -> None
- Produces: TaskCommands.queue_feedback_revision(task_id: int, actor_id: int, feedback_ids: tuple[int, ...]) -> None

- [ ] **Step 1: 写事务和权限失败测试**

~~~python
task_id = commands.dispatch(issue.id, admin.id, "优先验证回归")
with session_factory() as session:
    task = session.get(Task, task_id)
    assert task.instructions == "优先验证回归"
    assert task.issue.triage_state == "dispatched"
~~~

同时断言失败回滚、停用用户不能派发、关闭只接受当前允许状态。

- [ ] **Step 2: 运行并确认失败**

Run: uv run pytest tests/application -q

Expected: FAIL，包含 ModuleNotFoundError: No module named 'coderus.application'。

- [ ] **Step 3: 实现显式事务边界**

~~~python
@dataclass(frozen=True, slots=True)
class ReviewSource:
    chat_id: str
    message_id: str
    sender_id: str


@dataclass(frozen=True, slots=True)
class CancelResult:
    should_signal_runner: bool


class IssueCommands:
    def __init__(self, session_factory, providers: Mapping[str, IssueProvider]) -> None:
        self._sessions = session_factory
        self._providers = providers

    def dispatch(self, issue_id: int, actor_id: int, instructions: str = "") -> int:
        with self._sessions() as session:
            actor = session.get(User, actor_id)
            issue = session.get(Issue, issue_id)
            if actor is None or not actor.is_active:
                raise ValueError("用户不存在或已停用")
            if issue is None:
                raise ValueError("Issue 不存在")
            task = dispatch_issue(
                session, issue, actor, instructions, commit=False
            )
            session.commit()
            return task.id
~~~

ReviewCommands 和 TaskCommands 使用相同模式，不导入 FastAPI、Jinja2 或飞书类型。`sync_feedback` 在外部请求前读取并释放数据库会话，平台返回后重新打开会话并使用 CAS 写入；`request_cancel` 提交后由入口根据 `should_signal_runner` 决定是否通知运行中的 Orchestrator。

- [ ] **Step 4: 迁移飞书派发和检视**

保持消息去重与 outbox 的单事务语义。飞书外层已有 Session 时调用接收 Session 的领域函数，不能在一个命令中分成两个提交。

- [ ] **Step 5: 运行并提交**

~~~bash
uv run pytest tests/application tests/integrations/feishu -q
git add coderus/application coderus/integrations/feishu/service.py tests/application tests/integrations/feishu
git commit -m "refactor: centralize task command services"
~~~

### Task 3: 提取 Web 公共 UI、认证和用户路由

**Files:**
- Create: coderus/web/ui.py
- Create: coderus/web/routes/__init__.py
- Create: coderus/web/routes/auth.py
- Create: coderus/web/routes/users.py
- Modify: coderus/web/app.py
- Test: tests/web/test_route_contract.py
- Test: tests/test_web.py

**Interfaces:**
- Produces: WebUI.current_user(request, session) -> User | None
- Produces: WebUI.context(request, current_user=None, **values) -> dict[str, object]
- Produces: build_auth_router(ui: WebUI, session_factory: Callable[[], Session]) -> APIRouter
- Produces: build_user_router(ui: WebUI, session_factory: Callable[[], Session]) -> APIRouter

- [ ] **Step 1: 写会话失效和 flash 一次性读取测试**

~~~python
ui.flash(request, "已保存", "ok")
first = ui.context(request)
second = ui.context(request)
assert first["flash"] == {"message": "已保存", "tone": "ok"}
assert second["flash"] is None
~~~

- [ ] **Step 2: 移动 csrf、user_for、flash 和 context**

保持 Cookie key、CSRF key 和失效会话清理行为不变。

- [ ] **Step 3: 提取认证和用户路由**

~~~python
def build_auth_router(*, ui: WebUI, session_factory: Callable[[], Session]) -> APIRouter:
    router = APIRouter()
    return router
~~~

模块不得使用全局 Session factory 或服务实例。

- [ ] **Step 4: app.py 注册 Router 并删除旧闭包**

~~~python
app.include_router(build_auth_router(ui=ui, session_factory=sessions))
app.include_router(build_user_router(ui=ui, session_factory=sessions))
~~~

- [ ] **Step 5: 运行并提交**

~~~bash
uv run pytest tests/web/test_route_contract.py tests/test_web.py -q
git add coderus/web/ui.py coderus/web/routes coderus/web/app.py tests
git commit -m "refactor: extract authentication web routes"
~~~

### Task 4: 提取仓库和 Issue 路由

**Files:**
- Create: coderus/web/routes/repositories.py
- Create: coderus/web/routes/issues.py
- Modify: coderus/web/app.py
- Test: tests/web/test_route_contract.py
- Test: tests/test_web.py

**Interfaces:**
- Consumes: IssueCommands
- Produces: build_repository_router(...) -> APIRouter
- Produces: build_issue_router(...) -> APIRouter

- [ ] **Step 1: 写路由调用应用服务的失败测试**

注入记录调用的 IssueCommands fake，POST 派发后断言只调用一次，参数为当前用户 ID、Issue ID 和原 instructions。

- [ ] **Step 2: 提取仓库路由**

显式传入 Session factory、provider runtime、IssuePoller 和 WebUI。保留同步状态、错误映射、仓库筛选和重定向参数。

- [ ] **Step 3: 提取 Issue 路由**

手工添加和派发调用 IssueCommands；忽略和恢复迁移后删除 app.py 中的重复 SQL。

- [ ] **Step 4: 运行并提交**

~~~bash
uv run pytest tests/web/test_route_contract.py tests/test_web.py tests/test_issue_service.py -q
git add coderus/web/routes coderus/web/app.py tests
git commit -m "refactor: extract repository and issue routes"
~~~

### Task 5: 提取任务、检视、工作台和系统路由

**Files:**
- Create: coderus/web/routes/tasks.py
- Create: coderus/web/routes/reviews.py
- Create: coderus/web/routes/dashboard.py
- Create: coderus/web/routes/system.py
- Modify: coderus/web/app.py
- Test: tests/web/test_route_contract.py
- Test: tests/test_web.py

**Interfaces:**
- Consumes: TaskCommands、ReviewCommands
- Produces: build_task_router、build_review_router、build_dashboard_router、build_system_router

- [ ] **Step 1: 提取任务与检视路由**

取消、关闭、继续发布和反馈处理调用 TaskCommands；创建检视调用 ReviewCommands。列表和详情保留只读 SQL，不提前建立通用 Repository 层。

- [ ] **Step 2: 提取工作台路由**

移动仓库页签、任务聚合和展示模型构造，继续使用 coderus.web.presentation。

- [ ] **Step 3: 提取系统路由**

显式传入凭据管理器、飞书设置管理器、HTTP client 和运行状态访问器。Token 不进入 repr、异常或模板上下文。

- [ ] **Step 4: 删除 app.py 中所有业务路由闭包**

app.py 只保留应用创建、middleware、运行时装配、health/readiness、静态文件和 Router 注册。

- [ ] **Step 5: 运行并提交**

~~~bash
uv run pytest tests/web/test_route_contract.py tests/test_web.py -q
git add coderus/web/routes coderus/web/app.py tests
git commit -m "refactor: extract remaining web routes"
~~~

### Task 6: 提取运行时装配并迁移 lifespan

**Files:**
- Create: coderus/web/runtime.py
- Modify: coderus/web/app.py
- Test: tests/test_web.py
- Test: tests/test_runtime_lock.py

**Interfaces:**
- Produces: RuntimeComponents.start() -> Awaitable[None]
- Produces: RuntimeComponents.stop() -> Awaitable[None]
- Produces: build_runtime(settings, **existing_create_app_overrides) -> RuntimeComponents

- [ ] **Step 1: 写启动失败逆序清理测试**

按 model proxy、scheduler、poller、PR scheduler、Feishu 顺序启动，令 Feishu 抛错；断言此前组件逆序停止，engine 和自有 HTTP client 各关闭一次。

- [ ] **Step 2: 实现 RuntimeComponents**

对象拥有 engine、Session factory、providers、forges、orchestrators、schedulers、pollers、model proxy、Feishu bot 和自有 clients。现有测试和诊断读取的对象继续挂到 app.state。

- [ ] **Step 3: 使用 lifespan 替换 on_event**

~~~python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()
~~~

外部注入 Client 不由 Coderus 关闭；内部创建 Client 只关闭一次。

- [ ] **Step 4: 保持三种运行模式**

active 获取 Manager lock 并启动后台组件；preview 使用隔离路径且不领取任务；maintenance 不初始化 Agent、Forge 和后台组件。

- [ ] **Step 5: 运行并提交**

~~~bash
uv run pytest tests/test_web.py tests/test_runtime_lock.py -q
git add coderus/web/runtime.py coderus/web/app.py tests
git commit -m "refactor: isolate web runtime lifecycle"
~~~

### Task 7: 提取工作流提示词

**Files:**
- Create: coderus/workflow/prompts.py
- Modify: coderus/workflow/orchestrator.py
- Test: tests/workflow/test_prompts.py
- Test: tests/test_workflow.py

**Interfaces:**
- Produces: developer_prompt(task: Task) -> str
- Produces: review_prompt(task: Task, focus: str, developer_report: str) -> str
- Produces: revision_prompt(task: Task, findings: list[dict[str, Any]]) -> str
- Produces: feedback_prompt(task: Task, feedback: list[dict[str, Any]]) -> str
- Produces: pull_request_body(task: Task, reports: list[DeveloperReport]) -> str

- [ ] **Step 1: 为五种现有文本写逐字特征测试**

固定 Task fixture，断言流程、Issue URL、用户 instructions、六段中文报告和 JSON Schema 要求仍存在。

- [ ] **Step 2: 移动纯函数和常量**

函数不得访问 Session、Runner、Forge 或文件系统。删除 Orchestrator 对应私有方法。

- [ ] **Step 3: 运行并提交**

~~~bash
uv run pytest tests/workflow/test_prompts.py tests/test_workflow.py -q
git add coderus/workflow/prompts.py coderus/workflow/orchestrator.py tests
git commit -m "refactor: extract workflow prompts"
~~~

### Task 8: 提取 AgentStageExecutor

**Files:**
- Create: coderus/workflow/agent_stage.py
- Modify: coderus/workflow/orchestrator.py
- Test: tests/workflow/test_agent_stage.py
- Test: tests/test_workflow.py

**Interfaces:**
- Produces: AgentStageExecutor.execute(...) -> Awaitable[JobResult]
- Consumes: Session factory、Runner、CredentialBroker、timeout、cancel event、状态 callback。

- [ ] **Step 1: 写成功、失败、取消和凭据测试**

~~~python
result = await executor.execute(
    task_id=1,
    status="developer_working",
    stage=Stage.DEVELOP,
    role=AgentRole.DEVELOPER,
    workspace=workspace,
    prompt="work",
    claim_token="claim",
    cancel_event=asyncio.Event(),
)
assert result.status is JobStatus.SUCCEEDED
assert saved_run.structured_result["developer_report"]["问题描述"]
~~~

同时断言 attempt 递增、模型用量保存、Token 撤销和 AgentRun 终态。

- [ ] **Step 2: 移动 _run_stage 和 _finish_agent_run**

execute 接收 transition: Callable[[int, str, str], None]，参数依次为 task_id、目标状态、claim_token。状态 CAS 继续归 Orchestrator。

- [ ] **Step 3: Orchestrator 委托并删除旧实现**

TaskOrchestrator 构造函数保持兼容；未注入 executor 时创建默认实例。

- [ ] **Step 4: 运行并提交**

~~~bash
uv run pytest tests/workflow/test_agent_stage.py tests/test_workflow.py -q
git add coderus/workflow/agent_stage.py coderus/workflow/orchestrator.py tests
git commit -m "refactor: extract agent stage execution"
~~~

### Task 9: 提取 Reviewer 周期

**Files:**
- Create: coderus/workflow/review_cycle.py
- Modify: coderus/workflow/orchestrator.py
- Test: tests/workflow/test_review_cycle.py

**Interfaces:**
- Produces: ReviewCycle.run(task, workspace, developer_report, claim_token, cancel_event) -> Awaitable[list[dict[str, Any]]]
- Consumes: AgentStageExecutor 和 Session factory。

- [ ] **Step 1: 写并行执行和去重测试**

两个 fake Reviewer 通过 Event 证明同时启动；返回重叠 finding，断言顺序稳定、重复项只保留一次且每个角色写一条 Review。

- [ ] **Step 2: 移动 Reviewer 规格、解析和持久化**

保持角色、Stage、focus 文案、结构化解析和 deduplicate_findings 行为。一个 Reviewer 失败时 TaskGroup 取消另一个。

- [ ] **Step 3: 删除 _run_reviewers 和 _record_review**

Orchestrator 只调用 review_cycle.run，并根据 findings 决定是否修正。

- [ ] **Step 4: 运行并提交**

~~~bash
uv run pytest tests/workflow/test_review_cycle.py tests/test_workflow.py -q
git add coderus/workflow/review_cycle.py coderus/workflow/orchestrator.py tests
git commit -m "refactor: extract reviewer cycle"
~~~

### Task 10: 提取提交封装和 PR 发布

**Files:**
- Create: coderus/workflow/publication.py
- Modify: coderus/workflow/orchestrator.py
- Test: tests/workflow/test_publication.py
- Test: tests/test_workflow.py

**Interfaces:**
- Produces: TaskPublication.finalize(...) -> Awaitable[PublishResult]
- Produces: TaskPublication.publish_existing(...) -> Awaitable[PublishResult]
- Consumes: workspace git、ForgeRegistry、Session factory、artifacts root、Git identity、状态 callback。

- [ ] **Step 1: 写封装顺序和发布对账测试**

~~~python
assert calls == [
    "assert_has_changes", "seal", "assert_no_secrets", "assert_tree",
    "commit", "assert_committed_tree", "publish",
]
~~~

网络结果不确定后重试必须复用 publication_key、固定 branch 和 commit SHA。

- [ ] **Step 2: 移动 _finalize、_publish 和 _begin_publication**

发布组件返回 PublishResult，不发飞书通知；Orchestrator 在持久化 awaiting_review 后通知。

- [ ] **Step 3: 移动已有提交发布路径**

publish_existing 先验证工作区分支和 clean commit，再按原 publication intent 对账。反馈 selected/processed 更新仍由 Orchestrator 控制。

- [ ] **Step 4: 运行并提交**

~~~bash
uv run pytest tests/workflow/test_publication.py tests/test_workflow.py -q
git add coderus/workflow/publication.py coderus/workflow/orchestrator.py tests
git commit -m "refactor: extract task publication"
~~~

### Task 11: 收紧 Forge 发布接口

**Files:**
- Modify: coderus/forge/protocols.py
- Modify: coderus/forge/github.py
- Modify: coderus/forge/gitcode.py
- Modify: coderus/workflow/publication.py
- Test: tests/forge/test_protocols.py
- Test: tests/test_forge_runtime.py
- Test: tests/publisher/test_github.py
- Test: tests/publisher/test_gitcode.py

**Interfaces:**
- Produces: PublishRequest
- Changes: async Forge.publish(request: PublishRequest) -> PublishResult

- [ ] **Step 1: 写 PublishRequest 验证测试**

~~~python
request = PublishRequest(
    workspace=workspace,
    upstream_owner="acme",
    repository_name="widgets",
    default_branch="main",
    branch="coderus/issue-42-7",
    title="Fix parser",
    body="报告",
)
assert request.workspace == workspace.resolve()
~~~

空 owner/name/title、非绝对 workspace、默认分支等于工作分支必须拒绝。

- [ ] **Step 2: 实现 typed request**

~~~python
@dataclass(frozen=True, slots=True)
class PublishRequest:
    workspace: Path
    upstream_owner: str
    repository_name: str
    default_branch: str
    branch: str
    title: str
    body: str


class Forge(Protocol):
    async def publish(self, request: PublishRequest) -> PublishResult: ...
~~~

- [ ] **Step 3: 迁移两个 Forge**

适配器显式展开字段调用现有 Publisher；删除 Any kwargs 和字典索引。Publisher 的 HTTP 与 Git 代码本阶段不移动。

- [ ] **Step 4: 迁移 fake 和调用者**

Run: rg -n "\.publish\(" coderus tests

Expected: Forge 发布调用只传一个 PublishRequest；PR 评论和 Issue provider 不变。

- [ ] **Step 5: 运行并提交**

~~~bash
uv run pytest tests/forge tests/test_forge_runtime.py tests/publisher tests/workflow/test_publication.py -q
git add coderus/forge coderus/workflow/publication.py tests
git commit -m "refactor: type forge publication requests"
~~~

### Task 12: 清理、文档和验收

**Files:**
- Modify: docs/architecture.md
- Modify: docs/evolution-roadmap.md
- Modify: only files containing imports made unused by Tasks 1-11.

- [ ] **Step 1: 搜索重复和遗留定义**

~~~bash
rg -n "def (csrf|user_for|flash|context|_run_stage|_run_reviewers|_finalize|_publish)" coderus
rg -n "\*\*kwargs: Any" coderus/forge
~~~

Expected: 公共 Web helper 和阶段实现各只有一处；Forge publish 不接收任意 kwargs。

- [ ] **Step 2: 检查依赖方向**

web/routes 不得导入 Scheduler、LocalCodexRunner 或具体 Publisher；application 不得导入 FastAPI、Jinja2 或飞书；workflow 不得导入 web。

- [ ] **Step 3: 更新架构文档**

记录已完成的 Router、应用服务、阶段组件和 typed Forge；不得把阶段 2 的任务事件或诊断描述为已实现。

- [ ] **Step 4: 运行完整门禁**

~~~bash
uv run python scripts/check-public-release.py --root .
uv run ruff check coderus tests scripts
uv run pytest -q
git diff --check
~~~

Expected: 全部通过。

- [ ] **Step 5: 运行隔离候选版本端到端测试**

在 preview 数据库和工作目录验证登录、系统配置、GitHub/GitCode 仓库与 Issue、fake Runner Issue 全流程、fake PR 检视、飞书命令和进程退出。不得使用生产数据库写测试。

- [ ] **Step 6: 提交文档和清理**

~~~bash
git add coderus docs tests
git commit -m "docs: record converged application architecture"
~~~

## 阶段 1 完成标准

- 路由、页面、状态、数据库和飞书命令兼容。
- web/app.py 只负责应用创建、middleware、运行时装配、health/readiness、静态文件和 Router 注册。
- 网页和飞书的派发与 PR 检视调用同一应用服务。
- TaskOrchestrator 只保留租约、状态推进、分支选择和顶层异常策略。
- Agent 执行、Reviewer 周期、发布封装和提示词各有独立测试。
- Forge publish 使用 typed request；Issue provider 暂不迁移。
- 完整测试、公开发布扫描和隔离端到端验证通过。
