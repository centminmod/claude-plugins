# centminmod / claude-plugins

A personal Claude Code plugin marketplace. Each plugin is an independent, standalone unit that can be installed into any Claude Code project with a single command.

> Claude Code's plugin system supports multiple plugins per marketplace and handles install + update lifecycle automatically. See the [official plugin-marketplaces docs](https://code.claude.com/docs/en/plugin-marketplaces) for the broader ecosystem.

## Install the marketplace

Add this marketplace once, then install any plugin from it:

> **Run `/plugin` commands inside the Claude Code terminal CLI**
> (`claude` in your shell). They are not recognised in the desktop
> app, claude.ai/code, or IDE extensions — you'll see *"/plugin isn't
> a recognized command here"* if you try. Installed plugins then work
> from every surface; only the install step requires the CLI.

```
/plugin marketplace add centminmod/claude-plugins
```

## Available plugins

| Plugin | Description | Install |
|--------|-------------|---------|
| [`session-metrics`](plugins/session-metrics) | Per-turn token, cost, and cache metrics for Claude Code sessions. Multi-format export (text/JSON/CSV/MD/HTML) with 5-hour session blocks, weekly roll-up, hour-of-day punchcard, and pluggable chart libraries. | `/plugin install session-metrics@centminmod` |

More plugins coming.

## How the skill gets triggered

Plugin skills are namespaced as `plugin-name:skill-name` — for example
`/session-metrics:session-metrics`. In practice you rarely type that form:
each skill declares natural-language triggers in its `SKILL.md`, so Claude
Code auto-invokes the right one when you ask something like *"how much has
this session cost?"*.

## Licences

- Marketplace scaffold: MIT (see [`LICENSE`](LICENSE)).
- Each plugin under `plugins/*/` carries its own `LICENSE` and may bundle
  third-party assets under their upstream licences. For `session-metrics`
  specifically, the default Highcharts renderer ships under a
  non-commercial-free licence — commercial use requires a paid Highsoft
  licence. MIT-licensed alternatives (uPlot, Chart.js) are selectable via
  the `--chart-lib` flag. See
  [`plugins/session-metrics/skills/session-metrics/scripts/vendor/charts/README.md`](plugins/session-metrics/skills/session-metrics/scripts/vendor/charts/README.md)
  for per-library LICENSE.txt files.

## Contributing

This is a personal marketplace — issues and pull requests are welcome but
plugin additions are curated. Feel free to open an issue to discuss
bundling something new.

## Related

- [centminmod/my-claude-code-setup](https://github.com/centminmod/my-claude-code-setup) — personal Claude Code config template (bundles the same skills for direct copy)
