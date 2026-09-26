# NetBox MCP Server

<!-- mcp-name: io.github.mhajder/netbox-mcp -->

NetBox MCP Server is a Python-based Model Context Protocol (MCP) server that gives AI assistants structured access to [NetBox](https://netbox.dev) - the source of truth for network infrastructure (DCIM and IPAM). A small set of generic tools covers all 139 core object types of NetBox 4.7 (and plugin types, if enabled), plus specialised tools for search, change logs, GraphQL, free IP/prefix/VLAN allocation, cable tracing, config rendering and (optionally) netbox-branching. Writes can be hidden entirely (read-only mode) or simulated (dry-run mode).

## Features

### Core Features

- Query any NetBox object type with filtering, ordering and pagination through one tool
- Keep responses small with `fields`, `omit` and `brief` - the tools steer the model to use them
- Search many object types at once, with explicit reporting of failed and truncated types
- Read the change log (who changed what, when) and NetBox status/version/plugins
- Run read-only GraphQL queries for nested data in one round trip
- List free IPs, prefixes, VLANs and ASNs; trace cable paths; render device configs

### Management Operations

- Create, update and delete any object type, singly or in bulk (NetBox runs a bulk write as one transaction)
- Allocate the next free IP, prefix, VLAN or ASN atomically
- Optionally stage changes in a netbox-branching branch instead of main
- Describe an object type's writable fields and choices before writing

### Advanced Capabilities

- Read-only mode hides every write tool; dry-run mode turns every write into a preview
- NetBox v2 (`Bearer nbt_...`) and legacy v1 (`Token ...`) API tokens
- Plugin object type discovery (e.g. `netbox_dns.zone`)
- Filter validation that rejects filters NetBox would silently ignore (`__in`, multi-hop lookups)
- Retries on 429/5xx without ever replaying a create after a server error
- Tag-based tool filtering, optional tool-search transform, rate limiting and response size limit
- Bearer token authentication for HTTP transport
- Multiple transport options (STDIO, SSE, HTTP)
- Optional Sentry integration for error tracking

## Installation

### Prerequisites

- Python 3.11 to 3.14
- A NetBox instance, version 4.2 or newer. Features added later (`omit`, v2
  tokens, `/api/authentication-check/`) are used when the server detects them
- A NetBox API token with the permissions you want the assistant to have. The
  token is the real security boundary: a read-only token cannot write, whatever
  this server's settings are

### Quick Install from PyPI

The easiest way to get started is to install from PyPI:

```sh
# Using UV (recommended)
uvx netbox-mcp

# Or using pip
pip install netbox-mcp
```

Remember to configure the environment variables for your NetBox instance before running the server:

```sh
# Create environment configuration
export NETBOX_URL=https://netbox.example.com
export NETBOX_TOKEN=nbt_your-key.your-token
```

### Install from Source

1. Clone the repository:

```sh
git clone https://github.com/mhajder/netbox-mcp.git
cd netbox-mcp
```

2. Install dependencies:

```sh
# Using UV (recommended)
uv sync

# Or using pip
pip install -e .
```

3. Configure environment variables:

```sh
cp .env.example .env
# Edit .env with your NetBox URL and API token
```

4. Run the server:

```sh
# Using UV (recommended)
uv run netbox-mcp

# Or using the installed command directly
netbox-mcp
```

### Using Docker

A Docker image is available on GitHub Packages for easy deployment.

```sh
docker pull ghcr.io/mhajder/netbox-mcp:latest
```

### Development Setup

For development with additional tools:

```sh
# Clone and install with development dependencies
git clone https://github.com/mhajder/netbox-mcp.git
cd netbox-mcp
uv sync --group dev

# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/

# Run linting and formatting
uv run ruff check .
uv run ruff format .

# Run type checking
uv run ty check .

# Setup prek hooks
uv run prek install
```


## Configuration

### Environment Variables

```env
# NetBox MCP Server Environment Configuration

# NetBox Connection Details
NETBOX_URL=https://netbox.example.com

# API Token - v2 tokens (nbt_<key>.<token>, NetBox 4.5+) are sent as "Bearer",
# legacy v1 tokens as "Token"
NETBOX_TOKEN=nbt_your-key.your-token

# SSL Configuration
NETBOX_VERIFY_SSL=true
# Total time allowed for one NetBox request, retries included (seconds)
NETBOX_TIMEOUT=30

# Branching (requires the netbox-branching plugin)
# Set NETBOX_BRANCHING_ENABLED true to add the branch tools and the 'branch'
# argument to every tool. Off by default - leave it off on a NetBox without the plugin
NETBOX_BRANCHING_ENABLED=false
# Default branch (name or schema ID) used by every call; empty = main.
# Requires NETBOX_BRANCHING_ENABLED=true
NETBOX_BRANCH=

# Plugin Discovery
# Set NETBOX_PLUGIN_DISCOVERY true to expose plugin models (e.g. netbox_dns.zone)
NETBOX_PLUGIN_DISCOVERY=false

# Read-Only Mode
# Set READ_ONLY_MODE true to hide all write tools (create, update, delete)
READ_ONLY_MODE=false

# Dry-Run Mode
# Set DRY_RUN_MODE true to make every write tool only simulate the change:
# it returns the request it would send and never modifies NetBox
DRY_RUN_MODE=false

# Disabled Tags
# Comma-separated list of tags to disable tools for (empty by default)
# Example: DISABLED_TAGS=delete,graphql,branching
DISABLED_TAGS=

# Logging Configuration
LOG_LEVEL=INFO

# Response Size Limit
# Maximum tool response size in bytes; larger responses are truncated (empty = unlimited)
RESPONSE_MAX_SIZE=

# Rate Limiting
# Set RATE_LIMIT_ENABLED true to enable rate limiting
RATE_LIMIT_ENABLED=false
RATE_LIMIT_MAX_REQUESTS=60
RATE_LIMIT_WINDOW_MINUTES=1

# Tool Search Transform (Optional)
# Set TOOL_SEARCH_ENABLED true to replace full tool listings with search_tools + call_tool
TOOL_SEARCH_ENABLED=false
# Search strategy: bm25 (natural language) or regex (pattern match)
TOOL_SEARCH_STRATEGY=bm25
# Maximum number of tools returned by search_tools
TOOL_SEARCH_MAX_RESULTS=5

# Sentry Error Tracking (Optional)
# Set SENTRY_DSN to enable error tracking and performance monitoring
# SENTRY_DSN=https://your-key@o12345.ingest.us.sentry.io/6789
# Optional Sentry configuration
# SENTRY_TRACES_SAMPLE_RATE=1.0
# SENTRY_SEND_DEFAULT_PII=true
# SENTRY_ENVIRONMENT=production
# SENTRY_RELEASE=1.2.3
# SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0
# SENTRY_PROFILE_LIFECYCLE=trace
# SENTRY_ENABLE_LOGS=true

# MCP Transport Configuration
# Transport type: 'stdio' (default), 'sse' (Server-Sent Events), or 'http' (HTTP Streamable)
# MCP_TRANSPORT=stdio

# HTTP Transport Settings (used when MCP_TRANSPORT=sse or MCP_TRANSPORT=http)
# Host to bind the HTTP server (default: 127.0.0.1)
# MCP_HTTP_HOST=127.0.0.1
# Port to bind the HTTP server (default: 8000)
# MCP_HTTP_PORT=8000
# Optional bearer token for authentication (leave empty for no auth)
# MCP_HTTP_BEARER_TOKEN=
```

## Available Tools

Object types are named `app.model`, as NetBox itself names them (`dcim.device`, `ipam.prefix`, `virtualization.virtualmachine`). API paths such as `dcim/devices` are accepted too. With branching enabled, every tool that reads or writes objects also takes an optional `branch` argument.

### Objects (read)

| Tool | Description |
|---|---|
| `netbox_list_object_types` | List supported object types and their endpoints (core and discovered plugin types) |
| `netbox_get_objects` | List objects of a type with `filters`, `fields`, `omit`, `brief`, `ordering`, `limit`, `offset` |
| `netbox_get_object` | Get one object by ID |
| `netbox_search_objects` | Free-text search (`q`) across several object types at once |
| `netbox_describe_object_type` | Writable fields of a type: data type, required, choices |
| `netbox_get_changelogs` | Change log entries, newest first |

### Specialised (read)

| Tool | Description |
|---|---|
| `netbox_get_status` | NetBox version, plugins, workers, authenticated user and this server's modes |
| `netbox_graphql_query` | Read-only GraphQL query (mutations are rejected) |
| `netbox_get_available` | Free IPs / prefixes in a prefix or IP range, VLANs in a VLAN group, ASNs in an ASN range |
| `netbox_trace_cable` | Full cable path from an interface, front/rear/console/power port or circuit termination |
| `netbox_render_config` | Render a device's or VM's configuration from its config template |
| `netbox_list_branches` | List netbox-branching branches (only with `NETBOX_BRANCHING_ENABLED=true`) |

### Write

| Tool | Description |
|---|---|
| `netbox_create_object` | Create one object |
| `netbox_update_object` | Partially update one object (PATCH) |
| `netbox_bulk_create_objects` | Create up to 500 objects in one transaction |
| `netbox_bulk_update_objects` | Update up to 500 objects in one transaction |
| `netbox_allocate_available` | Allocate the next free IP, prefix, VLAN or ASN |
| `netbox_create_branch` | Create a netbox-branching branch (only with `NETBOX_BRANCHING_ENABLED=true`) |
| `netbox_delete_object` | Delete one object (`destructiveHint`) |
| `netbox_bulk_delete_objects` | Delete up to 500 objects in one transaction (`destructiveHint`) |

### Tool Tags

Every tool is tagged `netbox` plus a group tag, which `DISABLED_TAGS` can switch off: `objects`, `search`, `changelog`, `status`, `graphql`, `ipam`, `dcim`, `config`, `branching`, `write` and `delete`. Read tools also carry `read-only`.

## Security & Safety Features

### Read-Only Mode

Read-only mode hides every write tool - the client only sees the 11 read tools (12 with branching enabled):

```env
READ_ONLY_MODE=true
```

### Dry-Run Mode

Dry-run mode keeps the write tools visible, but none of them changes NetBox. Each one resolves and validates its target, then returns the request it would have sent, flagged `"dry_run": true`. Updates and deletes also fetch the current object, so the preview shows the values that would change and exactly what would be deleted:

```env
DRY_RUN_MODE=true
```

`READ_ONLY_MODE` takes precedence: when both are set, write tools are hidden.

### NetBox Branching

With the [netbox-branching](https://github.com/netboxlabs/netbox-branching) plugin, changes can be staged in a branch and reviewed before they are merged into main. Branching support is **off by default**, so a NetBox without the plugin gets no branch tools and no `branch` argument. Enable it with:

```env
NETBOX_BRANCHING_ENABLED=true
```

This adds `netbox_list_branches` and `netbox_create_branch`, and a `branch` argument (a branch name or its schema ID) to every other tool. To send every call to one branch by default:

```env
NETBOX_BRANCH=assistant-changes
```

Setting `NETBOX_BRANCH` without `NETBOX_BRANCHING_ENABLED=true` is a configuration error: the server refuses to start rather than silently writing to main.

Branch names are resolved to the schema ID that the `X-NetBox-Branch` header needs. Merging, syncing and reverting are deliberately not exposed as tools - they belong in a human review step.

### Plugin Discovery

```env
NETBOX_PLUGIN_DISCOVERY=true
```

On first use the server reads `/api/core/object-types/` and adds every plugin model with a REST endpoint (for example `netbox_dns.zone`). Plugin types never shadow core types. If discovery fails, the core types keep working.

### Tag-Based Tool Filtering

You can disable specific categories of tools by setting disabled tags. For example, allow creates and updates but never deletes:

```env
DISABLED_TAGS=delete
```

### Response Size Limit

Large NetBox objects add up quickly. `RESPONSE_MAX_SIZE` truncates any tool response above the given number of bytes:

```env
RESPONSE_MAX_SIZE=200000
```

### Tool Search for Large Toolsets

FastMCP tool search can reduce prompt size for servers with many tools.
When enabled, `list_tools` returns two synthetic tools:

- `search_tools`: Finds matching tools and returns their full schemas
- `call_tool`: Executes any discovered tool by name

Enable it with:

```env
TOOL_SEARCH_ENABLED=true
TOOL_SEARCH_STRATEGY=bm25      # bm25 or regex
TOOL_SEARCH_MAX_RESULTS=8      # optional, default is 5
```

`bm25` supports natural language queries, while `regex` uses a regex
`pattern` input for deterministic matching.

Tool search respects existing visibility controls (read-only mode and
disabled tags).

### Rate Limiting

The server supports rate limiting to control API usage and prevent abuse. If enabled, requests are limited per client using a sliding window algorithm.

Enable rate limiting by setting the following environment variables in your `.env` file:

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_MAX_REQUESTS=100   # Maximum requests allowed per window
RATE_LIMIT_WINDOW_MINUTES=1   # Window size in minutes
```

If `RATE_LIMIT_ENABLED` is set to `true`, the server will apply rate limiting middleware. Adjust `RATE_LIMIT_MAX_REQUESTS` and `RATE_LIMIT_WINDOW_MINUTES` as needed for your environment.

### Sentry Error Tracking & Monitoring (Optional)

The server optionally supports **Sentry** for error tracking, performance monitoring, and debugging. Sentry integration is completely optional and only initialized if configured.

#### Installation

To enable Sentry monitoring, install the optional dependency:

```sh
# Using UV (recommended)
uv sync --extra sentry
```

#### Configuration

Enable Sentry by setting the `SENTRY_DSN` environment variable in your `.env` file:

```env
# Required: Sentry DSN for your project
SENTRY_DSN=https://your-key@o12345.ingest.us.sentry.io/6789

# Optional: Performance monitoring sample rate (0.0-1.0, default: 1.0)
SENTRY_TRACES_SAMPLE_RATE=1.0

# Optional: Include personally identifiable information (default: true)
SENTRY_SEND_DEFAULT_PII=true

# Optional: Environment name (e.g., "production", "staging")
SENTRY_ENVIRONMENT=production

# Optional: Release version (auto-detected from package if not set)
SENTRY_RELEASE=1.2.3

# Optional: Profiling - continuous profiling sample rate (0.0-1.0, default: 1.0)
SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0

# Optional: Profiling - lifecycle mode for profiling (default: "trace")
# Options: "all", "continuation", "trace"
SENTRY_PROFILE_LIFECYCLE=trace

# Optional: Enable log capture as breadcrumbs and events (default: true)
SENTRY_ENABLE_LOGS=true
```

#### Features

When enabled, Sentry automatically captures:

- **Exceptions & Errors**: All unhandled exceptions with full context
- **Performance Metrics**: Request/response times and traces
- **MCP Integration**: Detailed MCP server activity and interactions
- **Logs & Breadcrumbs**: Application logs and event trails for debugging
- **Context Data**: Environment, client info, and request parameters

#### Getting a Sentry DSN

1. Create a free account at [sentry.io](https://sentry.io)
2. Create a new Python project
3. Copy your DSN from the project settings
4. Set it in your `.env` file

#### Disabling Sentry

Sentry is completely optional. If you don't set `SENTRY_DSN`, the server will run normally without any Sentry integration, and no monitoring data will be collected.

### SSL/TLS Configuration

The server supports SSL certificate verification and custom timeout settings:

```env
NETBOX_VERIFY_SSL=true    # Enable SSL certificate verification
NETBOX_TIMEOUT=30         # Total time per request, retries included (seconds)
```

Certificates are verified against the operating system trust store, so a NetBox behind an internal CA works as soon as that CA is trusted by the OS. `SSL_CERT_FILE` or `SSL_CERT_DIR` take precedence when set.

### Transport Configuration

The server supports multiple transport mechanisms for the MCP protocol:

#### STDIO Transport (Default)

The default transport uses standard input/output for communication. This is ideal for local usage and integration with tools that communicate via stdin/stdout:

```env
MCP_TRANSPORT=stdio
```

#### HTTP SSE Transport (Server-Sent Events)

For network-based deployments, you can use HTTP with Server-Sent Events. This allows the MCP server to be accessed over HTTP with real-time streaming:

```env
MCP_TRANSPORT=sse
MCP_HTTP_HOST=127.0.0.1        # Localhost
MCP_HTTP_PORT=8000           # Port to listen on
MCP_HTTP_BEARER_TOKEN=your-secret-token  # Optional authentication token
```

When using SSE transport with a bearer token, clients must include the token in their requests:

```bash
curl -H "Authorization: Bearer your-secret-token" http://localhost:8000/sse
```

#### HTTP Streamable Transport

The HTTP Streamable transport provides HTTP-based communication with request/response streaming. This is ideal for web integrations and tools that need HTTP endpoints:

```env
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1        # Localhost
MCP_HTTP_PORT=8000           # Port to listen on
MCP_HTTP_BEARER_TOKEN=your-secret-token  # Optional authentication token
```

When using streamable transport with a bearer token:

```sh
curl -H "Authorization: Bearer your-secret-token" \
     -H "Accept: application/json, text/event-stream" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://localhost:8000/mcp
```

**Note**: The HTTP transport requires proper JSON-RPC formatting with `jsonrpc` and `id` fields. The server may also require session initialization for some operations.

For more information on FastMCP transports, see the [FastMCP documentation](https://gofastmcp.com/deployment/running-server#transport-protocols).

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests and ensure code quality (`uv run pytest && uv run ruff check .`)
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

## License

MIT License - see LICENSE file for details.
