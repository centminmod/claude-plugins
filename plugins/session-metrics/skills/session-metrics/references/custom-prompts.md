# Custom prompts for `--compare-run`

> Think of the prompts like a test playlist. The skill ships with 10 tracks. You
> add your own tracks to the queue — the originals stay untouched.

---

## Add a prompt in 30 seconds

Two commands. Nothing else to know.

**Step 1 — add your prompt:**
```
session-metrics --compare-add-prompt "Your prompt text here."
```

**Step 2 — run the comparison:**
```
/session-metrics compare-run
```

That's it. Your prompt is included automatically alongside the built-in 10 for every
future `--compare-run`. No flags. No configuration files.

---

## What just happened?

Running `--compare-add-prompt` created a plain-text file at
`~/.session-metrics/prompts/`. The skill checks that directory automatically every
time it runs a comparison — no flags needed.

**To see what will run before spending on inference:**
```
session-metrics --compare-list-prompts
```

This prints the full list with a call-count summary (e.g. `11 prompts × 2 models = 22 calls`).

**What "ratio only" means:** Your custom prompt captures cost and token data just
like the built-in 10, but there is no pass/fail scoring column in the report
(because you haven't written a predicate for it — more on that in the Advanced
section below). Everything else — model, input tokens, output tokens, cost — is
measured.

---

## Manage your prompts

**Add another prompt:**
```
session-metrics --compare-add-prompt "Write a limerick about machine learning."
```

**See everything that will run:**
```
session-metrics --compare-list-prompts
```

Output looks like:
```
Suite: 10 built-in + 2 user = 12 prompt(s) × 2 models = 24 calls

  · claudemd_summarise    predicate: yes   (built-in)
  ...
  + my_first_prompt_user  predicate: no    ~/.session-metrics/prompts/
  + my_second_prompt_user predicate: no    ~/.session-metrics/prompts/

· = built-in   + = your prompts
```

**Remove a prompt** (use the name shown by `--compare-list-prompts`):
```
session-metrics --compare-remove-prompt my_first_prompt_user
```

**Add by creating a file directly** (alternative to `--compare-add-prompt`):
Just save any `.md` file with your prompt text into `~/.session-metrics/prompts/` and
it will be picked up automatically. No special format required — plain text is fine:

```
~/.session-metrics/prompts/joke_test.md
─────────────────────────────────────────
Tell me a programming joke and rate it 1-10.
```

---

## Advanced: add a pass/fail score to your prompt

> Skip this section unless you want the IFEval compliance column populated for
> your prompt. Most users don't need it.

The built-in prompts include a Python predicate — a small function that checks
whether the model's output satisfies a specific constraint. Think of it as a unit
test: the report shows a checkmark when the model passes and a cross when it fails.

To add a predicate, you need to write a full-format prompt file instead of the
plain-text "lite" format. Here's the minimal example:

```markdown
---
name: my_word_count
description: Response must be exactly 50 words.
---

[session-metrics:user-suite:v1:prompt=my_word_count]

Describe what makes a good API in exactly 50 words. Count carefully.

<!-- PREDICATE -->

````python
def check(text: str) -> bool:
    return len(text.split()) == 50
````
```

Save this file as `~/.session-metrics/prompts/my_word_count.md`.

**Key rules for full-format files:**

| Part | Required | Notes |
|------|----------|-------|
| `---` frontmatter | Yes | Must open and close with `---` fences |
| `name:` field | Yes | Must match the `prompt=<name>` in the sentinel; `[a-z0-9_]` only |
| Sentinel line | Yes | Copy the line `[session-metrics:user-suite:v1:prompt=<name>]` exactly, substituting your name |
| Prompt body | Yes | The text that gets sent to Claude |
| `<!-- PREDICATE -->` | No | Only needed if you want the pass/fail column |
| Python `check` function | No | Must return `True` (pass) or `False` (fail). Use `check = None` if no predicate needed |

The four-backtick fence (` ```` `) around the predicate block is intentional — it
avoids colliding with any three-backtick code samples in your prompt body.

**Verify it parses correctly before running:**
```
session-metrics --compare-list-prompts
```
Any syntax error in the predicate will surface here, before you spend on inference.

---

## Power user: replace all 10 prompts with a custom set

If you want to run a completely different benchmark suite — different prompt count,
different content shapes, starting from scratch — use `--compare-prompts DIR` to
point at your own directory. This replaces the built-in 10 entirely:

```
session-metrics --compare-run claude-opus-4-6 claude-opus-4-7 --compare-prompts ~/my-benchmarks/
```

All files in `~/my-benchmarks/` must be in full format (frontmatter + sentinel +
predicate, as described in the Advanced section). Numeric filename prefixes
(`01_first.md`, `02_second.md`) control run order.

For the full format specification, see
[`references/model-compare.md`](model-compare.md) → `## Prompt suite (v1)`.

---

## Troubleshooting

**"user prompt name X collides with a built-in prompt name"**
Rename your file (e.g. `my_claudemd.md` instead of `claudemd_summarise.md`) or
change the `name:` field in the frontmatter.

**"cannot derive a prompt name from the filename stem"**
Your filename contains only digits and underscores (e.g. `01_.md`). Add a
descriptive word: `01_my_test.md`.

**"malformed YAML frontmatter"**
Your file starts with `---` but is missing the closing `---` fence, or has a
syntax error. Either fix the frontmatter or remove the leading `---` to use
plain-text (lite) format instead.

**`--compare-remove-prompt` says "no user prompt named X"**
Run `--compare-list-prompts` to see the exact names. The name is derived from
the filename stem, not the `name:` field in frontmatter.

**Predicate syntax error**
Run `--compare-list-prompts` — it parses all files and will print the error
before any inference runs.
