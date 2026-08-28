"""Every persisted place that names a provider alias, and renaming one across all of them.

The list of places is data, and there is exactly one of it. It used to be three
hand-written walks — one inside `find_references`, one inside `rename_references`,
one inside `unresolved_references` — and they had already drifted apart: the agent
registry was renamed but never reported as dangling, and a second key of
`config.ini` was in none of them at all. One difference between the walks turned
out to be deliberate and is kept as a flag rather than flattened: the voice
priority list is not checked for dangling names because it legitimately holds
backend names beside aliases. The fixture said so by going red. A test written from any one
of those walks certifies what its author remembered, which is why the fixture for
this module could not notice that `[knowledge] cold_fallback_provider` was missing
(GLM 5.3 and Fable 5, 2026-08-22 reviews; Mike, 2026-08-23: «инвентарь как данные»).

Adding a persisted key is now one `Place` in `PLACES`, and the fixture walks the
same list, so a key that is not carried is a failing test rather than a silence.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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


# --- config.ini, line-wise ---------------------------------------------------
# Rewritten line by line rather than through configparser on purpose: the file is
# hand-edited and carries comments and spacing that a round-trip through
# configparser would silently discard.

def _ini_values(home: Path, section: str, key: str, is_list: bool) -> List[str]:
    ini = home / "config.ini"
    if not ini.is_file():
        return []
    try:
        lines = ini.read_text(encoding="utf-8").split("\n")
    except OSError:
        return []
    current = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            continue
        if current != section:
            continue
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            if is_list:
                return [item.strip() for item in value.split(",") if item.strip()]
            return [value.strip()] if value.strip() else []
    return []


def _rename_ini_value(home: Path, section: str, key: str, is_list: bool,
                      old: str, new: str) -> int:
    ini = home / "config.ini"
    if not ini.is_file():
        return 0
    try:
        lines = ini.read_text(encoding="utf-8").split("\n")
    except OSError:
        return 0
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            continue
        if current != section:
            continue
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        if is_list:
            items = [item.strip() for item in value.split(",") if item.strip()]
            hits = items.count(old)
            if not hits:
                return 0
            rewritten = ",".join(new if item == old else item for item in items)
        else:
            if value.strip() != old:
                return 0
            hits, rewritten = 1, new
        lines[index] = f"{name}{sep} {rewritten}"
        try:
            ini.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not rewrite config.ini [%s]%s: %s", section, key, exc)
            return 0
        return hits
    return 0


# --- the inventory -----------------------------------------------------------

@dataclass(frozen=True)
class Place:
    """One persisted location that can name a provider alias.

    `scan` answers «which aliases does this place name, and under what label»,
    and every other function in this module is built on it — which is what stops
    the three walks drifting apart again. `rename` returns the number of values
    it rewrote and any agent ids it touched.
    """

    key: str
    scan: Callable[[Path], List[Tuple[str, str]]]
    rename: Callable[[Path, str, str], Tuple[int, List[str]]]
    # providers.json is the one place the caller rewrites itself — it owns the
    # alias being renamed — so this module reports it and never writes it, and a
    # role naming an unknown alias is the caller's business rather than a
    # dangling reference of ours.
    writable: bool = True
    in_unresolved: bool = True
    legacy_counter: str = ""


def _scan_providers_roles(home: Path) -> List[Tuple[str, str]]:
    providers = _read_json(home / "providers.json")
    if not isinstance(providers, dict):
        return []
    return [(f"providers.json:{key}", providers[key])
            for key in PROVIDERS_ROLE_KEYS if providers.get(key)]


def _scan_agent_configs(home: Path) -> List[Tuple[str, str]]:
    found = []
    for agent_id, path in _agent_configs(home):
        config = _read_json(path)
        if not isinstance(config, dict):
            continue
        for key in AGENT_CONFIG_KEYS:
            if config.get(key):
                found.append((f"agents/{agent_id}/config.json:{key}", config[key]))
    return found


def _rename_agent_configs(home: Path, old: str, new: str) -> Tuple[int, List[str]]:
    total, touched = 0, []
    for agent_id, path in _agent_configs(home):
        config = _read_json(path)
        if not isinstance(config, dict):
            continue
        changed = sum(1 for key in AGENT_CONFIG_KEYS if config.get(key) == old)
        if not changed:
            continue
        for key in AGENT_CONFIG_KEYS:
            if config.get(key) == old:
                config[key] = new
        try:
            _write_json(path, config)
        except OSError as exc:
            logger.warning("Could not rewrite %s: %s", path, exc)
            continue
        total += changed
        touched.append(agent_id)
    return total, touched


def _scan_registry(home: Path) -> List[Tuple[str, str]]:
    registry = _read_json(home / "agents" / "_registry.json")
    if not isinstance(registry, dict):
        return []
    return [(f"agents/_registry.json:{agent_id}.provider_alias", entry["provider_alias"])
            for agent_id, entry in (registry.get("agents") or {}).items()
            if isinstance(entry, dict) and entry.get("provider_alias")]


def _rename_registry(home: Path, old: str, new: str) -> Tuple[int, List[str]]:
    path = home / "agents" / "_registry.json"
    registry = _read_json(path)
    if not isinstance(registry, dict):
        return 0, []
    entries = [e for e in (registry.get("agents") or {}).values()
               if isinstance(e, dict) and e.get("provider_alias") == old]
    if not entries:
        return 0, []
    for entry in entries:
        entry["provider_alias"] = new
    try:
        _write_json(path, registry)
    except OSError as exc:
        logger.warning("Could not rewrite %s: %s", path, exc)
        return 0, []
    return len(entries), []


def _scan_firewall(home: Path) -> List[Tuple[str, str]]:
    rules = _read_json(home / "privacy_rules.json")
    if not isinstance(rules, dict):
        return []
    compute = rules.get("compute")
    if isinstance(compute, dict) and compute.get("serving_alias"):
        return [("privacy_rules.json:compute.serving_alias", compute["serving_alias"])]
    return []


def _rename_firewall(home: Path, old: str, new: str) -> Tuple[int, List[str]]:
    path = home / "privacy_rules.json"
    rules = _read_json(path)
    if not isinstance(rules, dict):
        return 0, []
    compute = rules.get("compute")
    if not isinstance(compute, dict) or compute.get("serving_alias") != old:
        return 0, []
    compute["serving_alias"] = new
    try:
        _write_json(path, rules)
    except OSError as exc:
        logger.warning("Could not rewrite %s: %s", path, exc)
        return 0, []
    return 1, []


def _ini_place(section: str, key: str, is_list: bool, legacy: str = "",
               in_unresolved: bool = True) -> Place:
    return Place(
        key=f"config.ini:[{section}]{key}",
        scan=lambda home: [(f"config.ini:[{section}]{key}", value)
                           for value in _ini_values(home, section, key, is_list)],
        rename=lambda home, old, new: (
            _rename_ini_value(home, section, key, is_list, old, new), []
        ),
        legacy_counter=legacy,
        in_unresolved=in_unresolved,
    )


PLACES: Tuple[Place, ...] = (
    Place(
        key="providers.json:roles",
        scan=_scan_providers_roles,
        rename=lambda home, old, new: (0, []),
        writable=False,
        in_unresolved=False,
    ),
    Place(
        key="agents/*/config.json",
        scan=_scan_agent_configs,
        rename=_rename_agent_configs,
        legacy_counter="agent_configs",
    ),
    Place(
        key="agents/_registry.json",
        scan=_scan_registry,
        rename=_rename_registry,
        legacy_counter="registry",
    ),
    Place(
        key="privacy_rules.json:compute.serving_alias",
        scan=_scan_firewall,
        rename=_rename_firewall,
        legacy_counter="firewall",
    ),
    # Out of the dangling check, and this is a decision rather than an omission:
    # the priority list mixes provider aliases with backend names (`openai`,
    # `whisper-large-v3-turbo`), so an entry the provider list does not contain is
    # ordinary rather than broken. A rename still follows it; only the report
    # stays out, because a check that cries about every backend teaches people to
    # stop reading it.
    _ini_place("voice_transcription", "provider_priority", True,
               legacy="voice_priority", in_unresolved=False),
    _ini_place("knowledge", "cold_fallback_provider", False),
)


def find_references(alias: str, home: Path) -> List[str]:
    """Locations naming `alias`, each as file:field."""
    if not alias:
        return []
    return [where for place in PLACES
            for where, value in place.scan(home) if value == alias]


def rename_references(old: str, new: str, home: Path) -> Dict[str, Any]:
    """Rewrite every persisted reference from `old` to `new`; the caller owns providers.json itself."""
    result: Dict[str, Any] = {
        "agent_configs": 0,
        "registry": 0,
        "firewall": 0,
        "voice_priority": 0,
        "agent_ids": [],
        # Per place, so a place added to PLACES is counted without anyone
        # remembering to add a field here or a term to the caller's sum.
        "by_place": {},
    }
    if not old or not new or old == new:
        return result

    for place in PLACES:
        if not place.writable:
            continue
        count, agent_ids = place.rename(home, old, new)
        result["by_place"][place.key] = count
        result["agent_ids"].extend(agent_ids)
        if place.legacy_counter:
            result[place.legacy_counter] = count
    return result


def unresolved_references(known_aliases, home: Path) -> List[str]:
    """References naming an alias that `known_aliases` does not contain — an agent cut off from its provider."""
    known = set(known_aliases or ())
    return [f"{where} → '{value}'"
            for place in PLACES if place.in_unresolved
            for where, value in place.scan(home) if value and value not in known]
