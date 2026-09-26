from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator


class NetBoxConfig(BaseModel):
    """Configuration for NetBox API connection"""

    netbox_url: str = Field(
        ...,
        description="NetBox base URL, e.g. https://netbox.example.com",
    )
    token: str | None = Field(
        None,
        description="NetBox API token. v2 tokens (nbt_<key>.<token>) are sent as "
        "'Bearer', legacy v1 tokens as 'Token'",
    )
    # Connection settings
    verify_ssl: bool = Field(True, description="Verify SSL certificates (true/false)")
    timeout: float = Field(
        30,
        gt=0,
        description="Total time allowed for one request, retries included (seconds)",
    )
    branching_enabled: bool = Field(
        False,
        description="Enable netbox-branching support: branch tools and the 'branch' "
        "argument (requires the netbox-branching plugin)",
    )
    default_branch: str | None = Field(
        None,
        description="Default netbox-branching branch (name or schema ID) for every call",
    )
    plugin_discovery: bool = Field(
        False,
        description="Discover plugin object types from /api/core/object-types/",
    )
    # Server behavior
    read_only_mode: bool = Field(False, description="Read-only mode (true/false)")
    dry_run_mode: bool = Field(
        False,
        description="Dry-run mode: write tools only simulate changes (true/false)",
    )
    disabled_tags: set[str] = Field(
        default_factory=set, description="Set of tags to disable tools for"
    )
    response_max_size: int | None = Field(
        None,
        ge=1,
        description="Maximum size of a tool response in bytes (unset = unlimited)",
    )
    # Rate limiting
    rate_limit_enabled: bool = Field(
        False, description="Enable rate limiting (true/false)"
    )
    rate_limit_max_requests: int = Field(60, description="Maximum requests per window")
    rate_limit_window_minutes: int = Field(
        1, description="Rate limit window in minutes"
    )
    # Tool search transform
    tool_search_enabled: bool = Field(
        False, description="Enable FastMCP tool-search transform (true/false)"
    )
    tool_search_strategy: Literal["bm25", "regex"] = Field(
        "bm25",
        description="Tool search strategy: 'bm25' (natural language) or 'regex'",
    )
    tool_search_max_results: int = Field(
        5,
        ge=1,
        description="Maximum number of tools returned by search_tools",
    )

    @model_validator(mode="after")
    def _branch_needs_branching(self) -> "NetBoxConfig":
        # A default branch with branching off would be silently ignored, sending
        # every change to main while the operator believes it is staged.
        if self.default_branch and not self.branching_enabled:
            raise ValueError(
                "NETBOX_BRANCH is set but branching is disabled - set "
                "NETBOX_BRANCHING_ENABLED=true (requires the netbox-branching plugin)"
            )
        return self


class TransportConfig(BaseModel):
    """Configuration for MCP transport layer"""

    transport_type: str = Field(
        "stdio",
        description="Transport type: 'stdio', 'sse' (Server-Sent Events), or 'http' (HTTP Streamable)",
    )
    # HTTP transport settings (for both SSE and HTTP Streamable)
    http_host: str = Field(
        "127.0.0.1",
        description="Host to bind for HTTP transports (SSE/HTTP Streamable)",
    )
    http_port: int = Field(
        8000, description="Port to bind for HTTP transports (SSE/HTTP Streamable)"
    )
    http_bearer_token: str | None = Field(
        None, description="Bearer token for HTTP authentication"
    )
