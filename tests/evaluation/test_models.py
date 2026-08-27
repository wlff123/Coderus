from __future__ import annotations

import pytest
from pydantic import ValidationError

from coderus.evaluation.models import BaselineSelection, TaskAnnotation


def keys(count: int) -> tuple[str, ...]:
    return tuple(f"RE-{index}" for index in range(1, count + 1))


def test_selection_requires_ten_to_twenty_unique_task_keys() -> None:
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=("RE-1",) * 10)
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=keys(9))
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=keys(21))

    selection = BaselineSelection(task_keys=keys(10))
    assert len(selection.task_keys) == 10


def test_selection_rejects_malformed_task_keys() -> None:
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=(*keys(9), "RV-10"))
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=(*keys(9), "RE-x"))


def test_selection_rejects_unknown_fields_and_wrong_version() -> None:
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=keys(10), extra_field=True)
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=keys(10), contract_version=2)


def test_annotations_must_reference_selected_tasks_uniquely() -> None:
    with pytest.raises(ValidationError):
        BaselineSelection(
            task_keys=keys(10),
            annotations=(TaskAnnotation(task_key="RE-99"),),
        )
    with pytest.raises(ValidationError):
        BaselineSelection(
            task_keys=keys(10),
            annotations=(
                TaskAnnotation(task_key="RE-1"),
                TaskAnnotation(task_key="RE-1"),
            ),
        )

    selection = BaselineSelection(
        task_keys=keys(10),
        annotations=(
            TaskAnnotation(task_key="RE-1", tests_passed=True, human_changed_lines=3),
        ),
    )
    assert selection.annotations[0].human_changed_lines == 3


def test_selection_round_trips_through_json() -> None:
    selection = BaselineSelection(task_keys=keys(12))
    restored = BaselineSelection.model_validate_json(selection.model_dump_json())
    assert restored == selection
