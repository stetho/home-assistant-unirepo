#!/usr/bin/env python3
"""
Home Assistant rebuild audit tool - v0.4

Creates:
    audit.json       Structured inventory, redacted by default
    rebuild.md       Cleanup/rebuild-oriented checklist

Environment:
    HA_URL            e.g. http://homeassistant-old.local:8123
    HA_TOKEN          Home Assistant long-lived access token
    HA_AUDIT_REDACT   true/false (default: true)

Usage:
    HA_URL=http://homeassistant-old.local:8123 \
    HA_TOKEN='...' \
    python ha_audit.py
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets


HELPER_DOMAINS = {
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "counter",
    "timer",
    "schedule",
}

SENSITIVE_CONFIG_KEYS = {
    "latitude",
    "longitude",
    "external_url",
    "internal_url",
}

SYSTEM_PLATFORMS = {
    "hassio",
    "homeassistant",
    "backup",
}


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant returns an error."""


class HomeAssistantClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = self._websocket_url(url)
        self.token = token
        self._next_id = 1
        self.ws: Any = None

    @staticmethod
    def _websocket_url(url: str) -> str:
        parsed = urlparse(url)

        if parsed.scheme == "http":
            scheme = "ws"
        elif parsed.scheme == "https":
            scheme = "wss"
        elif parsed.scheme in {"ws", "wss"}:
            scheme = parsed.scheme
        else:
            raise ValueError(
                "HA_URL must start with http://, https://, ws:// or wss://"
            )

        path = parsed.path.rstrip("/") + "/api/websocket"
        return urlunparse((scheme, parsed.netloc, path, "", "", ""))

    async def __aenter__(self) -> "HomeAssistantClient":
        self.ws = await websockets.connect(
            self.url,
            max_size=None,
            open_timeout=15,
        )

        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise HomeAssistantError(f"Expected auth_required, got: {hello!r}")

        await self.ws.send(
            json.dumps({"type": "auth", "access_token": self.token})
        )

        auth_result = json.loads(await self.ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise HomeAssistantError(f"Authentication failed: {auth_result!r}")

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def command(self, command_type: str, **kwargs: Any) -> Any:
        message_id = self._next_id
        self._next_id += 1

        await self.ws.send(
            json.dumps({"id": message_id, "type": command_type, **kwargs})
        )

        while True:
            response = json.loads(await self.ws.recv())

            if response.get("id") != message_id:
                continue
            if response.get("type") != "result":
                continue

            if not response.get("success"):
                error = response.get("error", {})
                raise HomeAssistantError(
                    f"{command_type}: "
                    f"{error.get('code', 'unknown')}: "
                    f"{error.get('message', 'unknown error')}"
                )

            return response.get("result")

    async def optional_command(
        self, command_type: str, **kwargs: Any
    ) -> tuple[Any, str | None]:
        """Run a useful-but-not-guaranteed WebSocket command."""
        try:
            return await self.command(command_type, **kwargs), None
        except HomeAssistantError as exc:
            return None, str(exc)


def entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def state_map(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        state["entity_id"]: state
        for state in states
        if "entity_id" in state
    }


def friendly_name(
    entity: dict[str, Any],
    states_by_id: dict[str, dict[str, Any]],
) -> str:
    entity_id = entity["entity_id"]
    state = states_by_id.get(entity_id, {})
    attributes = state.get("attributes", {})

    return (
        entity.get("name")
        or attributes.get("friendly_name")
        or entity.get("original_name")
        or entity_id
    )


def redact_config(config: dict[str, Any], enabled: bool) -> dict[str, Any]:
    if not enabled:
        return dict(config)

    result = dict(config)
    for key in SENSITIVE_CONFIG_KEYS:
        if key in result and result[key] is not None:
            result[key] = "<redacted>"
    return result


def normalise_name(name: str) -> str:
    """Normalise display names for conservative duplicate hints."""
    value = name.casefold()
    value = value.replace("’", "'").replace("‘", "'")
    value = re.sub(r"'s\b", "", value)
    value = re.sub(r"\broom\b", "", value)
    value = re.sub(r"\bbedroom\b", "", value)
    value = re.sub(r"\b\d+\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def possible_duplicate_groups(
    items: list[dict[str, Any]],
    name_key: str,
    id_key: str,
    threshold: float = 0.88,
) -> list[list[dict[str, str]]]:
    """
    Produce conservative duplicate hints.

    This deliberately errs towards missing a duplicate rather than claiming
    unrelated items are duplicates.
    """
    candidates: list[tuple[int, int]] = []

    for i, left in enumerate(items):
        left_name = str(left.get(name_key) or "")
        left_norm = normalise_name(left_name)
        if not left_norm:
            continue

        for j in range(i + 1, len(items)):
            right = items[j]
            right_name = str(right.get(name_key) or "")
            right_norm = normalise_name(right_name)
            if not right_norm:
                continue

            exact_norm = left_norm == right_norm
            similarity = difflib.SequenceMatcher(
                None, left_norm, right_norm
            ).ratio()

            if exact_norm or similarity >= threshold:
                candidates.append((i, j))

    # Union-find to combine overlapping pairs.
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in candidates:
        union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    involved = {index for pair in candidates for index in pair}
    for index in involved:
        groups[find(index)].append(index)

    result = []
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        result.append(
            [
                {
                    "id": str(items[index].get(id_key) or ""),
                    "name": str(items[index].get(name_key) or ""),
                }
                for index in sorted(indexes)
            ]
        )

    return sorted(
        result,
        key=lambda group: group[0]["name"].casefold(),
    )


def config_entry_name(entry: dict[str, Any]) -> str:
    return (
        entry.get("title")
        or entry.get("domain")
        or entry.get("entry_id")
        or "Unnamed config entry"
    )


def cleanup_assessment(
    total: int,
    user_disabled: int,
    unavailable: int,
    entry_state: str | None = None,
    entry_source: str | None = None,
    entry_disabled_by: str | None = None,
) -> dict[str, Any]:
    """
    Heuristic only: prioritises what deserves human review.

    Important: entities disabled_by='integration' are usually optional or
    diagnostic entities disabled by default. They are deliberately NOT treated
    as cruft here.
    """
    score = 0
    reasons: list[str] = []

    if entry_disabled_by == "user":
        score += 5
        reasons.append("config entry explicitly disabled by user")

    if entry_state in {"setup_error", "setup_retry", "migration_error"}:
        score += 5
        reasons.append(f"config entry state is {entry_state}")
    elif entry_state == "not_loaded":
        if entry_source == "ignore":
            score += 3
            reasons.append("ignored/not loaded")
        else:
            score += 2
            reasons.append("not loaded")

    if total > 0:
        unavailable_ratio = unavailable / total

        if unavailable_ratio == 1:
            score += 5
            reasons.append("all entities unavailable")
        elif unavailable_ratio >= 0.5:
            score += 4
            reasons.append("at least half unavailable")
        elif unavailable_ratio >= 0.2:
            score += 2
            reasons.append("many unavailable")
        elif unavailable > 0:
            score += 1
            reasons.append("some unavailable")

        if user_disabled:
            if user_disabled == total:
                score += 4
                reasons.append("all entities disabled by user")
            else:
                score += 2
                reasons.append(f"{user_disabled} entities disabled by user")
    else:
        # Zero entities is common for service/configuration integrations,
        # Bluetooth adapters, discovery entries, and providers. Do not flag it.
        reasons.append("no registry entities")

    if score >= 5:
        level = "high"
    elif score >= 2:
        level = "review"
    else:
        level = "normal"

    return {
        "level": level,
        "score": score,
        "reason": ", ".join(reasons) if reasons else "no obvious warning signs",
    }

def identify_addons(
    devices: list[dict[str, Any]],
    entities_by_device: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    Detect add-ons from Supervisor-created service devices.

    This is intentionally based on device metadata rather than pretending
    every hassio entity is an add-on.
    """
    addons = []

    for device in devices:
        model = str(device.get("model") or "")
        manufacturer = str(device.get("manufacturer") or "")
        identifiers = device.get("identifiers") or []

        looks_like_addon = (
            model == "Home Assistant Add-on"
            or any(
                isinstance(identifier, (list, tuple))
                and len(identifier) >= 2
                and identifier[0] == "hassio"
                and str(identifier[1]).startswith("addon_")
                for identifier in identifiers
            )
        )

        if not looks_like_addon:
            continue

        device_entities = entities_by_device.get(device["id"], [])
        running_entities = [
            e for e in device_entities
            if e["entity_id"].startswith("binary_sensor.")
            and e["entity_id"].endswith("_running")
        ]

        running_state = (
            running_entities[0]["state"] if running_entities else None
        )

        addons.append(
            {
                "device_id": device["id"],
                "name": (
                    device.get("name_by_user")
                    or device.get("name")
                    or "Unnamed add-on"
                ),
                "manufacturer": manufacturer or None,
                "entity_count": len(device_entities),
                "running_state": running_state,
            }
        )

    return sorted(addons, key=lambda x: x["name"].casefold())


def build_inventory(
    config: dict[str, Any],
    config_entries: list[dict[str, Any]],
    areas: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    states: list[dict[str, Any]],
    redact: bool,
    warnings: list[str],
) -> dict[str, Any]:

    states_by_id = state_map(states)
    areas_by_id = {area["area_id"]: area for area in areas}
    devices_by_id = {device["id"]: device for device in devices}

    entries_by_id = {
        entry["entry_id"]: entry
        for entry in config_entries
        if entry.get("entry_id")
    }

    processed_entities = []
    entities_by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entities_by_config_entry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entities_by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for entity in entities:
        entity_id = entity["entity_id"]
        domain = entity_domain(entity_id)
        platform = entity.get("platform", "unknown")
        device = devices_by_id.get(entity.get("device_id"))
        area_id = entity.get("area_id") or (device or {}).get("area_id")
        area = areas_by_id.get(area_id)
        current_state = states_by_id.get(entity_id)
        config_entry_id = entity.get("config_entry_id")

        processed = {
            "entity_id": entity_id,
            "name": friendly_name(entity, states_by_id),
            "domain": domain,
            "platform": platform,
            "config_entry_id": config_entry_id,
            "device_id": entity.get("device_id"),
            "device_name": (
                (device or {}).get("name_by_user")
                or (device or {}).get("name")
            ),
            "area_id": area_id,
            "area_name": (area or {}).get("name"),
            "disabled_by": entity.get("disabled_by"),
            "hidden_by": entity.get("hidden_by"),
            "entity_category": entity.get("entity_category"),
            "state": current_state.get("state") if current_state else None,
            "has_state": current_state is not None,
        }

        processed_entities.append(processed)
        entities_by_platform[platform].append(processed)

        if config_entry_id:
            entities_by_config_entry[config_entry_id].append(processed)

        if processed["device_id"]:
            entities_by_device[processed["device_id"]].append(processed)

    registry_ids = {entity["entity_id"] for entity in entities}
    state_only_entities = [
        {
            "entity_id": state["entity_id"],
            "domain": entity_domain(state["entity_id"]),
            "state": state.get("state"),
            "name": (
                state.get("attributes", {}).get("friendly_name")
                or state["entity_id"]
            ),
        }
        for state in states
        if state["entity_id"] not in registry_ids
    ]

    processed_devices = []
    devices_by_config_entry: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for device in devices:
        area = areas_by_id.get(device.get("area_id"))
        device_entity_ids = sorted(
            e["entity_id"] for e in entities_by_device.get(device["id"], [])
        )

        processed = {
            "id": device["id"],
            "name": (
                device.get("name_by_user")
                or device.get("name")
                or "Unnamed device"
            ),
            "manufacturer": device.get("manufacturer"),
            "model": device.get("model"),
            "area_id": device.get("area_id"),
            "area_name": (area or {}).get("name"),
            "disabled_by": device.get("disabled_by"),
            "config_entries": sorted(device.get("config_entries") or []),
            "entities": device_entity_ids,
        }
        processed_devices.append(processed)

        for entry_id in processed["config_entries"]:
            devices_by_config_entry[entry_id].append(processed)

    # Config entries: the proper "Settings > Devices & services" layer.
    processed_config_entries = []

    for entry in config_entries:
        entry_id = entry.get("entry_id")
        entry_entities = entities_by_config_entry.get(entry_id, [])
        entry_devices = devices_by_config_entry.get(entry_id, [])

        total = len(entry_entities)
        integration_disabled = sum(
            e["disabled_by"] == "integration" for e in entry_entities
        )
        user_disabled = sum(
            e["disabled_by"] == "user" for e in entry_entities
        )
        unavailable = sum(e["state"] == "unavailable" for e in entry_entities)
        assessment = cleanup_assessment(
            total=total,
            user_disabled=user_disabled,
            unavailable=unavailable,
            entry_state=entry.get("state"),
            entry_source=entry.get("source"),
            entry_disabled_by=entry.get("disabled_by"),
        )

        processed_config_entries.append(
            {
                "entry_id": entry_id,
                "domain": entry.get("domain"),
                "title": entry.get("title"),
                "name": config_entry_name(entry),
                "state": entry.get("state"),
                "disabled_by": entry.get("disabled_by"),
                "source": entry.get("source"),
                "supports_options": entry.get("supports_options"),
                "supports_reconfigure": entry.get("supports_reconfigure"),
                "device_count": len(entry_devices),
                "entity_count": total,
                "integration_disabled_entities": integration_disabled,
                "user_disabled_entities": user_disabled,
                "unavailable_entities": unavailable,
                "cleanup": assessment,
            }
        )

    # Platform summaries remain useful for YAML/platform-only entities and
    # integrations where config entry information isn't available.
    platform_summaries = []
    for platform, platform_entities in entities_by_platform.items():
        total = len(platform_entities)
        integration_disabled = sum(
            e["disabled_by"] == "integration" for e in platform_entities
        )
        user_disabled = sum(
            e["disabled_by"] == "user" for e in platform_entities
        )
        unavailable = sum(e["state"] == "unavailable" for e in platform_entities)

        platform_summaries.append(
            {
                "platform": platform,
                "entity_count": total,
                "integration_disabled_entities": integration_disabled,
                "user_disabled_entities": user_disabled,
                "unavailable_entities": unavailable,
                "cleanup": cleanup_assessment(
                    total=total,
                    user_disabled=user_disabled,
                    unavailable=unavailable,
                ),
            }
        )

    platform_summaries.sort(key=lambda x: (-x["entity_count"], x["platform"]))

    addons = identify_addons(devices, entities_by_device)

    duplicate_areas = possible_duplicate_groups(
        areas, name_key="name", id_key="area_id", threshold=0.88
    )

    # Device duplicate hints are stricter because legitimately repeated names
    # ("Back Garden", "Living Room") are common in HA.
    duplicate_devices = possible_duplicate_groups(
        processed_devices, name_key="name", id_key="id", threshold=0.97
    )

    unavailable = [
        e for e in processed_entities if e["state"] == "unavailable"
    ]
    disabled = [
        e for e in processed_entities if e["disabled_by"] is not None
    ]

    automations = [e for e in processed_entities if e["domain"] == "automation"]
    scripts = [e for e in processed_entities if e["domain"] == "script"]
    scenes = [e for e in processed_entities if e["domain"] == "scene"]
    helpers = [e for e in processed_entities if e["domain"] in HELPER_DOMAINS]

    return {
        "schema_version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "redacted": redact,
        "warnings": warnings,
        "home_assistant": redact_config(config, redact),
        "summary": {
            "areas": len(areas),
            "devices": len(devices),
            "devices_without_area": sum(
                1 for device in processed_devices if not device["area_id"]
            ),
            "config_entries": len(processed_config_entries),
            "registry_entities": len(entities),
            "state_entities": len(states),
            "state_only_entities": len(state_only_entities),
            "automations": len(automations),
            "scripts": len(scripts),
            "scenes": len(scenes),
            "helpers": len(helpers),
            "unavailable_entities": len(unavailable),
            "disabled_entities": len(disabled),
            "integration_disabled_entities": sum(
                e["disabled_by"] == "integration" for e in processed_entities
            ),
            "user_disabled_entities": sum(
                e["disabled_by"] == "user" for e in processed_entities
            ),
            "addons_detected": len(addons),
            "possible_duplicate_area_groups": len(duplicate_areas),
            "same_or_similar_name_device_groups": len(duplicate_devices),
        },
        "config_entries": sorted(
            processed_config_entries,
            key=lambda x: (
                {"high": 0, "review": 1, "normal": 2}.get(
                    x["cleanup"]["level"], 3
                ),
                x["name"].casefold(),
            ),
        ),
        "platforms": platform_summaries,
        "addons": addons,
        "areas": sorted(areas, key=lambda x: x.get("name", "").casefold()),
        "possible_duplicate_areas": duplicate_areas,
        "same_or_similar_name_devices": duplicate_devices,
        "devices": sorted(
            processed_devices, key=lambda x: x["name"].casefold()
        ),
        "entities": sorted(
            processed_entities, key=lambda x: x["entity_id"]
        ),
        "state_only_entities": sorted(
            state_only_entities, key=lambda x: x["entity_id"]
        ),
        "automations": automations,
        "scripts": scripts,
        "scenes": scenes,
        "helpers": helpers,
        "unavailable_entities": unavailable,
        "disabled_entities": disabled,
    }


def md_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def md_row(*values: Any) -> str:
    return "| " + " | ".join(md_cell(v) for v in values) + " |"


def render_markdown(audit: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = audit["summary"]

    lines += [
        "# Home Assistant Rebuild Audit",
        "",
        f"Generated: `{audit['generated_at']}`",
        "",
        "> This is a rebuild checklist, not a deletion recommendation. "
        "HIGH/REVIEW flags are heuristics telling you where to look first.",
        "",
        "## Summary",
        "",
        md_row("Item", "Count"),
        md_row("---", "---:"),
    ]

    for key, value in summary.items():
        lines.append(md_row(key.replace("_", " ").title(), value))

    if audit["warnings"]:
        lines += ["", "## Audit Warnings", ""]
        for warning in audit["warnings"]:
            lines.append(f"- {warning}")

    lines += [
        "",
        "## Config Entries / Integrations",
        "",
        md_row(
            "Decision",
            "Priority",
            "Integration",
            "Domain",
            "Devices",
            "Entities",
            "Default-disabled",
            "User-disabled",
            "Unavailable",
            "Why flagged",
        ),
        md_row(
            "---", "---", "---", "---", "---:", "---:", "---:", "---:", "---:", "---"
        ),
    ]

    for entry in audit["config_entries"]:
        cleanup = entry["cleanup"]
        priority = cleanup["level"].upper()
        lines.append(
            md_row(
                "[ ]",
                priority,
                entry["name"],
                entry["domain"],
                entry["device_count"],
                entry["entity_count"],
                entry["integration_disabled_entities"],
                entry["user_disabled_entities"],
                entry["unavailable_entities"],
                cleanup["reason"],
            )
        )

    lines += [
        "",
        "### Platform Summary",
        "",
        "> Useful as a cross-check. Platforms are not always one-to-one with "
        "config entries.",
        "",
        md_row(
            "Priority",
            "Platform",
            "Entities",
            "Default-disabled",
            "User-disabled",
            "Unavailable",
            "Why flagged",
        ),
        md_row("---", "---", "---:", "---:", "---:", "---:", "---"),
    ]

    for platform in audit["platforms"]:
        cleanup = platform["cleanup"]
        lines.append(
            md_row(
                cleanup["level"].upper(),
                platform["platform"],
                platform["entity_count"],
                platform["integration_disabled_entities"],
                platform["user_disabled_entities"],
                platform["unavailable_entities"],
                cleanup["reason"],
            )
        )

    lines += ["", "## Add-ons", ""]

    if not audit["addons"]:
        lines.append("_No Home Assistant add-on devices detected._")
    else:
        lines += [
            md_row("Decision", "Add-on", "Running entity", "Entities"),
            md_row("---", "---", "---", "---:"),
        ]
        for addon in audit["addons"]:
            lines.append(
                md_row(
                    "[ ]",
                    addon["name"],
                    addon["running_state"],
                    addon["entity_count"],
                )
            )

    lines += ["", "## Areas", ""]
    for area in audit["areas"]:
        lines.append(f"- [ ] {area.get('name', 'Unnamed area')}")

    lines += ["", "### Possible Duplicate Areas", ""]
    if not audit["possible_duplicate_areas"]:
        lines.append("_No same/similar-name groups found._")
    else:
        for group in audit["possible_duplicate_areas"]:
            lines.append(
                "- ⚠ " + " / ".join(item["name"] for item in group)
            )

    lines += [
        "",
        "## Same/Similar Device Names",
        "",
        "> These are NOT assumed to be duplicates. Home Assistant commonly has "
        "several legitimate devices with the same room/name. This is just a "
        "naming review list.",
        "",
    ]
    if not audit["same_or_similar_name_devices"]:
        lines.append("_No conservative duplicate hints found._")
    else:
        for group in audit["same_or_similar_name_devices"]:
            lines.append(
                "- ⚠ " + " / ".join(item["name"] for item in group)
            )

    lines += [
        "",
        "## Devices Without an Area",
        "",
        md_row("Decision", "Device", "Manufacturer", "Model", "Entities"),
        md_row("---", "---", "---", "---", "---:"),
    ]

    for device in audit["devices"]:
        if device["area_id"]:
            continue
        lines.append(
            md_row(
                "[ ]",
                device["name"],
                device["manufacturer"],
                device["model"],
                len(device["entities"]),
            )
        )

    def entity_section(
        title: str,
        entities: list[dict[str, Any]],
    ) -> None:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                md_row("Entity", "Name", "Platform", "Area", "State"),
                md_row("---", "---", "---", "---", "---"),
            ]
        )
        for entity in sorted(entities, key=lambda x: x["entity_id"]):
            lines.append(
                md_row(
                    f"`{entity['entity_id']}`",
                    entity["name"],
                    entity["platform"],
                    entity["area_name"],
                    entity["state"],
                )
            )

    entity_section("Automations", audit["automations"])
    entity_section("Scripts", audit["scripts"])
    entity_section("Scenes", audit["scenes"])
    entity_section("Helpers", audit["helpers"])
    entity_section("Unavailable Entities", audit["unavailable_entities"])

    lines += [
        "",
        "## Disabled Entities",
        "",
        md_row("Entity", "Name", "Platform", "Disabled By"),
        md_row("---", "---", "---", "---"),
    ]
    for entity in sorted(
        audit["disabled_entities"], key=lambda x: x["entity_id"]
    ):
        lines.append(
            md_row(
                f"`{entity['entity_id']}`",
                entity["name"],
                entity["platform"],
                entity["disabled_by"],
            )
        )

    lines += [
        "",
        "## State-only Entities",
        "",
        "> Present in Home Assistant's state machine but absent from the "
        "entity registry.",
        "",
    ]
    for entity in audit["state_only_entities"]:
        lines.append(
            f"- `{entity['entity_id']}` — {entity['name']} ({entity['state']})"
        )

    lines.append("")
    return "\n".join(lines)


def identity_key(item: dict[str, Any], kind: str) -> str:
    """Stable-ish human comparison key across independent HA installations."""
    if kind == "config_entry":
        return f"{item.get('domain', '')}::{normalise_name(str(item.get('name') or item.get('title') or ''))}"
    if kind == "addon":
        return normalise_name(str(item.get("name") or ""))
    if kind == "area":
        return normalise_name(str(item.get("name") or ""))
    raise ValueError(kind)


def compare_collection(
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    kind: str,
) -> dict[str, Any]:
    old_map = {identity_key(x, kind): x for x in old_items if identity_key(x, kind)}
    new_map = {identity_key(x, kind): x for x in new_items if identity_key(x, kind)}

    old_keys = set(old_map)
    new_keys = set(new_map)

    return {
        "old_only": [old_map[k] for k in sorted(old_keys - new_keys)],
        "both": [
            {"old": old_map[k], "new": new_map[k]}
            for k in sorted(old_keys & new_keys)
        ],
        "new_only": [new_map[k] for k in sorted(new_keys - old_keys)],
    }


def compare_audits(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    integrations = compare_collection(
        old.get("config_entries", []),
        new.get("config_entries", []),
        "config_entry",
    )
    addons = compare_collection(
        old.get("addons", []), new.get("addons", []), "addon"
    )
    areas = compare_collection(
        old.get("areas", []), new.get("areas", []), "area"
    )

    old_integration_count = len(old.get("config_entries", []))
    integration_both = len(integrations["both"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "old_generated_at": old.get("generated_at"),
        "new_generated_at": new.get("generated_at"),
        "progress": {
            "old_config_entries": old_integration_count,
            "matched_config_entries": integration_both,
            "new_config_entries": len(new.get("config_entries", [])),
            "matched_percent": (
                round(integration_both / old_integration_count * 100, 1)
                if old_integration_count else 0
            ),
            "old_addons": len(old.get("addons", [])),
            "matched_addons": len(addons["both"]),
            "new_addons": len(new.get("addons", [])),
            "old_areas": len(old.get("areas", [])),
            "matched_areas": len(areas["both"]),
            "new_areas": len(new.get("areas", [])),
            "old_devices": old.get("summary", {}).get("devices", 0),
            "new_devices": new.get("summary", {}).get("devices", 0),
            "old_entities": old.get("summary", {}).get("registry_entities", 0),
            "new_entities": new.get("summary", {}).get("registry_entities", 0),
        },
        "integrations": integrations,
        "addons": addons,
        "areas": areas,
    }


def render_comparison_md(comparison: dict[str, Any]) -> str:
    p = comparison["progress"]
    lines = [
        "# Home Assistant Rebuild Progress",
        "",
        f"Generated: `{comparison['generated_at']}`",
        "",
        "> Matching is deliberately based on integration domain + normalised "
        "display name, not Home Assistant internal IDs, because IDs differ "
        "between independent installations.",
        "",
        "## Progress",
        "",
        md_row("Item", "Old", "Matched", "New"),
        md_row("---", "---:", "---:", "---:"),
        md_row(
            "Config entries",
            p["old_config_entries"],
            p["matched_config_entries"],
            p["new_config_entries"],
        ),
        md_row(
            "Add-ons",
            p["old_addons"],
            p["matched_addons"],
            p["new_addons"],
        ),
        md_row(
            "Areas",
            p["old_areas"],
            p["matched_areas"],
            p["new_areas"],
        ),
        md_row("Devices", p["old_devices"], "—", p["new_devices"]),
        md_row("Registry entities", p["old_entities"], "—", p["new_entities"]),
        "",
        f"Matched old config entries: **{p['matched_percent']}%**",
        "",
        "## Integrations: Old Only",
        "",
        "> These are decisions still outstanding, not necessarily things that "
        "should be migrated.",
        "",
        md_row("Decision", "Integration", "Domain", "Old state", "Entities", "Unavailable"),
        md_row("---", "---", "---", "---", "---:", "---:"),
    ]

    for item in comparison["integrations"]["old_only"]:
        lines.append(md_row(
            "[ ]",
            item.get("name"),
            item.get("domain"),
            item.get("state"),
            item.get("entity_count"),
            item.get("unavailable_entities"),
        ))

    lines += [
        "",
        "## Integrations: Present on Both",
        "",
        md_row("Integration", "Domain", "Old entities", "New entities", "New state"),
        md_row("---", "---", "---:", "---:", "---"),
    ]
    for pair in comparison["integrations"]["both"]:
        old, new = pair["old"], pair["new"]
        lines.append(md_row(
            new.get("name"),
            new.get("domain"),
            old.get("entity_count"),
            new.get("entity_count"),
            new.get("state"),
        ))

    lines += [
        "",
        "## Integrations: New Only",
        "",
    ]
    for item in comparison["integrations"]["new_only"]:
        lines.append(f"- {item.get('name')} (`{item.get('domain')}`)")

    lines += ["", "## Add-ons: Old Only", ""]
    for item in comparison["addons"]["old_only"]:
        lines.append(f"- [ ] {item.get('name')}")

    lines += ["", "## Add-ons: Present on Both", ""]
    for pair in comparison["addons"]["both"]:
        lines.append(f"- [x] {pair['new'].get('name')}")

    lines += ["", "## Add-ons: New Only", ""]
    for item in comparison["addons"]["new_only"]:
        lines.append(f"- {item.get('name')}")

    lines += ["", "## Areas: Old Only", ""]
    for item in comparison["areas"]["old_only"]:
        lines.append(f"- [ ] {item.get('name')}")

    lines += ["", "## Areas: Present on Both", ""]
    for pair in comparison["areas"]["both"]:
        lines.append(f"- [x] {pair['new'].get('name')}")

    lines += ["", "## Areas: New Only", ""]
    for item in comparison["areas"]["new_only"]:
        lines.append(f"- {item.get('name')}")

    lines.append("")
    return "\n".join(lines)


def load_audit(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a Home Assistant instance or compare two audits."
    )
    sub = parser.add_subparsers(dest="mode")

    sub.add_parser("audit", help="Audit HA_URL and create audit.json/rebuild.md")

    compare = sub.add_parser(
        "compare", help="Compare an old audit.json with a new audit.json"
    )
    compare.add_argument("old", help="Old instance audit.json")
    compare.add_argument("new", help="New instance audit.json")
    compare.add_argument(
        "--output",
        default="progress.md",
        help="Markdown report filename (default: progress.md)",
    )
    compare.add_argument(
        "--json-output",
        default="comparison.json",
        help="Structured comparison filename (default: comparison.json)",
    )

    args = parser.parse_args()
    if args.mode is None:
        args.mode = "audit"
    return args


async def audit_main() -> None:
    ha_url = os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN")
    redact = os.environ.get("HA_AUDIT_REDACT", "true").casefold() not in {
        "0", "false", "no", "off"
    }

    if not ha_url:
        sys.exit("HA_URL is not set")
    if not ha_token:
        sys.exit("HA_TOKEN is not set")

    warnings: list[str] = []

    print(f"Connecting to {ha_url}...")

    try:
        async with HomeAssistantClient(ha_url, ha_token) as ha:
            print("Authenticated.")

            print("Reading Home Assistant configuration...")
            config = await ha.command("get_config")

            print("Reading config entries...")
            config_entries, config_entries_error = await ha.optional_command(
                "config_entries/get"
            )
            if config_entries is None:
                config_entries = []
                warnings.append(
                    "Could not retrieve config entries; the server rejected "
                    f"`config_entries/get`: {config_entries_error}"
                )
                print("  Warning: config_entries/get unavailable.")

            print("Reading areas...")
            areas = await ha.command("config/area_registry/list")

            print("Reading devices...")
            devices = await ha.command("config/device_registry/list")

            print("Reading entity registry...")
            entities = await ha.command("config/entity_registry/list")

            print("Reading current states...")
            states = await ha.command("get_states")

    except Exception as exc:
        sys.exit(f"Audit failed: {exc}")

    # Some HA versions return a wrapper rather than the bare list.
    if isinstance(config_entries, dict):
        config_entries = (
            config_entries.get("entries")
            or config_entries.get("config_entries")
            or []
        )

    audit = build_inventory(
        config=config,
        config_entries=config_entries,
        areas=areas,
        devices=devices,
        entities=entities,
        states=states,
        redact=redact,
        warnings=warnings,
    )

    Path("audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    Path("rebuild.md").write_text(
        render_markdown(audit),
        encoding="utf-8",
    )

    s = audit["summary"]

    print()
    print("Audit complete")
    print("--------------")
    print(f"Config entries:         {s['config_entries']}")
    print(f"Areas:                  {s['areas']}")
    print(f"Devices:                {s['devices']}")
    print(f"Devices without area:   {s['devices_without_area']}")
    print(f"Registry entities:      {s['registry_entities']}")
    print(f"Runtime states:         {s['state_entities']}")
    print(f"Automations:            {s['automations']}")
    print(f"Scripts:                {s['scripts']}")
    print(f"Scenes:                 {s['scenes']}")
    print(f"Helpers:                {s['helpers']}")
    print(f"Unavailable entities:   {s['unavailable_entities']}")
    print(f"Disabled entities:      {s['disabled_entities']}")
    print(f"  by integration:       {s['integration_disabled_entities']}")
    print(f"  by user:              {s['user_disabled_entities']}")
    print(f"Add-ons detected:       {s['addons_detected']}")
    print(
        "Duplicate area groups: "
        f"{s['possible_duplicate_area_groups']}"
    )
    print(
        "Same-name device groups: "
        f"{s['same_or_similar_name_device_groups']}"
    )
    print(f"Redaction enabled:      {audit['redacted']}")
    print()
    print("Created:")
    print("  audit.json")
    print("  rebuild.md")

    if warnings:
        print()
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")


def main() -> None:
    args = parse_args()

    if args.mode == "compare":
        old = load_audit(args.old)
        new = load_audit(args.new)
        comparison = compare_audits(old, new)

        Path(args.json_output).write_text(
            json.dumps(comparison, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        Path(args.output).write_text(
            render_comparison_md(comparison),
            encoding="utf-8",
        )

        p = comparison["progress"]
        print("Comparison complete")
        print("-------------------")
        print(
            f"Config entries: {p['matched_config_entries']} matched "
            f"of {p['old_config_entries']} old entries "
            f"({p['matched_percent']}%)"
        )
        print(f"Add-ons:        {p['matched_addons']} matched of {p['old_addons']}")
        print(f"Areas:          {p['matched_areas']} matched of {p['old_areas']}")
        print(f"Devices:        {p['old_devices']} old -> {p['new_devices']} new")
        print(f"Entities:       {p['old_entities']} old -> {p['new_entities']} new")
        print()
        print("Created:")
        print(f"  {args.json_output}")
        print(f"  {args.output}")
        return

    asyncio.run(audit_main())


if __name__ == "__main__":
    main()
