---
name: tool_heavy_task
content_shape: agentic-tool-use
reference_tokens_per_char: 0.27
description: Force at least three tool calls. No predicate — the value is measuring tool-fanout ratio, not text compliance.
---

[session-metrics:compare-suite:v1:prompt=tool_heavy_task]

Use your Read tool to read each of these three files in turn, then reconcile what you see across them into a single one-paragraph summary of this project's testing strategy:

1. `.claude/skills/session-metrics/SKILL.md`
2. `.claude/skills/session-metrics/references/pricing.md`
3. `.claude/skills/session-metrics/references/jsonl-schema.md`

You must actually invoke the Read tool three separate times — one per file — before writing the summary. Output the summary as one paragraph after the reads are complete.

<!-- PREDICATE -->

````python
# No predicate — tool fan-out is what we're measuring here, not the
# text output. The paired-turn ratio columns (token + cost) carry the
# signal; a check() that tested text would give misleading pass/fail.
check = None
````
