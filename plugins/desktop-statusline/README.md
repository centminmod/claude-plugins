# desktop-statusline

**A Claude Code mod that puts a status band above the prompt in the Claude
Desktop app's Code tab.**

The Claude Code CLI has a `statusLine` setting that runs a script and prints
its output under the prompt. The desktop Code tab never runs that script, so
desktop sessions show none of it. This mod reads the same session data from
inside Claude Code and draws it in the desktop app instead.

![The desktop-statusline band above the prompt in the Claude Desktop app's Code tab, showing folder, branch, changed files, session cost, context meter, 5-hour and weekly usage limit meters, and last-turn stats](images/desktop-statusline.png)

- Desktop only. In a terminal session it draws nothing and skips its git
  calls, so your CLI `statusLine` keeps working as before.
- No network calls. The only process it starts is `git`, in the session's
  folder.
- Works on any plan. The usage limit meters appear when Claude Code reports
  plan limits for your account.

---

## What it shows

Four lines on a normal window. On a narrow window the two limit meters stack,
which makes five.

1. **Where you are.** Folder, git branch, a 🌳 marker in a worktree, commits
   ahead or behind upstream, and the number of uncommitted files. Right-aligned
   on the same line: session age, prompt count, and session cost.
2. **Context.** A meter for how full the context window is, with tokens used
   against the window size. When an auto-compact window smaller than the
   model's is set (`/autocompact`, the `autoCompactWindow` setting, or
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW`), it measures against that window, as
   `/context` does.
3. **Plan usage limits.** The 5-hour and weekly limits, each with a meter and
   a reset countdown.
4. **Last turn.** How long it took, the model that answered, the cache hit
   rate, how long the session has been idle, and how many times it has
   compacted, with the token count before and after the last compaction.

Each running subagent gets a row of its own, up to three, followed by a
`+N more agents running` line.

Meters turn amber at 80% and red at 95%. A cache hit rate under 50% shows as a
warning, and so does an idle time past the prompt-cache TTL, marked
`(cache cold)`. A toast pops up when a usage limit crosses 80% and again at
95%, once per limit window.

## Install

Mods need Claude Code v2.1.287 or later. The desktop app ships its own copy of
Claude Code, so keep the app updated too.

Run these inside the Claude Code terminal CLI (`claude` in your shell):

```
/plugin marketplace add centminmod/claude-plugins
/plugin install desktop-statusline@centminmod
```

Then start a new session in the desktop app's Code tab. The band appears once
the session has data to show, usually right away.

## Settings

One setting, `cache_ttl`, sets how long your session can sit idle before the
band marks the prompt cache cold. Claude Code asks for it when you enable the
plugin, and you can change it later in `/plugin` or `/config`. Enter `1h` or
`5m`; any other value counts as `1h`.

Claude Code's [prompt caching docs](https://code.claude.com/docs/en/prompt-caching#cache-lifetime)
give these defaults for the main conversation:

| Account | Default TTL | Set `cache_ttl` to |
| :- | :- | :- |
| Claude subscription, within plan usage | 1 hour | `1h`, the default |
| API key, cloud provider, or a subscription drawing on usage credits | 5 minutes | `5m` |

A `promptCacheTtl` setting or the `CLAUDE_CODE_PROMPT_CACHE_TTL` environment
variable overrides those defaults, so match whatever you set there. The docs
don't split Pro from Max. If you're not sure which TTL you get, run this and
look at `usage.cache_creation` in the output:

```bash
claude -p "hello" --output-format json
```

One-hour cache writes count under `ephemeral_1h_input_tokens`, five-minute
writes under `ephemeral_5m_input_tokens`.

Claude Code stores your answer in `~/.claude/settings.json`:

```json
{
  "pluginConfigs": {
    "desktop-statusline@centminmod": {
      "options": { "cache_ttl": "5m" }
    }
  }
}
```

See [Cache lifetime](https://code.claude.com/docs/en/prompt-caching#cache-lifetime)
for which TTL your sessions get.

## When it updates

The band refreshes when the session starts, when the desktop app attaches,
after every turn, when a plan limit's percentage changes, two seconds after
Claude starts a subagent, and every 60 seconds.

## What it runs, reads and sends

**Programs it runs.** Only `git`, with two fixed read-only commands, in the
session's folder and with a 5-second timeout:

- `git status --porcelain=v2 --branch`, for the branch, commits ahead or
  behind upstream, and the uncommitted file count
- `git rev-parse --git-dir --git-common-dir`, to tell a worktree from the main
  checkout

Neither command contacts a remote, and the mod runs them only in desktop
sessions. It starts no other program. Both commands are written out in full at
their `$.process.run` calls in `hooks/register.tsx`. The session's folder
(`$.session.cwd`) is passed to them only as the working directory; nothing else
the mod reads, such as the prompt count from `$.session.turns`, is passed to
them. Their output goes only into the band.

**What it reads.** Through Claude Code's mods API, it reads the session's
usage (context size, cost, and the 5-hour and weekly plan limits), the
session's folder, the number of prompts, the start time, and the type and
short task description of each running subagent. It asks for the usage with
Claude Code's local `/context` estimate (no API call) and uses only its
auto-compact window and whether auto-compaction is on; it ignores the rest of
that estimate, such as memory file paths and tool names. From each finished turn it
reads the duration, the model, and the token counts used for the cache hit
rate. After a compaction it reads the token counts before and after. It
doesn't read prompt or response text, files, environment variables, or
credentials such as API keys or tokens. The plan limits come from Claude Code,
not from your login.

**What it sends.** Nothing. It makes no network calls and writes no files.
What it reads goes only into the band above the prompt and the usage-limit
toasts, and into the session's `$.state`, which ends with the session.

The one URL in the code, `http://www.w3.org/2000/svg`, is the standard SVG
namespace in the meter bars' `xmlns` attribute. It labels the markup as SVG
and is never fetched. The token counts next to it are the context and
compaction sizes the band displays, not API keys or login tokens.

**What its hooks change.** Nothing. The `session.compact` hook waits for the
compaction to finish, records the before and after token counts, and returns
the result unchanged. The `tool.call` hook for the Agent tool schedules a band
refresh two seconds later and passes the call through unchanged.

## Check it yourself

Before installing a mod, you can list what it hooks and calls without running
it. Clone this repo and run:

```bash
claude plugin validate plugins/desktop-statusline
```

For this mod the report lists:

- **Hooks**: `session.start`, `session.attach` for the desktop,
  `session.measure`, `turn.complete`, `tool.call` for the Agent tool only,
  `session.compact`, and `ui.render` for the `AbovePrompt` band.
- **Calls**: session usage, cwd and turn count, the running agent list, the
  clock, toasts, and `$.process.run` for `git status --porcelain=v2 --branch`
  and `git rev-parse --git-dir --git-common-dir`.

It keeps its values in the session's `$.state` and writes no files.

## Limitations

- **No lines added or removed.** The mods API doesn't expose them, so the band
  shows the uncommitted file count instead.
- **The cache-cold warning follows your setting, not your account.** Claude
  Code drops to the 5-minute TTL when a subscription starts drawing on usage
  credits, and the band keeps using whatever `cache_ttl` says.
- **No effort level or permission mode.** The desktop app's footer already
  shows both.
- **Nothing draws in the VS Code extension, `claude -p`, or cloud sessions.**
  Mods only draw in the terminal and the desktop Code tab, and this one only
  draws in the desktop.

## Turn it off

Disable it from the **Installed** tab in `/plugin`, or uninstall it from your
shell:

```bash
claude plugin uninstall desktop-statusline@centminmod
```

## Licence

MIT. See [`LICENSE`](LICENSE).
