"""What the containers cost while ``local-kb bench`` runs: memory, CPU, disk, images.

A background thread samples ``podman stats`` once a second, tagging each sample with the phase
the bench is in (``rds:load``, ``opensearch:latency``, …). Phase boundaries also read each
container's cgroup ``cpu.stat``, so CPU-seconds per phase are exact rather than integrated from
percentages.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import defaultdict
from typing import Any

CONTAINERS = {
    "opensearch": "local-kb_opensearch_1",
    "dashboards": "local-kb_dashboards_1",
    "postgres": "local-kb_postgres_1",
}
VOLUMES = {"opensearch": "local-kb_opensearch-data", "postgres": "local-kb_postgres-data"}
_UNITS = {
    "b": 1,
    "kb": 1e3,
    "mb": 1e6,
    "gb": 1e9,
    "tb": 1e12,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
}


def _run(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout


def _bytes(text: str) -> float:
    match = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]*)\s*", text)
    if not match:
        return float("nan")
    return float(match.group(1)) * _UNITS.get(match.group(2).lower() or "b", 1)


def running() -> dict[str, str]:
    """Which of the stack's containers are up: role -> container name."""
    names = set(_run("podman", "ps", "--format", "{{.Names}}").split())
    return {role: name for role, name in CONTAINERS.items() if name in names}


def stats(names: list[str]) -> dict[str, dict[str, float]]:
    """One ``podman stats`` reading: RSS in bytes and CPU percent per container."""
    if not names:
        return {}
    rows = json.loads(_run("podman", "stats", "--no-stream", "--format", "json", *names))
    out = {}
    for row in rows:
        used = str(row.get("mem_usage", "")).split("/")[0]
        out[str(row["name"])] = {
            "rss": _bytes(used),
            "cpu_percent": float(str(row.get("cpu_percent", "0")).rstrip("%") or 0),
        }
    return out


def stop(names: list[str]) -> None:
    """Stop those of ``names`` that are running; OpenSearch gets a minute to shut down."""
    up = [n for n in names if n in set(running().values())]
    if up:
        _run("podman", "stop", "-t", "60", *up)


def start(names: list[str]) -> None:
    if names:
        _run("podman", "start", *names)


def cpu_seconds(name: str) -> float | None:
    """The container's cumulative CPU time, from cgroup v2."""
    try:
        text = _run("podman", "exec", name, "cat", "/sys/fs/cgroup/cpu.stat")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"^usage_usec (\d+)$", text, re.M)
    return int(match.group(1)) / 1e6 if match else None


def image_sizes() -> dict[str, Any]:
    out = {}
    for role, name in running().items():
        image = _run("podman", "inspect", name, "--format", "{{.ImageName}}").strip()
        size = _run("podman", "image", "inspect", image, "--format", "{{.Size}}").strip()
        out[role] = {"image": image, "bytes": int(size)}
    return out


def volume_bytes() -> dict[str, int | None]:
    """Each store's data volume on disk. ``podman unshare`` reads it as the container's user,
    since rootless volume files belong to subordinate uids."""
    out: dict[str, int | None] = {}
    for role, volume in VOLUMES.items():
        try:
            path = _run(
                "podman", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"
            ).strip()
            out[role] = int(_run("podman", "unshare", "du", "-sb", path).split()[0])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            out[role] = None
    return out


def _delta(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else end - start


class Sampler:
    """Samples every ``interval`` seconds until ``stop()``; ``phase(label)`` starts a phase."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        # Every container the stack can run; ones stopped (by --isolate) simply have no samples.
        self.names = list(CONTAINERS.values())
        self.label = "idle"
        self.samples: list[dict[str, Any]] = []
        self.cpu: dict[str, dict[str, float | None]] = {}
        self._cpu_start: dict[str, float | None] = {}
        self._started = time.monotonic()
        self._phase_started = self._started
        self._durations: dict[str, float] = defaultdict(float)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="bench-sampler", daemon=True)

    def __enter__(self) -> Sampler:
        self._cpu_start = {n: cpu_seconds(n) for n in self.names}
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.phase("done")
        self._stop.set()
        self._thread.join()

    def phase(self, label: str) -> None:
        now = time.monotonic()
        ended = {n: cpu_seconds(n) for n in self.names}
        self.cpu[self.label] = {n: _delta(self._cpu_start[n], ended[n]) for n in self.names}
        self._durations[self.label] += now - self._phase_started
        self._cpu_start, self._phase_started, self.label = ended, now, label

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                reading = stats(list(running().values()))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                reading = {}
            t = round(time.monotonic() - self._started, 1)
            for name, values in reading.items():
                self.samples.append({"t": t, "phase": self.label, "container": name, **values})
            self._stop.wait(self.interval)

    def summary(self) -> dict[str, Any]:
        """Per phase and container: seconds, peak and mean RSS, mean CPU %, CPU-seconds."""
        out: dict[str, Any] = {}
        for label, seconds in self._durations.items():
            phase: dict[str, Any] = {"seconds": round(seconds, 2), "containers": {}}
            for name in self.names:
                rows = [s for s in self.samples if s["phase"] == label and s["container"] == name]
                if not rows and self.cpu.get(label, {}).get(name) is None:
                    continue  # stopped throughout this phase
                rss = [s["rss"] for s in rows]
                cpu = [s["cpu_percent"] for s in rows]
                phase["containers"][name] = {
                    "samples": len(rows),
                    "rss_peak": max(rss) if rss else None,
                    "rss_mean": sum(rss) / len(rss) if rss else None,
                    "cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
                    "cpu_seconds": self.cpu.get(label, {}).get(name),
                }
            out[label] = phase
        return out
