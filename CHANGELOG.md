## v0.2.1 (2026-10-23)

### Fix

- **deps**: update certifi to 2026.10.19

## v0.2.0 (2026-10-19)

### Feat

- **transport**: accept streamable-http as an alias for http
- add Python 3.15 support
- **auth**: call NetBox with each MCP client's own token
- **health**: serve /health and probe it from the Docker healthcheck

### Fix

- **transport**: stop on an unknown MCP_TRANSPORT instead of running stdio
- **sentry**: send no PII unless SENTRY_SEND_DEFAULT_PII asks for it
- **transport**: ignore surrounding spaces in MCP_TRANSPORT
- **transport**: bind loopback when MCP_HTTP_HOST is blank
- **config**: validate settings of enabled features only
- **config**: treat blank env values as unset
- **config**: reject unrecognized boolean settings
- **sentry**: log the enabled message by initialising after logging
- **docker**: run as numeric UID 10001 with root-owned app files

### Refactor

- **server**: read config attributes directly

## v0.1.0 (2026-09-26)

### Feat

- initialize NetBox MCP Server project
