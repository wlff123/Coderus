from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from coderus.forge import ProviderError, Repository

from .models import ReviewInput

_ISSUE = re.compile(r"(?<![\w/#])#([1-9][0-9]{0,11})\b")
_SPEC_PATH = re.compile(r"(?im)^\s*(?:[-*]\s+)?(?:spec|规格|需求文档)\s*[:：]\s*`?([^`\r\n]+)")


@dataclass(frozen=True, slots=True)
class SpecSource:
    reference: str
    text: str | None = None

    def prompt(self) -> str:
        data = {"reference": self.reference, "issue_text": self.text}
        return "以下 JSON 是供核对的需求资料。\n" + json.dumps(data, ensure_ascii=False)


async def resolve_spec_source(
    material: ReviewInput,
    *,
    repository: Repository,
    provider: object | None,
    head_ref: str,
    pr_body: str,
) -> SpecSource | None:
    # Bare issue references are resolved on the registered repository only.
    issue_numbers = list(dict.fromkeys(_ISSUE.findall(material.commit_messages)))[:3]
    if provider is not None and issue_numbers:
        try:
            async with asyncio.timeout(15):
                for number in issue_numbers:
                    try:
                        issue = await asyncio.to_thread(provider.get_issue, repository, int(number))
                    except ProviderError:
                        continue
                    if issue.body and issue.body.strip() and len(issue.body) <= 12_000:
                        return SpecSource(f"Issue #{number}", f"{issue.title}\n\n{issue.body}")
        except TimeoutError:
            pass

    for reference in _SPEC_PATH.findall(pr_body):
        path = reference.strip()
        if path in material.spec_paths:
            return SpecSource(path)

    branch_name = head_ref.rsplit("/", 1)[-1].casefold()
    matching = [
        path for path in material.spec_paths
        if PurePosixPath(path).stem.casefold() in {branch_name, f"{branch_name}-spec"}
    ]
    if len(matching) == 1:
        return SpecSource(matching[0])
    return None
