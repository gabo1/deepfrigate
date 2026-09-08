import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator

from app.rules import RuleEngine, RuleError, RuleSet, Schedule, derived_event, rule_update

SCHEMA = next(
    p / "contracts/event.schema.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/event.schema.json").exists()
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA.read_text()))
TZ = ZoneInfo("America/Mexico_City")


def local(day: int, hour: int, minute: int = 0) -> float:
    # September 2026: the 7th is a Monday.
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ).timestamp()


def event(event_type: str, data: dict, *, camera: str = "user", ts: float | None = None, track: int = 7) -> dict:
    return {
        "type": "event",
        "id": f"src-{event_type}-{track}-{ts}",
        "event_type": event_type,
        "object_id": f"{camera}-{track}",
        "camera_id": camera,
        "track_id": track,
        "timestamp": local(7, 12) if ts is None else ts,
        "source_update_type": {"dwell_time": "zone", "object_entered_zone": "zone", "overcrowding": "overcrowding", "direction_match": "direction", "object_detected": "detection"}[event_type],
        "severity": "info",
        "data": data,
    }


RULES = """
version: 1
rules:
  - name: merodeo
    when: {event_type: [dwell_time], label: person, zone: [calle], min_dwell_seconds: 30}
    cooldown: {seconds: 120}
    then: {severity: warning, sub_label: Merodeo, message: "{label} {dwell_time}s en {zone} ({camera})"}
  - name: nocturna
    when:
      event_type: [object_entered_zone]
      label: [person]
      schedule: {between: ["22:00", "06:00"], timezone: America/Mexico_City}
    then: {severity: critical}
  - name: aforo
    when: {event_type: overcrowding, min_count: 3}
    cooldown: {seconds: 300, scope: camera}
    then: {severity: critical, message: "{count}/{threshold} en {zone}"}
  - name: apagada
    enabled: false
    when: {}
    then: {severity: info}
"""


def test_rule_matching_filters_and_thresholds() -> None:
    ruleset = RuleSet.from_text(RULES)
    merodeo = ruleset.rules[0]
    assert merodeo.matches(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 31.2}))
    assert not merodeo.matches(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 12}))
    assert not merodeo.matches(event("dwell_time", {"label": "car", "zone": "calle", "dwell_time": 60}))
    assert not merodeo.matches(event("dwell_time", {"label": "person", "zone": "banqueta", "dwell_time": 60}))
    assert not merodeo.matches(event("object_entered_zone", {"label": "person", "zone": "calle"}))
    # dwell missing entirely: a floor cannot be satisfied.
    assert not merodeo.matches(event("dwell_time", {"label": "person", "zone": "calle"}))
    assert [rule.name for rule in ruleset.enabled] == ["merodeo", "nocturna", "aforo"]


def test_schedule_wraps_midnight_and_respects_weekdays() -> None:
    window = Schedule.parse({"between": ["22:00", "06:00"]}, "x")
    assert window.matches(local(7, 23, 30))
    assert window.matches(local(8, 5, 59))
    assert not window.matches(local(8, 6, 0))
    assert not window.matches(local(8, 12))
    weekdays = Schedule.parse({"days": ["mon", "tue"], "between": ["22:00", "06:00"]}, "x")
    assert weekdays.matches(local(7, 23))          # Monday night
    assert weekdays.matches(local(8, 2))           # still Monday's window
    assert weekdays.matches(local(9, 1))           # Tuesday's window spills into Wednesday
    assert not weekdays.matches(local(10, 23))     # Wednesday night
    day = Schedule.parse({"between": ["09:00", "18:00"]}, "x")
    assert day.matches(local(7, 9)) and not day.matches(local(7, 18))


def test_derived_event_is_valid_idempotent_and_carries_context() -> None:
    rule = RuleSet.from_text(RULES).rules[0]
    source = event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 45.5, "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}})
    derived = derived_event(rule, source)
    VALIDATOR.validate(derived)
    assert derived["event_type"] == "rule_matched" and derived["severity"] == "warning"
    assert derived["data"]["rule"] == "merodeo" and derived["data"]["sub_label"] == "Merodeo"
    assert derived["data"]["message"] == "person 45.5s en calle (user)"
    assert derived["data"]["source_event_id"] == source["id"] and derived["data"]["zone"] == "calle"
    assert derived["id"] == derived_event(rule, source)["id"]
    companion = rule_update({"update_type": "zone"}, derived)
    assert companion["update_type"] == "custom" and companion["data"]["kind"] == "rule"
    assert companion["data"]["sub_label"] == "Merodeo" and companion["object_id"] == "user-7"


def test_engine_reloads_on_change_and_keeps_rules_on_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(RULES)
    now = [1000.0]
    engine = RuleEngine(path, reload_seconds=2, clock=lambda: now[0])
    assert engine.summary()["enabled"] == ["merodeo", "nocturna", "aforo"]

    path.write_text("rules: [{name: 'bad name!', then: {severity: warning}}]")
    import os
    os.utime(path, (now[0] + 10, now[0] + 10))
    now[0] += 5
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40}))
    assert engine.summary()["enabled"] == ["merodeo", "nocturna", "aforo"]  # previous set kept

    path.write_text("version: 1\nrules: []\n")
    os.utime(path, (now[0] + 20, now[0] + 20))
    now[0] += 5
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40}, track=8)) == []
    assert engine.summary()["rules"] == 0

    path.unlink()
    now[0] += 5
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40}, track=9)) == []


def test_engine_cooldown_per_object_and_per_camera(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(RULES)
    engine = RuleEngine(path, clock=lambda: 0.0)
    base = local(7, 12)
    first = engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 30}, ts=base))
    assert [item["data"]["rule"] for item in first] == ["merodeo"]
    # Same object 10 s later: suppressed. Another object: fires.
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40}, ts=base + 10)) == []
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40}, ts=base + 10, track=8))
    # After the cooldown the same object fires again.
    assert engine.evaluate(event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 200}, ts=base + 130))
    # Camera scope: a second zone on the same camera is suppressed.
    crowd = engine.evaluate(event("overcrowding", {"label": "person", "zone": "calle", "count": 4, "threshold": 3}, ts=base))
    assert crowd and crowd[0]["data"]["message"] == "4/3 en calle"
    assert engine.evaluate(event("overcrowding", {"label": "person", "zone": "caja", "count": 5, "threshold": 3}, ts=base + 1, track=9)) == []
    assert engine.evaluate(event("overcrowding", {"label": "person", "zone": "caja", "count": 5, "threshold": 3}, ts=base + 1, track=9, camera="tienda"))
    assert engine.summary()["matched"] == 5 and engine.summary()["suppressed"] == 2


def test_night_rule_uses_local_time() -> None:
    engine = RuleSet.from_text(RULES).rules[1]
    assert engine.matches(event("object_entered_zone", {"label": "person", "zone": "calle"}, ts=local(7, 23)))
    assert not engine.matches(event("object_entered_zone", {"label": "person", "zone": "calle"}, ts=local(7, 15)))


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("rules: [{name: 'x y', then: {}}]", "must be [A-Za-z0-9_-]+"),
        ("rules: [{name: a, then: {severity: loud}}]", "severity"),
        ("rules: [{name: a, when: {zone: []}}]", "non-empty list"),
        ("rules: [{name: a, when: {schedule: {between: ['25:00', '06:00']}}}]", "HH:MM"),
        ("rules: [{name: a, when: {schedule: {days: [lunes]}}}]", "unknown weekday"),
        ("rules: [{name: a, when: {schedule: {timezone: Mars/Olympus}}}]", "unknown timezone"),
        ("rules: [{name: a, when: {colour: red}}]", "unknown `when` keys"),
        ("rules: [{name: a}, {name: a}]", "duplicate"),
        ("rules: [{name: a, cooldown: {scope: planet}}]", "cooldown.scope"),
        ("rules: [{name: a, when: {stationary: 'yes'}}]", "true/false"),
        ("version: 2\nrules: []", "unsupported"),
        ("rules: {a: 1}", "must be a list"),
        ("- not\n- a mapping", "mapping"),
        ("rules: [{name: a, when: {min_count: many}}]", "must be a number"),
        ("rules: [\n  bad: yaml", "invalid YAML"),
    ],
)
def test_rule_file_errors_name_the_problem(text: str, fragment: str) -> None:
    with pytest.raises(RuleError) as error:
        RuleSet.from_text(text)
    assert fragment in str(error.value)


def test_checked_in_rules_file_parses() -> None:
    path = next(
        p / "config/rules/rules.yaml"
        for p in Path(__file__).resolve().parents
        if (p / "config/rules/rules.yaml").exists()
    )
    ruleset = RuleSet.from_text(path.read_text(encoding="utf-8"), source=str(path))
    assert {"merodeo_calle", "persona_nocturna", "aforo_excedido"} <= {r.name for r in ruleset.enabled}
    for rule in ruleset.rules:
        VALIDATOR.validate(derived_event(rule, event("dwell_time", {"label": "person", "zone": "calle", "dwell_time": 40})))
