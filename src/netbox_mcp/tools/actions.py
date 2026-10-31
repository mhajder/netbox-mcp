"""
Task-shaped write tools: run a custom script, connect a cable.

Both carry the ``write`` tag, so READ_ONLY_MODE hides them. With DRY_RUN_MODE
on, each resolves its target and returns the request it would have sent.
"""

import asyncio
from typing import Annotated
from typing import Any
from typing import Literal

import httpx2
from fastmcp import Context
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator

from netbox_mcp.models import NetBoxConfig
from netbox_mcp.netbox_client import NetBoxClient
from netbox_mcp.object_types import endpoint_for
from netbox_mcp.tools.common import CREATE_ANNOTATIONS
from netbox_mcp.tools.common import RUN_ANNOTATIONS
from netbox_mcp.tools.common import BranchArg
from netbox_mcp.tools.common import dry_run_result
from netbox_mcp.tools.common import netbox_tool
from netbox_mcp.tools.common import pick
from netbox_mcp.tools.errors import fail
from netbox_mcp.tools.extras import describe_endpoint

# Job states after which a job will not change again.
JOB_FINAL_STATES = frozenset({"completed", "errored", "failed"})
JOB_FIELDS = ("id", "status", "created", "started", "completed", "error")
# Seconds between job status checks: quick at first, then steady.
POLL_DELAYS = (1, 1, 2)

CABLE_FIELDS_RESERVED = ("a_terminations", "b_terminations")
# Port types that belong to a device, so they can be found by device + name.
DEVICE_COMPONENT_TYPES = (
    "dcim.interface",
    "dcim.frontport",
    "dcim.rearport",
    "dcim.consoleport",
    "dcim.consoleserverport",
    "dcim.powerport",
    "dcim.poweroutlet",
)
CableEndType = Literal[
    "dcim.interface",
    "dcim.frontport",
    "dcim.rearport",
    "dcim.consoleport",
    "dcim.consoleserverport",
    "dcim.powerport",
    "dcim.poweroutlet",
    "dcim.powerfeed",
    "circuits.circuittermination",
]


class TerminationSpec(BaseModel):
    """One end of a cable: a port given by ID, or by its device and name."""

    object_type: Annotated[CableEndType, Field(description="Type of the port.")]
    object_id: Annotated[
        int | None, Field(default=None, ge=1, description="Port ID.")
    ] = None
    device: Annotated[
        int | str | None,
        Field(
            default=None,
            description="Device name or numeric ID (with 'name'; device ports only).",
        ),
    ] = None
    name: Annotated[
        str | None,
        Field(default=None, description="Port name on that device, e.g. 'eth0'."),
    ] = None

    @model_validator(mode="after")
    def _one_way_to_find_it(self) -> "TerminationSpec":
        if self.object_id is not None:
            return self
        if self.device is None or not self.name:
            raise ValueError("Give 'object_id', or both 'device' and 'name'.")
        if self.object_type not in DEVICE_COMPONENT_TYPES:
            raise ValueError(
                f"{self.object_type} cannot be looked up by device and name; "
                "give its 'object_id'."
            )
        return self


TerminationsArg = Annotated[
    list[TerminationSpec],
    Field(
        min_length=1,
        max_length=8,
        description="Ports on this end, usually one, e.g. [{'object_type': "
        "'dcim.interface', 'device': 'sw01', 'name': 'eth1'}] or "
        "[{'object_type': 'dcim.frontport', 'object_id': 42}].",
    ),
]


async def _pause(seconds: float) -> None:
    """Wait between job status checks (replaced in tests)."""
    await asyncio.sleep(seconds)


def _job_status(job: dict[str, Any]) -> str | None:
    """Return a job's status value; NetBox renders it as {value, label}."""
    status = job.get("status")
    if isinstance(status, dict):
        status = status.get("value")
    return status


def _job_result(job: dict[str, Any]) -> dict[str, Any]:
    """Reduce a job to its state and the script's log, output and test results."""
    result: dict[str, Any] = pick(job, JOB_FIELDS)
    result["status"] = _job_status(job)
    data = job.get("data")
    if isinstance(data, dict):
        for key in ("log", "output", "tests"):
            if key in data:
                result[key] = data[key]
    elif data is not None:
        result["data"] = data
    return result


def _port_label(port: dict[str, Any]) -> str:
    """Name a port for an error message, e.g. "sw01 eth1 (dcim.interface 7)"."""
    device = port.get("device")
    device_name = device.get("display") or device.get("name") if device else None
    name = port.get("display") or port.get("name") or "?"
    return f"{device_name} {name}" if device_name else str(name)


async def _resolve_termination(nb: NetBoxClient, spec: TerminationSpec) -> dict:
    """Fetch the port a spec names, and check it is free.

    Raises:
        ValueError: If no port or more than one matches, or it is already cabled.
    """
    endpoint = endpoint_for(spec.object_type)
    if spec.object_id is not None:
        port = await nb.get(f"{endpoint}/{spec.object_id}")
    else:
        device_filter = "device_id" if isinstance(spec.device, int) else "device"
        data = await nb.get(
            endpoint,
            params={device_filter: spec.device, "name": spec.name, "limit": 10},
        )
        rows = (data or {}).get("results", [])
        if not rows:
            raise ValueError(
                f"No {spec.object_type} named {spec.name!r} on device {spec.device!r}."
            )
        if len(rows) > 1:
            found = ", ".join(
                f"{_port_label(row)} (device id {(row.get('device') or {}).get('id')})"
                for row in rows
            )
            raise ValueError(
                f"{len(rows)} ports match {spec.device!r} / {spec.name!r}: {found}. "
                "Pass the device's numeric ID instead."
            )
        port = rows[0]
    cable = port.get("cable")
    if cable:
        cable_name = cable.get("display") or cable.get("id")
        raise ValueError(
            f"{_port_label(port)} ({spec.object_type} {port.get('id')}) is already "
            f"connected via cable {cable_name}. Delete or move that cable first."
        )
    return port


def register_actions_tools(
    mcp, config: NetBoxConfig, transport: httpx2.AsyncBaseTransport | None = None
):
    """Register the script and cable tools with the MCP server"""

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write", "scripts"},
        annotations=RUN_ANNOTATIONS,
    )
    async def netbox_run_script(
        ctx: Context,
        script: Annotated[
            int | str,
            Field(
                description="Script ID, or 'module.ClassName' (e.g. "
                "'cleanup.StaleIPs'). List scripts with netbox_get_objects on "
                "'extras.script'."
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Values for the script's input variables, e.g. "
                "{'site': 3, 'dry_run_note': 'test'}. Object variables take IDs.",
            ),
        ] = None,
        commit: Annotated[
            bool,
            Field(
                default=False,
                description="Keep the script's database changes. False (default) "
                "runs it and rolls its changes back - use that first.",
            ),
        ] = False,
        wait_seconds: Annotated[
            int,
            Field(
                default=30,
                ge=0,
                le=120,
                description="How long to wait for the result. 0 returns as soon "
                "as the job is queued.",
            ),
        ] = 30,
    ) -> dict:
        """Run a NetBox custom script and return its log and output.

        Runs with commit=false (changes rolled back) unless commit is set. The
        script runs as a background job; if it is still running after
        wait_seconds, poll it with netbox_get_object('core.job', job id).
        Confirm with the user before commit=true.
        """
        try:
            script_ref = str(script).strip()
            if not script_ref or "/" in script_ref:
                raise ValueError("'script' must be an ID or 'module.ClassName'.")
            path = f"extras/scripts/{script_ref}"
            payload = {"data": data or {}, "commit": commit}
            async with NetBoxClient(config, transport=transport) as nb:
                if config.dry_run_mode:
                    found = await nb.get(path)
                    return dry_run_result(
                        "run_script",
                        "POST",
                        path,
                        payload,
                        None,
                        script=pick(
                            found, ("id", "display", "name", "description", "vars")
                        ),
                    )
                await ctx.info(f"Running script {script_ref} (commit={commit})...")
                queued = await nb.post(path, payload) or {}
                job: Any = queued.get("result") if isinstance(queued, dict) else None
                if not isinstance(job, dict) or not job.get("id"):
                    return {"success": True, "committed": commit, "response": queued}
                job_id = job["id"]

                waited = 0
                delays = iter(POLL_DELAYS)
                while (
                    _job_status(job) not in JOB_FINAL_STATES and waited < wait_seconds
                ):
                    delay = min(next(delays, POLL_DELAYS[-1]), wait_seconds - waited)
                    await _pause(delay)
                    waited += delay
                    job = await nb.get(f"core/jobs/{job_id}")

            result = {"success": True, "committed": commit, "job": _job_result(job)}
            if _job_status(job) not in JOB_FINAL_STATES:
                result["message"] = (
                    f"Job {job_id} is still {_job_status(job)}. Check it later "
                    f"with netbox_get_object('core.job', {job_id})."
                )
            return result
        except Exception as e:
            await fail(ctx, f"Error running script {script}", e)

    @netbox_tool(
        mcp,
        config,
        tags={"netbox", "write", "dcim"},
        annotations=CREATE_ANNOTATIONS,
    )
    async def netbox_connect_cable(
        ctx: Context,
        a_terminations: TerminationsArg,
        b_terminations: TerminationsArg,
        cable: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Cable attributes, e.g. {'type': 'cat6', 'status': "
                "'connected', 'label': 'C-101', 'color': 'ff0000', 'length': 3, "
                "'length_unit': 'm'}.",
            ),
        ] = None,
        branch: BranchArg = None,
    ) -> dict:
        """Connect ports with a new cable.

        Ports can be given by device and name - no ID lookups needed. Fails
        before writing if a port is not found, is ambiguous or is already
        cabled. Use netbox_trace_cable afterwards to check the path.
        """
        try:
            attributes = dict(cable or {})
            reserved = [key for key in CABLE_FIELDS_RESERVED if key in attributes]
            if reserved:
                raise ValueError(
                    f"Give {', '.join(reserved)} as the tool arguments, not in 'cable'."
                )
            async with NetBoxClient(config, branch, transport=transport) as nb:
                a_ports = [await _resolve_termination(nb, s) for s in a_terminations]
                b_ports = [await _resolve_termination(nb, s) for s in b_terminations]
                payload = {
                    **attributes,
                    "a_terminations": [
                        {"object_type": s.object_type, "object_id": p["id"]}
                        for s, p in zip(a_terminations, a_ports, strict=True)
                    ],
                    "b_terminations": [
                        {"object_type": s.object_type, "object_id": p["id"]}
                        for s, p in zip(b_terminations, b_ports, strict=True)
                    ],
                }
                if config.dry_run_mode:
                    return dry_run_result(
                        "connect_cable",
                        "POST",
                        "dcim/cables",
                        payload,
                        nb.branch,
                        object_type="dcim.cable",
                        a_ends=[describe_endpoint(p) for p in a_ports],
                        b_ends=[describe_endpoint(p) for p in b_ports],
                    )
                await ctx.info("Creating cable...")
                created = await nb.post("dcim/cables", payload)
            return {
                "success": True,
                "object_type": "dcim.cable",
                "object": created,
                "a_ends": [describe_endpoint(p) for p in a_ports],
                "b_ends": [describe_endpoint(p) for p in b_ports],
            }
        except Exception as e:
            await fail(ctx, "Error connecting cable", e)
