import json
import socket
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from .snapshot import ContainerSummary


class DockerConnection(HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=3)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def docker_ps(path: Path, limit: int) -> tuple[int | None, list[ContainerSummary] | None]:
    try:
        with closing(DockerConnection(path)) as connection:
            connection.request("GET", "/containers/json?all=0")
            with connection.getresponse() as response:
                if response.status != 200:
                    return None, None
                containers = json.load(response)
        if not isinstance(containers, list):
            return None, None
        brief: list[ContainerSummary] = []
        for container in containers[:limit]:
            names = container.get("Names") or []
            name = names[0].lstrip("/") if names else container["Id"][:12]
            brief.append({
                "name": name,
                "image": container["Image"],
                "status": container.get("Status") or container.get("State", ""),
            })
        return len(containers), brief
    except (OSError, HTTPException, ValueError, AttributeError, KeyError, TypeError):
        return None, None
