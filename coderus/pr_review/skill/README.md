`SKILL.md` comes from [mattpocock/skills](https://github.com/mattpocock/skills/blob/7739eaed7f8f3915437a8def198a6d0d7206dbc7/skills/engineering/code-review/SKILL.md) at commit `7739eaed7f8f3915437a8def198a6d0d7206dbc7`. The adjacent `LICENSE` is the upstream MIT license.

Coderus runs Standards and Spec in separate `codex exec` sessions against the fixed PR comparison. Each session emits the Coderus JSON schema directly. The service resolves available issue or spec references before launching the Spec job and publishes the results using Coderus's PR comment format.
