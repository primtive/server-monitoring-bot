from .snapshot import Snapshot

NO_DATA = "нет данных"
GIB = 1024 ** 3


def format_ip(snapshot: Snapshot | None) -> str:
    return f"LAN IPv4: {(snapshot and snapshot.lan_ipv4) or NO_DATA}"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return NO_DATA
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [f"{value} {unit}" for value, unit in (
        (days, "д"), (hours, "ч"), (minutes, "мин"),
    ) if value]
    if not days and not hours and seconds:
        parts.append(f"{seconds} с")
    return " ".join(parts) or "0 с"


def format_capacity(used: int | None, total: int | None, divisor: int) -> str:
    if used is None or not total:
        return NO_DATA
    return f"{used / divisor:.1f} / {total / divisor:.1f} ГиБ ({used / total:.0%})"


def format_temperature(snapshot: Snapshot | None) -> str:
    value = snapshot.temperature_c if snapshot else None
    return f"Температура CPU: {NO_DATA if value is None else f'{value:g} °C'}"


def format_cpu(snapshot: Snapshot | None) -> str:
    current = snapshot.cpu_percent if snapshot else None
    average = snapshot.cpu_average_percent if snapshot else None
    average_seconds = snapshot.cpu_average_seconds if snapshot else None
    current_text = NO_DATA if current is None else f"{current:.0f}%"
    average_text = (
        f"Средняя за {format_duration(average_seconds)}: {average:.0f}%"
        if average is not None and average_seconds is not None
        else f"Средняя загрузка: {NO_DATA}"
    )
    return "\n".join((format_temperature(snapshot), f"Загрузка CPU: {current_text}", average_text))


def format_memory(snapshot: Snapshot | None) -> str:
    used = snapshot.memory_used_kib if snapshot else None
    total = snapshot.memory_total_kib if snapshot else None
    return f"RAM: {format_capacity(used, total, GIB // 1024)}"


def format_disk(snapshot: Snapshot | None) -> str:
    used = snapshot.disk_used_bytes if snapshot else None
    total = snapshot.disk_total_bytes if snapshot else None
    return f"Диск: {format_capacity(used, total, GIB)}"


def format_uptime(snapshot: Snapshot | None) -> str:
    return f"Аптайм: {format_duration(snapshot.uptime_seconds if snapshot else None)}"


def format_docker(snapshot: Snapshot | None) -> str:
    count = snapshot.docker_running if snapshot else None
    return f"Контейнеров запущено: {NO_DATA if count is None else count}"


def format_docker_ps(snapshot: Snapshot | None) -> str:
    count_text = format_docker(snapshot)
    if not snapshot or snapshot.docker_running is None:
        return count_text
    containers = snapshot.docker_containers or []
    lines = [f"{row['name'][:48]} · {row['image'][:56]} · {row['status'][:56]}" for row in containers]
    remaining = snapshot.docker_running - len(containers)
    if remaining > 0:
        lines.append(f"… и ещё {remaining}")
    return "\n".join((count_text, "", *lines)) if lines else count_text


def format_status(snapshot: Snapshot | None) -> str:
    if snapshot is None:
        return "Dabro · Состояние\n\nНет свежих данных с сервера."
    return "\n".join((
        "Dabro · Состояние", "",
        format_cpu(snapshot),
        format_memory(snapshot),
        format_disk(snapshot),
        format_uptime(snapshot),
        format_ip(snapshot),
    ))
