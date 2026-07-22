import json

import pytest

from coderus.workflow.reviewer_result import (
    ReviewerResultError,
    parse_reviewer_result,
    reviewer_result_schema_path,
)


def test_legacy_reviewer_result_defaults_to_contract_version_one() -> None:
    result = parse_reviewer_result(
        json.dumps({"decision": "approve", "findings": []})
    )

    assert result.contract_version == 1
    assert result.decision == "approve"
    assert result.findings == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "contract_version": 1,
            "decision": "approve",
            "findings": [{"severity": "high", "message": "still broken"}],
        },
        {
            "contract_version": 1,
            "decision": "changes_requested",
            "findings": [],
        },
        {
            "contract_version": 1,
            "decision": "changes_requested",
            "findings": [{"severity": "unknown", "message": "broken"}],
        },
        {"contract_version": 2, "decision": "approve", "findings": []},
    ],
)
def test_reviewer_result_rejects_inconsistent_or_unknown_contracts(payload) -> None:
    with pytest.raises(ReviewerResultError):
        parse_reviewer_result(json.dumps(payload))


def test_reviewer_result_accepts_a_valid_finding() -> None:
    result = parse_reviewer_result(
        json.dumps(
            {
                "contract_version": 1,
                "decision": "changes_requested",
                "findings": [
                    {"severity": "medium", "message": "targeted tests are missing"}
                ],
            }
        )
    )

    assert result.findings[0].message == "targeted tests are missing"
    assert reviewer_result_schema_path().is_file()
