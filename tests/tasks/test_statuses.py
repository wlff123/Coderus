from coderus.tasks.statuses import RUNNING_TASK_STATES, TERMINAL_TASK_STATES


def test_task_state_groups_define_current_workflow_contract() -> None:
    assert RUNNING_TASK_STATES == (
        "preparing",
        "developer_working",
        "reviewing",
        "developer_revising",
        "sealing",
        "publishing",
    )
    assert TERMINAL_TASK_STATES == (
        "completed",
        "closed",
        "dismissed",
        "failed",
        "cancelled",
        "manual_intervention",
    )
    assert set(RUNNING_TASK_STATES).isdisjoint(TERMINAL_TASK_STATES)
