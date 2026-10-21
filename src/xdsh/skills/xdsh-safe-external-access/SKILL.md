---
name: xdsh-safe-external-access
description: Use Web, MCP, browser, or media capabilities only through XDSH's authenticated allowlisted boundary with bounded resources and credential-safe output.
---

# Safe external access

- Use only an explicitly enabled XDSH capability and its advertised tools;
  absence of a capability is a normal fail-closed result.
- Keep destinations on the configured host/port allowlist. Treat redirects,
  DNS changes, private or metadata addresses, unexpected MIME, oversized
  responses, and schema mismatches as rejection conditions.
- Refer to credentials by the configured environment reference. Never ask for,
  print, persist, or place a secret in a prompt, URL, Skill, or progress card.
- Keep downloads, decompression, pages, tool arguments, response size, and
  time bounded. Do not unpack untrusted archives into a workspace.
- Record only content-free operation status and cite the returned provenance;
  do not bypass the gateway with SSH, Docker, system services, or raw upstream
  endpoints.
