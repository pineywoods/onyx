import os
from typing import Any

import docker
from typing_extensions import override

from onyx.chat.emitter import Emitter
from onyx.server.query_and_chat.placement import Placement
from onyx.server.query_and_chat.streaming_models import DockerStatusStart
from onyx.server.query_and_chat.streaming_models import Packet
from onyx.tools.interface import Tool
from onyx.tools.models import ToolResponse
from onyx.utils.logger import setup_logger

logger = setup_logger()

# api_server reaches the Docker daemon only through the read-only
# docker-socket-proxy sidecar (CONTAINERS=1); it never mounts the raw socket.
DEFAULT_DOCKER_HOST = "tcp://docker-socket-proxy:2375"

# Keep the client from hanging if the proxy is unreachable.
DOCKER_CLIENT_TIMEOUT_SECONDS = 10


def _format_ports(ports: list[dict[str, Any]]) -> str:
    """Render the /containers/json `Ports` list as a compact string."""
    rendered: list[str] = []
    seen: set[str] = set()
    for port in ports or []:
        private = port.get("PrivatePort")
        public = port.get("PublicPort")
        proto = port.get("Type", "tcp")
        mapping = f"{public}->{private}/{proto}" if public else f"{private}/{proto}"
        if mapping not in seen:
            seen.add(mapping)
            rendered.append(mapping)
    return ", ".join(rendered) if rendered else "-"


class DockerStatusTool(Tool[None]):
    NAME = "list_docker_containers"
    DISPLAY_NAME = "Docker Status"
    DESCRIPTION = (
        "List the Docker containers currently running on the host, including each "
        "container's name, image, status, and published ports. Use this when the "
        "user asks what containers or services are running."
    )

    def __init__(self, tool_id: int, emitter: Emitter) -> None:
        super().__init__(emitter=emitter)
        self._id = tool_id

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }

    @override
    def emit_start(self, placement: Placement) -> None:
        self.emitter.emit(Packet(placement=placement, obj=DockerStatusStart()))

    @override
    def run(
        self,
        placement: Placement,  # noqa: ARG002
        override_kwargs: None,  # noqa: ARG002
        **llm_kwargs: Any,  # noqa: ARG002
    ) -> ToolResponse:
        base_url = os.environ.get("DOCKER_HOST", DEFAULT_DOCKER_HOST)
        client: docker.DockerClient | None = None
        try:
            client = docker.DockerClient(
                base_url=base_url, timeout=DOCKER_CLIENT_TIMEOUT_SECONDS
            )
            # Read the raw /containers/json summary so we never trigger the extra
            # /images/<id> lookups that .containers.list() would (the proxy only
            # grants CONTAINERS access).
            summaries = client.api.containers(all=False)
        except Exception as exc:
            logger.error("DockerStatusTool failed to query Docker at %s: %s", base_url, exc)
            return ToolResponse(
                rich_response=None,
                llm_facing_response=(
                    f"Unable to query the Docker daemon at {base_url}: {exc}"
                ),
            )
        finally:
            if client is not None:
                client.close()

        if not summaries:
            return ToolResponse(
                rich_response=None,
                llm_facing_response="No running Docker containers were found.",
            )

        lines = [f"{len(summaries)} running container(s):"]
        for summary in summaries:
            names = summary.get("Names") or []
            name = names[0].lstrip("/") if names else summary.get("Id", "")[:12]
            image = summary.get("Image", "<unknown>")
            status = summary.get("Status", summary.get("State", ""))
            ports = _format_ports(summary.get("Ports", []))
            lines.append(f"- {name} | image: {image} | status: {status} | ports: {ports}")

        return ToolResponse(
            rich_response=None,
            llm_facing_response="\n".join(lines),
        )
