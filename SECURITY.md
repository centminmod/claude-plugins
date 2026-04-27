# Security policy

## Reporting a vulnerability

**Do NOT open a public issue for security vulnerabilities.**

If you believe you have found a security issue in any plugin distributed
through this marketplace — for example, a way to trigger arbitrary code
execution, exfiltrate user data, or escalate privileges via a plugin's
hooks, commands, or skills — please report it privately through GitHub's
Private Vulnerability Reporting:

→ <https://github.com/centminmod/claude-plugins/security/advisories/new>

Please include:

- Which plugin and version are affected.
- A clear description of the issue and its impact.
- Reproduction steps or a proof-of-concept.
- Any logs or transcript excerpts (redact secrets, PII, file paths,
  repo names, and proprietary code before sending).

## Out of scope

The following belong in the public issue tracker, not in a security
advisory:

- Functional bugs that don't have a security impact.
- Feature requests.
- Documentation errors.
- Crashes or error messages with no security consequence.

## Supported versions

Only the latest published version of each plugin is supported. Reports
against older versions will be closed with a request to retest on the
current release.

## Response expectations

This is a personal marketplace maintained by a single person on a
best-effort basis. Acknowledgement of valid reports typically arrives
within a few days; fix timelines depend on severity and the upstream
plugin's maintenance state. There is no formal SLA.

## Disclosure

For confirmed issues, fixes will ship through the normal plugin update
flow (`/plugin update <name>@centminmod`). A GitHub Security Advisory
will be published after a fix is available, crediting the reporter
unless they request anonymity.
