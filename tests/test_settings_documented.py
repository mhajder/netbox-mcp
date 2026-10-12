"""Every setting read from the environment must be documented."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# netbox_client.py reads the settings; utils.py holds the transport helpers
# shared with the container healthcheck.
SOURCES = [
    (ROOT / "src" / "netbox_mcp" / name).read_text()
    for name in ("netbox_client.py", "utils.py")
]
ENV_VARS = sorted(
    {name for src in SOURCES for name in re.findall(r'os\.getenv\("([A-Z_]+)"', src)}
)


def test_env_vars_found():
    assert "NETBOX_URL" in ENV_VARS
    assert "DRY_RUN_MODE" in ENV_VARS
    assert "MCP_HTTP_PORT" in ENV_VARS


@pytest.mark.parametrize("name", ENV_VARS)
def test_env_var_in_env_example(name):
    assert name in (ROOT / ".env.example").read_text()


@pytest.mark.parametrize("name", ENV_VARS)
def test_env_var_in_readme(name):
    assert name in (ROOT / "README.md").read_text()


# The registry manifest configures a local install; binding and HTTP auth are
# left to the operator, as in the MCP registry examples.
REGISTRY_EXCLUDED = {"MCP_HTTP_HOST", "MCP_HTTP_BEARER_TOKEN"}


@pytest.mark.parametrize("name", sorted(set(ENV_VARS) - REGISTRY_EXCLUDED))
def test_env_var_in_server_json(name):
    assert name in (ROOT / "server.json").read_text()
