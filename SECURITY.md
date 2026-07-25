<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Security Policy

## Supported Versions

Until CodeNib reaches a stable release, security fixes are applied to the
latest `0.1.x` release and current `main`.

| Version | Supported |
|---|---|
| `0.1.x` | Yes |
| Earlier snapshots | No |

## Report A Vulnerability

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/sysevol-ai/CodeNib/security/advisories/new)
or email `zhy025@ucsd.edu` when private reporting is unavailable.

Include the affected version or commit, impact, reproduction steps, and any
known mitigation. Maintainers will acknowledge a complete report within seven
days and will coordinate disclosure after a fix or mitigation is available.

## Scope

Security reports may cover the Python package, CLI, Wiki backend/frontend,
MCP server, release workflow, or unsafe handling of untrusted repository
content. Model output quality and unsupported third-party language servers are
not vulnerabilities by themselves, but a CodeNib boundary violation involving
those components is in scope.
