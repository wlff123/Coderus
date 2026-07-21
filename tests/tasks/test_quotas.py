import pytest

from coderus.tasks.quotas import can_start_task


@pytest.mark.parametrize(
    ("global_running", "user_running", "global_limit", "user_limit"),
    [
        (0, 0, 1, 1),
        (7, 1, 8, 2),
        (0, 0, 8, 2),
    ],
)
def test_task_can_start_below_both_limits(
    global_running: int,
    user_running: int,
    global_limit: int,
    user_limit: int,
) -> None:
    assert can_start_task(
        global_running=global_running,
        user_running=user_running,
        global_limit=global_limit,
        user_limit=user_limit,
    )


@pytest.mark.parametrize(
    ("global_running", "user_running", "global_limit", "user_limit"),
    [
        (8, 0, 8, 2),
        (0, 2, 8, 2),
        (8, 2, 8, 2),
        (9, 0, 8, 2),
        (0, 3, 8, 2),
        (0, 0, 0, 2),
        (0, 0, 8, 0),
    ],
)
def test_task_cannot_start_at_or_above_either_limit(
    global_running: int,
    user_running: int,
    global_limit: int,
    user_limit: int,
) -> None:
    assert not can_start_task(
        global_running=global_running,
        user_running=user_running,
        global_limit=global_limit,
        user_limit=user_limit,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("global_running", -1),
        ("user_running", -1),
        ("global_limit", -1),
        ("user_limit", -1),
        ("global_running", True),
        ("user_limit", 1.5),
    ],
)
def test_quota_inputs_must_be_non_negative_integers(field: str, value: object) -> None:
    values = {
        "global_running": 0,
        "user_running": 0,
        "global_limit": 8,
        "user_limit": 2,
    }
    values[field] = value

    with pytest.raises(ValueError, match=f"{field} must be a non-negative integer"):
        can_start_task(**values)
