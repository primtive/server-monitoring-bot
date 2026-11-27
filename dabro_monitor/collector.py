import json
import logging
import os
import subprocess
import time
from ipaddress import IPv4Address
from pathlib import Path

from .config import positive_env
from .snapshot import Snapshot, write_snapshot

PROC = Path("/host/proc")
HWMON = Path("/host/sys/class/hwmon")
LOGGER = logging.getLogger(__name__)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cpu_counters(proc: Path) -> tuple[int, int] | None:
    fields = read_text(proc / "stat").splitlines()
    if not fields:
        return None
    label, *counters = fields[0].split()
    if label != "cpu" or len(counters) < 4:
        return None
    try:
        values = [int(value) for value in counters[:8]]
    except ValueError:
        return None
    return sum(values), sum(values[3:5])


def cpu_usage(
    previous: tuple[int, int] | None, current: tuple[int, int] | None,
) -> float | None:
    if previous is None or current is None:
        return None
    elapsed = current[0] - previous[0]
    if elapsed <= 0:
        return None
    idle = current[1] - previous[1]
    return 100 * (elapsed - min(elapsed, max(0, idle))) / elapsed


def memory_usage(proc: Path) -> tuple[int | None, int | None]:
    values = {}
    for line in read_text(proc / "meminfo").splitlines():
        label, _, value = line.partition(":")
        if label in {"MemTotal", "MemAvailable"}:
            try:
                values[label] = int(value.split()[0])
            except (ValueError, IndexError):
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or not 0 <= available <= total or total == 0:
        return None, None
    return total - available, total


def host_uptime(proc: Path) -> int | None:
    try:
        return int(float(read_text(proc / "uptime").split()[0]))
    except (ValueError, IndexError, OverflowError):
        return None


def cpu_temperature(hwmon: Path) -> float | None:
    readings = []
    for device in hwmon.glob("hwmon*"):
        if read_text(device / "name") != "coretemp":
            continue
        packages, cores = [], []
        for sensor in device.glob("temp*_input"):
            label = read_text(sensor.with_name(sensor.name.replace("_input", "_label")))
            try:
                temperature = int(read_text(sensor)) / 1000
            except ValueError:
                continue
            if label.startswith("Package id "):
                packages.append(temperature)
            elif label.startswith("Core "):
                cores.append(temperature)
        readings.extend(packages or cores)
    return max(readings, default=None)


def ip_query(*arguments: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            check=True, timeout=3,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def iface_addresses(interface: str) -> list[str]:
    addresses = []
    for device in ip_query("address", "show", "dev", interface, "scope", "global"):
        for record in device.get("addr_info", []):
            if record.get("family") != "inet" or not record.get("local"):
                continue
            address = IPv4Address(record["local"])
            if not (address.is_loopback or address.is_link_local or address.is_unspecified):
                addresses.append(str(address))
    return addresses


def lan_ipv4(interface: str) -> str | None:
    if interface:
        return next(iter(iface_addresses(interface)), None)

    routes = ip_query("route", "show", "default")
    for route in sorted(routes, key=lambda entry: entry.get("metric", 0)):
        if not route.get("dev"):
            continue
        addresses = iface_addresses(route["dev"])
        preferred = route.get("prefsrc")
        if preferred in addresses:
            return preferred
        if addresses:
            return addresses[0]
    return None


def collect(interval: float, interface: str) -> None:
    previous = None
    LOGGER.info("Сбор метрик запущен, интервал %g с.", interval)
    while True:
        started_at = time.monotonic()
        current = cpu_counters(PROC)
        used, total = memory_usage(PROC)
        write_snapshot(Snapshot(
            measured_at=started_at,
            temperature_c=cpu_temperature(HWMON),
            cpu_percent=cpu_usage(previous, current),
            memory_used_kib=used,
            memory_total_kib=total,
            uptime_seconds=host_uptime(PROC),
            lan_ipv4=lan_ipv4(interface),
        ))
        previous = current
        time.sleep(max(0, interval - (time.monotonic() - started_at)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        interval = positive_env("SAMPLE_INTERVAL_SECONDS")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    collect(interval, os.environ.get("LAN_INTERFACE", "").strip())


if __name__ == "__main__":
    main()
