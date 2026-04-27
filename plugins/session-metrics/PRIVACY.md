# Privacy Policy — session-metrics plugin

**Last updated:** 2026-04-28
**Plugin:** `session-metrics` (`centminmod/claude-plugins` marketplace)
**Maintainer:** George Liu — [github.com/centminmod](https://github.com/centminmod)
**Contact:** [github.com/centminmod/claude-plugins/issues](https://github.com/centminmod/claude-plugins/issues)

This is a short, factual policy. The plugin runs entirely on your own machine, makes no network requests on default code paths, and collects no telemetry. The sections below describe exactly what it touches on disk so you can verify the claims yourself against the source code.

## 1. Who this applies to

Anyone who installs the `session-metrics` plugin into Claude Code, by any of the supported channels:

- The `centminmod/claude-plugins` marketplace (`/plugin install session-metrics@centminmod`).
- Direct copy from [`centminmod/my-claude-code-setup`](https://github.com/centminmod/my-claude-code-setup).
- A clone of the upstream development repository.

It applies to every version of the plugin, current and prior. Material changes to this policy are versioned via git — the file's commit history is the source of truth for what changed and when.

## 2. What the plugin reads on your machine

The plugin reads only two locations on your computer:

- **Claude Code conversation logs** under `~/.claude/projects/`. These are the JSONL transcripts Claude Code writes for every session. They are the sole data source for every report the plugin produces.
- **Its own parse cache** at `~/.cache/session-metrics/parse/`. Cache blobs are gzipped JSON dumps of parsed JSONL entries, keyed by the source file's mtime and the script version. The cache speeds up re-runs on unchanged sessions; it never contains data the plugin didn't already read from the JSONL files in the previous step.

Path validation is enforced in code: resolved paths are asserted to live under `~/.claude/projects/` before any read, and CLI inputs (`--session`, `--slug`) are validated against strict allowlists with `..` rejection.

## 3. What the plugin writes on your machine

The plugin writes report files (text, JSON, CSV, Markdown, HTML) into the output directory you choose. The default is `exports/session-metrics/` under the current project root; `--output-dir` overrides it. The plugin writes nothing outside the directory you specify, the parse-cache directory above, and standard output / standard error.

## 4. What the plugin sends over the network

**Nothing on every default code path.** The vendored chart libraries (Highcharts, uPlot, Chart.js) are bundled into the plugin payload and verified against `manifest.json` SHA-256 hashes before being inlined into the rendered HTML. There is no CDN fetch and no remote `<script>` tag in any output file.

There is exactly one exception: the optional `--count-tokens-only` mode calls Anthropic's `POST /v1/messages/count_tokens` endpoint **with your own API key, supplied via the `ANTHROPIC_API_KEY` environment variable**. This mode is opt-in, separately billed against your API account, and explicitly documented as **not** the right tool for subscription-plan users. If you never pass `--count-tokens-only`, this code path is not exercised and no network request is made.

## 5. What the plugin does NOT do

- No telemetry, no analytics, no usage pings.
- No error reporting, no crash reporting, no remote logging.
- No auto-update against any server (versioning is delivered through `git pull` on the marketplace repo, which is initiated by the user via `/plugin update` or equivalent).
- No data sent to the maintainer or any third party.
- No advertising identifiers, no tracking pixels, no cookies — the plugin doesn't have a server to set them.

The maintainer cannot see whether anyone is using the plugin or how. There is no way for me to learn anything about your sessions from your installation of this plugin.

## 6. Third-party components

The plugin bundles three chart-rendering libraries inside its repository:

| Library | License | Where it runs |
|---------|---------|---------------|
| Highcharts | Non-commercial-free (Highsoft licence — commercial use requires a paid licence) | Inside the rendered HTML, opened from `file://` |
| uPlot | MIT | Same |
| Chart.js | MIT | Same |

All three render entirely client-side inside the HTML report. Because the HTML is opened from a `file://` URL and contains no remote `<script>` tags, the bundled libraries make no outbound network requests when you view a report. The default renderer is Highcharts; pass `--chart-lib uplot`, `--chart-lib chartjs`, or `--chart-lib none` to choose an MIT alternative or omit charts entirely.

The plugin runs on top of Python's standard library and Claude Code's bundled `uv` runtime. It declares no PyPI dependencies.

## 7. Data retention and deletion

Everything the plugin stores is local and under your control:

- **Reports** — delete the output directory you specified (default: `exports/session-metrics/`) to remove every generated report.
- **Parse cache** — delete `~/.cache/session-metrics/` to wipe the cache. The cache is rebuilt automatically on the next run, or skipped entirely with `--no-cache`.
- **JSONL conversation logs under `~/.claude/projects/`** are owned by Claude Code, not by this plugin. Refer to Anthropic's documentation for retention controls on the source data.

The plugin keeps no other state.

## 8. Children and sensitive data

`session-metrics` is a developer tool that operates only on the user's own Claude Code session logs. It does not solicit input from third parties, does not process data on behalf of anyone other than the operator, and is not directed at children.

## 9. Security

- All CLI string inputs that influence file paths are validated against strict allowlists with `..` rejection.
- All resolved file paths are asserted to live under `~/.claude/projects/` before being read.
- Vendored chart files are SHA-256 verified against `manifest.json` on every render; on hash mismatch, the file is refused and a stderr warning is emitted.

Report security issues by opening a private security advisory at [github.com/centminmod/claude-plugins/security](https://github.com/centminmod/claude-plugins/security), or — for non-sensitive reports — via the issue tracker at [github.com/centminmod/claude-plugins/issues](https://github.com/centminmod/claude-plugins/issues).

## 10. Changes to this policy

This file is versioned via git. The commit history at [github.com/centminmod/claude-plugins/commits/master/plugins/session-metrics/PRIVACY.md](https://github.com/centminmod/claude-plugins/commits/master/plugins/session-metrics/PRIVACY.md) is the canonical record of changes. Material updates will also be noted in the plugin's [CHANGELOG](https://github.com/centminmod/claude-plugins/blob/master/plugins/session-metrics/CHANGELOG.md).

## 11. Contact

The fastest way to reach the maintainer is the marketplace repo's issue tracker:

- General questions / bug reports: [github.com/centminmod/claude-plugins/issues](https://github.com/centminmod/claude-plugins/issues)
- Security reports: [github.com/centminmod/claude-plugins/security](https://github.com/centminmod/claude-plugins/security) (private advisory)
- Marketplace repo: [github.com/centminmod/claude-plugins](https://github.com/centminmod/claude-plugins)
