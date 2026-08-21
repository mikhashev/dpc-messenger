"""Every persisted place that names a provider alias, and renaming one across all of them."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

AGENT_CONFIG_KEYS = (
    "provider_alias",
    "sleep_provider_alias",
    "snapshot_summarize_provider",
    "compaction_provider",
)

PROVIDERS_ROLE_KEYS = (
    "default_provider",
    "vision_provider",
    "voice_provider",
    "agent_provider",
    "knowledge_provider",
)

_VOICE_SECTION = "voice_transcription"
_VOICE_KEY = "provider_priority"


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _agent_configs(home: Path) -> List[Tuple[str, Path]]:
    agents = home / "agents"
    if not agents.is_dir():
        return []
    found = []
    for entry in sorted(agents.iterdir()):
        config = entry / "config.json"
        if entry.is_dir() and config.is_file():
            found.append((entry.name, config))
    return found


def _voice_priority(home: Path) -> List[str]:
    ini = home / "config.ini"
    if not ini.is_file():
        return []
    section = None
    try:
        lines = ini.read_text(encoding="utf-8").split("\n")
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != _VOICE_SECTION:
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() == _VOICE_KEY:
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _rename_voice_priority(home: Path, old: str, new: str) -> int:
    ini = home / "config.ini"
    if not ini.is_file():
        return 0
    try:
        lines = ini.read_text(encoding="utf-8").split("\n")
    except OSError:
        return 0
    section = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != _VOICE_SECTION:
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() != _VOICE_KEY:
            continue
        items = [item.strip() for item in value.split(",") if item.strip()]
        hits = items.count(old)
        if not hits:
            return 0
        renamed = [new if item == old else item for item in items]
        lines[index] = f"{key}{sep} {','.join(renamed)}"
        try:
            ini.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not rewrite the voice priority list: %s", exc)
            return 0
        return hits
    return 0


def find_references(alias: str, home: Path) -> List[str]:
    """Locations naming `alias`, each as file:field."""
    found: List[str] = []
    if not alias:
        return found

    providers = _read_json(home / "providers.json")
    if isinstance(providers, dict):
        for key in PROVIDERS_ROLE_KEYS:
            if providers.get(key) == alias:
                found.append(f"providers.json:{key}")

    for agent_id, path in _agent_configs(home):
        config = _read_json(path)
        if not isinstance(config, dict):
            continue
        for key in AGENT_CONFIG_KEYS:
            if config.get(key) == alias:
                found.append(f"agents/{agent_id}/config.json:{key}")

    registry = _read_json(home / "agents" / "_registry.json")
    if isinstance(registry, dict):
        for agent_id, entry in (registry.get("agents") or {}).items():
            if isinstance(entry, dict) and entry.get("provider_alias") == alias:
                found.append(f"agents/_registry.json:{agent_id}.provider_alias")

    rules = _read_json(home / "privacy_rules.json")
    if isinstance(rules, dict):
        compute = rules.get("compute")
        if isinstance(compute, dict) and compute.get("serving_alias") == alias:
            found.append("privacy_rules.json:compute.serving_alias")

    if alias in _voice_priority(home):
        found.append(f"config.ini:[{_VOICE_SECTION}]{_VOICE_KEY}")

    return found


def rename_references(old: str, new: str, home: Path) -> Dict[str, Any]:
    """Rewrite every persisted reference from `old` to `new`; the caller owns providers.json itself."""
    result: Dict[str, Any] = {
        "agent_configs": 0,
        "registry": 0,
        "firewall": 0,
        "voice_priority": 0,
        "agent_ids": [],
    }
    if not old or not new or old == new:
        return result

    for agent_id, path in _agent_configs(home):
        config = _read_json(path)
        if not isinstance(config, dict):
            continue
        changed = 0
        for key in AGENT_CONFIG_KEYS:
            if config.get(key) == old:
                config[key] = new
                changed += 1
        if changed:
            try:
                _write_json(path, config)
            except OSError as exc:
                logger.warning("Could not rewrite %s: %s", path, exc)
                continue
            result["agent_configs"] += changed
            result["agent_ids"].append(agent_id)

    registry_path = home / "agents" / "_registry.json"
    registry = _read_json(registry_path)
    if isinstance(registry, dict):
        changed = 0
        for entry in (registry.get("agents") or {}).values():
            if isinstance(entry, dict) and entry.get("provider_alias") == old:
                entry["provider_alias"] = new
                changed += 1
        if changed:
            try:
                _write_json(registry_path, registry)
                result["registry"] = changed
            except OSError as exc:
                logger.warning("Could not rewrite %s: %s", registry_path, exc)

    rules_path = home / "privacy_rules.json"
    rules = _read_json(rules_path)
    if isinstance(rules, dict):
        compute = rules.get("compute")
        if isinstance(compute, dict) and compute.get("serving_alias") == old:
            compute["serving_alias"] = new
            try:
                _write_json(rules_path, rules)
                result["firewall"] = 1
            except OSError as exc:
                logger.warning("Could not rewrite %s: %s", rules_path, exc)

    result["voice_priority"] = _rename_voice_priority(home, old, new)
    return result


def unresolved_references(known_aliases, home: Path) -> List[str]:
    """References naming an alias that `known_aliases` does not contain — an agent cut off from its provider."""
    known = set(known_aliases or ())
    dangling: List[str] = []

    for agent_id, path in _agent_configs(home):
        config = _read_json(path)
        if not isinstance(config, dict):
            continue
        for key in AGENT_CONFIG_KEYS:
            value = config.get(key)
            if value and value not in known:
                dangling.append(f"agents/{agent_id}/config.json:{key} → '{value}'")

    rules = _read_json(home / "privacy_rules.json")
    if isinstance(rules, dict):
        compute = rules.get("compute")
        if isinstance(compute, dict):
            value = compute.get("serving_alias")
            if value and value not in known:
                dangling.append(f"privacy_rules.json:compute.serving_alias → '{value}'")

    return dangling
