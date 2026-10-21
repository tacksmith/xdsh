# Model providers behind a TUN fake-IP proxy

XDSH rejects remote model hostnames that resolve to private or special-use
addresses by default. Some operator-controlled TUN proxies return synthetic
IPv4 addresses from `198.18.0.0/15`, while routing HTTPS to the real endpoint.
This can make a valid model connection fail before any upstream request.

Only when the host deliberately uses such a trusted proxy, set this variable
in the **model gateway process environment** and restart it at a safe idle point:

```dotenv
XDSH_PROVIDER_FAKE_IP_HOSTS=api.example.com
```

Use exact hostnames, comma-separated if necessary. No schemes, paths or
wildcards. The default is empty. This is an operator trust decision: the proxy
controls the fake-IP routing. Do not enable it for unknown endpoints or to
bypass an unexplained DNS change. Fix DNS instead when no trusted proxy exists.

The exception permits only the fake-IP range for the named model hostnames.
HTTPS verification remains enabled, redirects remain disabled, and loopback,
RFC1918, link-local/metadata and mixed private DNS answers remain rejected.
Direct fake-IP URLs remain invalid. This does not alter Web/MCP/browser SSRF
policy or grant model-controlled URLs any exception.

Local DNS-policy failures now carry `XDSH_PROVIDER_DNS` with HTTP 503 rather
than masquerading as HTTP 400 model-parameter rejection. Do not change API keys
or reasoning settings as a workaround for this error.

The RC2 offline overlay Dockerfile accepts an optional
`PROVIDER_FAKE_IP_HOSTS` build argument. Generic release builds leave it empty;
deployment-specific values must be recorded and retained across upgrades.
