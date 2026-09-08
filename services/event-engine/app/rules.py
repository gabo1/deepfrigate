"""Declarative rules over normalized events.

A rule file (YAML) turns the stream of normalized events into business alerts
without code: "a person that stays 30 s in `calle` between 22:00 and 06:00 is
critical", "a car crossing `cruce` inbound on weekends is a warning". Each match
emits one derived event of type `rule_matched` that is persisted, published on
MQTT like any other event and, when the rule asks for it, labels the Frigate
event (`sub_label`).

Rules are evaluated on the MQTT thread, so everything here is dictionary
lookups and clock arithmetic. The file is re-read when its mtime changes
(checked at most every `reload_seconds`), a broken file keeps the previous
rule set and is logged once per version.

Rule shape::

    version: 1
    rules:
      - name: persona_nocturna_calle          # unique, [a-z0-9_-]
        enabled: true
        when:
          event_type: [object_entered_zone, dwell_time]   # any of
          camera: [user]                                   # any of
          label: [person]                                  # data.label
          zone: [calle]                                    # data.zone
          line: [cruce]                                    # data.line
          direction: [hacia_arriba]                        # data.direction
          min_dwell_seconds: 30                            # data.dwell_time >=
          min_count: 3                                     # data.count >=
          min_confidence: 0.6                              # data.confidence >=
          stationary: true                                 # data.stationary ==
          schedule:
            days: [mon, tue, wed, thu, fri]                # local weekday
            between: ["22:00", "06:00"]                    # wraps midnight
            timezone: America/Mexico_City
        cooldown:
          seconds: 60                                      # suppress repeats
          scope: object                                    # object|camera|rule
        then:
          severity: critical                               # info|warning|critical
          sub_label: "Persona nocturna"                    # Frigate sub_label
          message: "{label} en {zone} ({camera}) {dwell_time}s"

Every `when` key is optional; an empty `when` matches every event.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("event-engine.rules")

RULE_EVENT_TYPE = "rule_matched"
SEVERITIES = ("info", "warning", "critical")
COOLDOWN_SCOPES = ("object", "camera", "rule")
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DEFAULT_TIMEZONE = os.getenv("RULES_TIMEZONE", "America/Mexico_City")
# Message placeholders come from the event; anything missing renders as "-".
_MESSAGE_FIELDS = (
    "label",
    "zone",
    "line",
    "direction",
    "camera",
    "event_type",
    "dwell_time",
    "count",
    "threshold",
    "confidence",
    "object_id",
)


class RuleError(ValueError):
    """The rule file is not usable; the message names the rule and the key."""


def _as_set(value: Any, key: str, rule: str) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise RuleError(f"rule {rule!r}: `{key}` must be a value or a non-empty list")
    return frozenset(str(item) for item in value)


def _as_number(value: Any, key: str, rule: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise RuleError(f"rule {rule!r}: `{key}` must be a number") from error


def _parse_clock(value: Any, key: str, rule: str) -> time:
    try:
        hours, minutes = str(value).split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError) as error:
        raise RuleError(f"rule {rule!r}: `{key}` must be HH:MM, got {value!r}") from error


@dataclass(frozen=True)
class Schedule:
    """Local-time window; `start > end` wraps past midnight (22:00-06:00)."""

    days: frozenset[int] | None = None
    start: time | None = None
    end: time | None = None
    timezone: str = DEFAULT_TIMEZONE

    @classmethod
    def parse(cls, raw: Any, rule: str) -> "Schedule | None":
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RuleError(f"rule {rule!r}: `schedule` must be a mapping")
        days = None
        if raw.get("days") is not None:
            names = raw["days"] if isinstance(raw["days"], list) else [raw["days"]]
            try:
                days = frozenset(WEEKDAYS[str(name).lower()[:3]] for name in names)
            except KeyError as error:
                raise RuleError(f"rule {rule!r}: unknown weekday {error.args[0]!r}") from None
        start = end = None
        if raw.get("between") is not None:
            between = raw["between"]
            if not isinstance(between, (list, tuple)) or len(between) != 2:
                raise RuleError(f"rule {rule!r}: `between` must be [HH:MM, HH:MM]")
            start = _parse_clock(between[0], "between", rule)
            end = _parse_clock(between[1], "between", rule)
        timezone = str(raw.get("timezone") or DEFAULT_TIMEZONE)
        try:
            ZoneInfo(timezone)
        except Exception as error:  # noqa: BLE001 - ZoneInfoNotFoundError or ValueError
            raise RuleError(f"rule {rule!r}: unknown timezone {timezone!r}") from error
        return cls(days=days, start=start, end=end, timezone=timezone)

    def matches(self, timestamp: float) -> bool:
        local = datetime.fromtimestamp(timestamp, ZoneInfo(self.timezone))
        if self.days is not None and local.weekday() not in self.days:
            # A window that wraps midnight belongs to the day it started on.
            if not (
                self.start is not None
                and self.end is not None
                and self.start > self.end
                and local.time() < self.end
                and (local.weekday() - 1) % 7 in self.days
            ):
                return False
        if self.start is None or self.end is None:
            return True
        now = local.time()
        if self.start <= self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end


@dataclass(frozen=True)
class Rule:
    name: str
    enabled: bool = True
    event_types: frozenset[str] | None = None
    cameras: frozenset[str] | None = None
    labels: frozenset[str] | None = None
    zones: frozenset[str] | None = None
    lines: frozenset[str] | None = None
    directions: frozenset[str] | None = None
    min_dwell_seconds: float | None = None
    min_count: float | None = None
    min_confidence: float | None = None
    stationary: bool | None = None
    schedule: Schedule | None = None
    cooldown_seconds: float = 0.0
    cooldown_scope: str = "object"
    severity: str = "warning"
    sub_label: str | None = None
    message: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Rule":
        if not isinstance(raw, dict):
            raise RuleError("each rule must be a mapping")
        name = str(raw.get("name") or "").strip()
        if not name or not all(ch.isalnum() or ch in "_-" for ch in name):
            raise RuleError(f"rule name {name!r} must be [A-Za-z0-9_-]+")
        when = raw.get("when") or {}
        then = raw.get("then") or {}
        cooldown = raw.get("cooldown") or {}
        for section, value in (("when", when), ("then", then), ("cooldown", cooldown)):
            if not isinstance(value, dict):
                raise RuleError(f"rule {name!r}: `{section}` must be a mapping")
        unknown = set(when) - {
            "event_type", "camera", "label", "zone", "line", "direction",
            "min_dwell_seconds", "min_count", "min_confidence", "stationary", "schedule",
        }
        if unknown:
            raise RuleError(f"rule {name!r}: unknown `when` keys {sorted(unknown)}")
        severity = str(then.get("severity") or "warning")
        if severity not in SEVERITIES:
            raise RuleError(f"rule {name!r}: severity must be one of {SEVERITIES}")
        scope = str(cooldown.get("scope") or "object")
        if scope not in COOLDOWN_SCOPES:
            raise RuleError(f"rule {name!r}: cooldown.scope must be one of {COOLDOWN_SCOPES}")
        stationary = when.get("stationary")
        if stationary is not None and not isinstance(stationary, bool):
            raise RuleError(f"rule {name!r}: `stationary` must be true/false")
        return cls(
            name=name,
            enabled=bool(raw.get("enabled", True)),
            event_types=_as_set(when.get("event_type"), "event_type", name),
            cameras=_as_set(when.get("camera"), "camera", name),
            labels=_as_set(when.get("label"), "label", name),
            zones=_as_set(when.get("zone"), "zone", name),
            lines=_as_set(when.get("line"), "line", name),
            directions=_as_set(when.get("direction"), "direction", name),
            min_dwell_seconds=_as_number(when.get("min_dwell_seconds"), "min_dwell_seconds", name),
            min_count=_as_number(when.get("min_count"), "min_count", name),
            min_confidence=_as_number(when.get("min_confidence"), "min_confidence", name),
            stationary=stationary,
            schedule=Schedule.parse(when.get("schedule"), name),
            cooldown_seconds=_as_number(cooldown.get("seconds"), "cooldown.seconds", name) or 0.0,
            cooldown_scope=scope,
            severity=severity,
            sub_label=(str(then["sub_label"]).strip() or None) if then.get("sub_label") else None,
            message=str(then["message"]) if then.get("message") else None,
        )

    def matches(self, event: dict[str, Any]) -> bool:
        data = event.get("data") or {}
        checks = (
            (self.event_types, event.get("event_type")),
            (self.cameras, event.get("camera_id")),
            (self.labels, data.get("label")),
            (self.zones, data.get("zone")),
            (self.lines, data.get("line")),
            (self.directions, data.get("direction")),
        )
        for wanted, actual in checks:
            if wanted is not None and str(actual) not in wanted:
                return False
        numeric = (
            (self.min_dwell_seconds, data.get("dwell_time")),
            (self.min_count, data.get("count")),
            (self.min_confidence, data.get("confidence")),
        )
        for floor, actual in numeric:
            if floor is None:
                continue
            try:
                if float(actual) < floor:
                    return False
            except (TypeError, ValueError):
                return False
        if self.stationary is not None and bool(data.get("stationary")) != self.stationary:
            return False
        if self.schedule is not None and not self.schedule.matches(float(event["timestamp"])):
            return False
        return True

    def cooldown_key(self, event: dict[str, Any]) -> tuple[str, ...]:
        if self.cooldown_scope == "object":
            return (self.name, str(event.get("object_id")))
        if self.cooldown_scope == "camera":
            return (self.name, str(event.get("camera_id")))
        return (self.name,)

    def render_message(self, event: dict[str, Any]) -> str:
        data = event.get("data") or {}
        values = {key: data.get(key) for key in _MESSAGE_FIELDS}
        values.update(
            camera=event.get("camera_id"),
            event_type=event.get("event_type"),
            object_id=event.get("object_id"),
        )
        clean = {key: ("-" if value is None else value) for key, value in values.items()}
        template = self.message or "{event_type} {label} {camera}"
        try:
            return template.format_map(_Defaulting(clean))
        except (ValueError, IndexError):
            return template


class _Defaulting(dict):
    def __missing__(self, key: str) -> str:
        return "-"


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...] = ()
    digest: str = ""
    source: str = ""

    @classmethod
    def from_document(cls, document: Any, source: str = "") -> "RuleSet":
        if document is None:
            document = {}
        if not isinstance(document, dict):
            raise RuleError("rules file must be a mapping with `rules:`")
        version = document.get("version", 1)
        if int(version) != 1:
            raise RuleError(f"unsupported rules version {version!r}")
        raw_rules = document.get("rules") or []
        if not isinstance(raw_rules, list):
            raise RuleError("`rules` must be a list")
        rules = tuple(Rule.parse(item) for item in raw_rules)
        names = [rule.name for rule in rules]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise RuleError(f"duplicate rule names {duplicates}")
        return cls(rules=rules, digest=_digest(document), source=source)

    @classmethod
    def from_text(cls, text: str, source: str = "") -> "RuleSet":
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise RuleError(f"invalid YAML: {error}") from error
        return cls.from_document(document, source=source)

    @property
    def enabled(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled)

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "digest": self.digest,
            "rules": len(self.rules),
            "enabled": [rule.name for rule in self.enabled],
        }


def _digest(document: Any) -> str:
    return hashlib.sha256(
        yaml.safe_dump(document, sort_keys=True, default_flow_style=False).encode()
    ).hexdigest()[:12]


def derived_event(rule: Rule, event: dict[str, Any]) -> dict[str, Any]:
    """One `rule_matched` event per (rule, source event). Idempotent id."""
    data = event.get("data") or {}
    payload: dict[str, Any] = {
        "rule": rule.name,
        "message": rule.render_message(event),
        "source_event_type": event["event_type"],
        "source_event_id": event["id"],
        "label": data.get("label"),
    }
    for key in ("zone", "line", "direction", "dwell_time", "count", "threshold", "confidence", "bbox"):
        if data.get(key) is not None:
            payload[key] = data[key]
    if rule.sub_label:
        payload["sub_label"] = rule.sub_label
    return {
        "type": "event",
        "id": str(uuid5(NAMESPACE_URL, f"deepfrigate:rule:{rule.name}:{event['id']}")),
        "event_type": RULE_EVENT_TYPE,
        "object_id": event["object_id"],
        "camera_id": event["camera_id"],
        "track_id": int(event["track_id"]),
        "timestamp": float(event["timestamp"]),
        "source_update_type": event["source_update_type"],
        "severity": rule.severity,
        "data": payload,
    }


def rule_update(update: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    """The queue companion of a derived event for the Frigate bridge.

    `update_type: custom` is in the tracked-object contract; the bridge only
    reacts to `data.kind == "rule"` (Frigate `sub_label`).
    """
    return {
        "type": "tracked_object_update",
        "object_id": derived["object_id"],
        "camera_id": derived["camera_id"],
        "track_id": derived["track_id"],
        "timestamp": derived["timestamp"],
        "update_type": "custom",
        "data": {"kind": "rule", **derived["data"]},
    }


class RuleEngine:
    """Hot-reloading rule set with per-rule cooldowns."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        reload_seconds: float = 2.0,
        clock: Callable[[], float] = _time.time,
    ) -> None:
        self.path = Path(path) if path else None
        self.reload_seconds = reload_seconds
        self.clock = clock
        self.ruleset = RuleSet()
        self._mtime: float | None = None
        self._checked_at = 0.0
        self._failed_mtime: float | None = None
        self._last_fired: dict[tuple[str, ...], float] = {}
        self.matched = 0
        self.suppressed = 0
        self.reload(force=True)

    # ------------------------------------------------------------- loading
    def reload(self, force: bool = False) -> bool:
        """Re-read the file when its mtime changed. Returns True on change."""
        if self.path is None:
            return False
        now = self.clock()
        if not force and now - self._checked_at < self.reload_seconds:
            return False
        self._checked_at = now
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None or force:
                logger.warning("Rules file %s missing; no rules active", self.path)
            self._mtime = None
            self.ruleset = RuleSet(source=str(self.path))
            return True
        if not force and mtime == self._mtime:
            return False
        if mtime == self._failed_mtime:
            return False
        try:
            ruleset = RuleSet.from_text(self.path.read_text(encoding="utf-8"), source=str(self.path))
        except (RuleError, OSError, ValueError) as error:
            self._failed_mtime = mtime
            logger.error("Rules file %s rejected, keeping %d previous rule(s): %s", self.path, len(self.ruleset.rules), error)
            return False
        self._mtime = mtime
        self._failed_mtime = None
        self.ruleset = ruleset
        self._last_fired = {
            key: fired for key, fired in self._last_fired.items()
            if any(rule.name == key[0] for rule in ruleset.rules)
        }
        logger.info("Rules loaded: %s", ruleset.summary())
        return True

    # ---------------------------------------------------------- evaluation
    def evaluate(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.reload()
        if not self.ruleset.rules:
            return []
        out: list[dict[str, Any]] = []
        for rule in self.ruleset.enabled:
            if not rule.matches(event):
                continue
            if rule.cooldown_seconds > 0:
                key = rule.cooldown_key(event)
                fired = self._last_fired.get(key)
                timestamp = float(event["timestamp"])
                if fired is not None and 0 <= timestamp - fired < rule.cooldown_seconds:
                    self.suppressed += 1
                    continue
                self._last_fired[key] = timestamp
                if len(self._last_fired) > 10000:
                    self._prune(timestamp)
            self.matched += 1
            out.append(derived_event(rule, event))
        return out

    def _prune(self, now: float) -> None:
        horizon = max((rule.cooldown_seconds for rule in self.ruleset.rules), default=0.0)
        self._last_fired = {
            key: fired for key, fired in self._last_fired.items() if now - fired < horizon
        }

    def summary(self) -> dict[str, Any]:
        return {**self.ruleset.summary(), "matched": self.matched, "suppressed": self.suppressed}


def engine_from_env() -> RuleEngine | None:
    path = os.getenv("RULES_CONFIG", "/app/config/rules/rules.yaml")
    if not path or os.getenv("RULES_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None
    return RuleEngine(path, reload_seconds=float(os.getenv("RULES_RELOAD_SECONDS", "2")))
