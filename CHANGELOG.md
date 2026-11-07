# Changelog

## [0.3.0](https://github.com/mhajder/netbox-mcp/compare/v0.2.1...v0.3.0) (2026-11-07)


### 🚀 Features

* **branching:** point netbox_list_branches at staged change diffs ([2ff5356](https://github.com/mhajder/netbox-mcp/commit/2ff53567ea3dd4cd4253615b6f9ffb93fc7f6927))
* **tools:** add script, cable, rack space and prefix utilization tools ([d56a6a1](https://github.com/mhajder/netbox-mcp/commit/d56a6a1a0ef126f61d04dbe288f90d25258b010f))


### 🐛 Bug Fixes

* **graphql:** explain an empty or non-object GraphQL response ([1e4474e](https://github.com/mhajder/netbox-mcp/commit/1e4474e6638f6940243fd4764591a1ef73ebade0))


### 🧩 CI

* pin actions to commit SHAs at latest versions ([9334b32](https://github.com/mhajder/netbox-mcp/commit/9334b32c36e1fdffafdbb9974bdc539b360c7045))
* release with release-please ([23e0ffe](https://github.com/mhajder/netbox-mcp/commit/23e0ffeb952cf45e862f7ffea9dd4bbfede4c7ce))
* replace softprops/action-gh-release with the gh CLI ([e619d7e](https://github.com/mhajder/netbox-mcp/commit/e619d7eb37cf45f48336e02e81335895a857b285))


### 📦 Build

* **deps-dev:** bump the uv-dev-dependencies group across 1 directory with 4 updates ([#21](https://github.com/mhajder/netbox-mcp/issues/21)) ([db422bb](https://github.com/mhajder/netbox-mcp/commit/db422bba97cbef75f7d43f90c564d66cae90df09))
* **deps:** bump authlib from 1.8.0 to 1.9.1 ([#19](https://github.com/mhajder/netbox-mcp/issues/19)) ([a843b48](https://github.com/mhajder/netbox-mcp/commit/a843b486a11098aafdeb61cc399cd391d810878d))
* **deps:** bump joserfc from 1.7.5 to 1.7.6 ([#18](https://github.com/mhajder/netbox-mcp/issues/18)) ([5a8b4b6](https://github.com/mhajder/netbox-mcp/commit/5a8b4b6e3fd4327f0b5636ce36b12801568d1d72))
* **deps:** bump the uv-dependencies group across 1 directory with 4 updates ([#16](https://github.com/mhajder/netbox-mcp/issues/16)) ([a56eee1](https://github.com/mhajder/netbox-mcp/commit/a56eee1e69c36e9791f760e80bcccec0413fe942))
* **deps:** bump ty to 0.0.98 and type check for all platforms ([ff67c15](https://github.com/mhajder/netbox-mcp/commit/ff67c15f6a9d982b05de71e2af6e7009097e8ce1))
* **deps:** raise minimum versions of dev tools and vulnerable dependencies ([d6ee1be](https://github.com/mhajder/netbox-mcp/commit/d6ee1bebe0b8f6751279bb2add88b4d4a04527a9))
* **deps:** stop Dependabot proposing Python pre-release and minor bumps ([fea802c](https://github.com/mhajder/netbox-mcp/commit/fea802c70c7e0fb1b28a8a41f64fc385a19128bf))
* **docker:** pin base images by version and digest and update them with Dependabot ([5055615](https://github.com/mhajder/netbox-mcp/commit/505561509f76a5ec2b95993330f7a522cf9b8ad7))
* install optional extras for ty check in pre-commit hook ([3240808](https://github.com/mhajder/netbox-mcp/commit/324080882c1265e39ed9055236b60aa2a6685dbb))


### 🧪 Tests

* raise line coverage to 99.9% ([b01119b](https://github.com/mhajder/netbox-mcp/commit/b01119b47612ca18208a728aeae75e7b90acdf41))

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
