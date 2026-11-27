import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SNAPSHOT_PATH = Path("/run/dabro/snapshot.json")


@dataclass(frozen=True)
class Snapshot:
    measured_at: float
    temperature_c: float | None
    cpu_percent: float | None
    memory_used_kib: int | None
    memory_total_kib: int | None
    uptime_seconds: int | None
    lan_ipv4: str | None


def write_snapshot(snapshot: Snapshot, path: Path = SNAPSHOT_PATH) -> None:
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(asdict(snapshot)), encoding="utf-8")
    temporary_path.replace(path)


def read_snapshot(max_age: float, path: Path = SNAPSHOT_PATH) -> Snapshot | None:
    try:
        snapshot = Snapshot(**json.loads(path.read_text(encoding="utf-8")))
        if 0 <= time.monotonic() - snapshot.measured_at <= max_age:
            return snapshot
    except (OSError, ValueError, TypeError):
        return None
    return None
