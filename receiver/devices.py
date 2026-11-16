#!/usr/bin/env python3
"""Persistent store for phones paired with the receiver.

Each record holds the app's client token, the friendly name it sent, when it
paired, when it was last seen and its IP. The file is mode 0600.

The store reloads when the file changes on disk, so `firetv-receiver forget`
takes effect immediately even while the service is running.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def default_store_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "firetv-receiver" / "devices.json"


def _legacy_tokens_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "firetv-receiver" / "poc-tls" / "tokens.json"


@dataclass
class Device:
    token: str
    name: str
    paired_at: float
    last_seen: float
    ip: str = ""

    @property
    def token_prefix(self) -> str:
        return self.token[:10] + "..." if len(self.token) > 10 else self.token

    def to_json(self) -> dict[str, object]:
        return asdict(self)


class DeviceStore:
    """Load/save paired devices, with backward-compatible token migration."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_store_path()
        self.devices: list[Device] = []
        self._mtime_ns: int | None = None
        self._last_save = 0.0
        self.load()

    # -- persistence -----------------------------------------------------

    def _stat_mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def load(self) -> None:
        path = self.path
        if not path.exists():
            self._migrate_legacy()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return
        devices: list[Device] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            token = str(record.get("token") or "")
            if not token:
                continue
            now = time.time()
            devices.append(
                Device(
                    token=token,
                    name=str(record.get("name") or "phone"),
                    paired_at=float(record.get("paired_at") or now),
                    last_seen=float(record.get("last_seen") or now),
                    ip=str(record.get("ip") or ""),
                )
            )
        self.devices = devices
        self._mtime_ns = self._stat_mtime()

    def reload_if_changed(self) -> None:
        mtime = self._stat_mtime()
        if mtime != self._mtime_ns:
            self.load()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"version": 1, "devices": [d.to_json() for d in self.devices]},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            tmp.chmod(0o600)
            tmp.replace(self.path)
            self._mtime_ns = self._stat_mtime()
            self._last_save = time.monotonic()
        except OSError as exc:
            print(f"[devices] could not save {self.path}: {exc}", flush=True)

    def _migrate_legacy(self) -> None:
        legacy = _legacy_tokens_path()
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict) or not tokens:
            return
        now = time.time()
        self.devices = [
            Device(token=str(token), name=str(name or "phone"), paired_at=now, last_seen=now)
            for token, name in tokens.items()
            if token
        ]
        self.save()
        try:
            legacy.rename(legacy.with_suffix(legacy.suffix + ".migrated"))
        except OSError:
            pass
        print(f"[devices] migrated {len(self.devices)} token(s) from {legacy}", flush=True)

    # -- queries ---------------------------------------------------------

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        self.reload_if_changed()
        return any(device.token == token for device in self.devices)

    def touch(self, token: str | None, ip: str = "") -> None:
        if not token:
            return
        self.reload_if_changed()
        now = time.time()
        changed = False
        for device in self.devices:
            if device.token == token:
                device.last_seen = now
                if ip:
                    device.ip = ip
                changed = True
                break
        # Do not rewrite the file on every HTTP request; once per 30 s is enough.
        if changed and time.monotonic() - self._last_save > 30:
            self.save()

    def list(self) -> list[Device]:
        self.reload_if_changed()
        return sorted(self.devices, key=lambda d: (d.name.lower(), d.paired_at))

    def add(self, name: str, ip: str = "") -> str:
        """Create a token for a phone, replacing an older token with that name."""
        self.reload_if_changed()
        token = secrets.token_urlsafe(24)
        now = time.time()
        self.devices = [d for d in self.devices if d.name != name]
        self.devices.append(
            Device(token=token, name=name or "phone", paired_at=now, last_seen=now, ip=ip)
        )
        self.save()
        return token

    def forget(self, query: str) -> int:
        """Remove by index (1-based), name, token, or unique token prefix."""
        self.reload_if_changed()
        query = query.strip()
        if not query:
            return 0
        ordered = self.list()
        removed = 0
        if query.isdigit():
            index = int(query)
            if 1 <= index <= len(ordered):
                target = ordered[index - 1]
                self.devices = [d for d in self.devices if d.token != target.token]
                removed = 1
        if not removed:
            matches = [
                d
                for d in self.devices
                if d.name == query or d.token == query or d.token.startswith(query)
            ]
            if len(matches) == 1:
                token = matches[0].token
                self.devices = [d for d in self.devices if d.token != token]
                removed = 1
            elif len(matches) > 1:
                raise ValueError("multiple devices match; use the index from 'devices'")
        if removed:
            self.save()
        return removed

    def clear(self) -> int:
        self.reload_if_changed()
        count = len(self.devices)
        self.devices.clear()
        self.save()
        return count
