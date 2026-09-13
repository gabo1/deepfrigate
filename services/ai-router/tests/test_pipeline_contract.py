"""Capabilities read from the pipeline contract.

Two properties matter more than the mapping itself:

* the contract can only switch OFF what it describes — anything it does not
  mention keeps its environment value, so enabling the watcher can never
  silence a capability by omission;
* a broken or missing document changes nothing.
"""
import textwrap

import pytest

from app.pipeline_contract import (
    Capabilities,
    ContractWatcher,
    capabilities_from_contract,
)

ENV_EMBEDDING = frozenset({"car", "person"})
ENV_ATTRIBUTE = frozenset({"person", "car"})


def _caps(document):
    return capabilities_from_contract(
        document, env_embedding=ENV_EMBEDDING, env_attribute=ENV_ATTRIBUTE)


def _pipeline(*enrichments):
    return {"pipeline": {"enrichments": list(enrichments)}}


def test_enrichment_without_enabled_key_is_on() -> None:
    """The contract default. Reading it the other way round would switch off a
    running pipeline the first time the watcher fires."""
    caps = _caps(_pipeline(
        {"model": "vehicle-embedding", "family": "pp-shitu", "labels": ["car"]}))

    assert caps.embedding_labels == {"car"}


def test_disabled_embedding_stops_embedding_every_label() -> None:
    caps = _caps(_pipeline(
        {"model": "vehicle-embedding", "labels": ["car", "person"], "enabled": False}))

    assert caps.embedding_labels == frozenset()
    assert caps.attribute_labels == ENV_ATTRIBUTE


def test_contract_labels_are_the_authority_on_which_labels_embed() -> None:
    caps = _caps(_pipeline({"family": "pp-shitu", "labels": ["person"]}))

    assert caps.embedding_labels == {"person"}


def test_disabled_person_attributes_drop_only_that_label() -> None:
    caps = _caps(_pipeline(
        {"model": "person-attribute", "labels": ["person"], "enabled": False}))

    assert caps.attribute_labels == {"car"}


def test_vehicle_attributes_are_left_alone() -> None:
    """`vehicle-attribute` is the PULC head, which is off, while the car
    attributes that reach the UI come from the alpr-worker — and the SAME
    `attribute_labels` set gates both. Honouring this flag would silently switch
    off licence plates."""
    caps = _caps(_pipeline(
        {"model": "vehicle-attribute", "labels": ["car"], "enabled": False}))

    assert "car" in caps.attribute_labels


def test_an_enrichment_the_contract_omits_keeps_its_env_value() -> None:
    caps = _caps(_pipeline({"model": "algo-nuevo", "labels": ["dog"]}))

    assert caps.embedding_labels == ENV_EMBEDDING
    assert caps.attribute_labels == ENV_ATTRIBUTE


@pytest.mark.parametrize("document", [None, {}, {"pipeline": {}}, "texto suelto",
                                      {"pipeline": {"enrichments": "no es lista"}}])
def test_a_useless_document_changes_nothing(document) -> None:
    caps = _caps(document)

    assert caps.embedding_labels == ENV_EMBEDDING
    assert caps.attribute_labels == ENV_ATTRIBUTE
    assert caps.source == "env"


def test_matching_falls_back_to_the_family_when_the_model_is_renamed() -> None:
    caps = _caps(_pipeline(
        {"model": "embeddings-v2", "family": "pp-shitu", "labels": ["car"],
         "enabled": False}))

    assert caps.embedding_labels == frozenset()


# ── watcher ─────────────────────────────────────────────────────────────────

def _watcher(tmp_path, applied, text="pipeline:\n  enrichments: []\n"):
    path = tmp_path / "pipeline.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path, ContractWatcher(
        path, applied.append, env_embedding=ENV_EMBEDDING,
        env_attribute=ENV_ATTRIBUTE, interval=0.01)


def test_first_check_applies_and_the_second_does_not(tmp_path) -> None:
    applied: list[Capabilities] = []
    _, watcher = _watcher(tmp_path, applied)

    assert watcher.check_once() == "applied"
    assert watcher.check_once() == "unchanged"
    assert len(applied) == 1


def test_a_rewrite_applies_again(tmp_path) -> None:
    applied: list[Capabilities] = []
    path, watcher = _watcher(tmp_path, applied)
    watcher.check_once()

    path.write_text(
        "pipeline:\n  enrichments:\n  - model: vehicle-embedding\n"
        "    labels: [car]\n    enabled: false\n", encoding="utf-8")
    # mtime resolution can hide a rewrite within the same tick.
    watcher._mtime = None

    assert watcher.check_once() == "applied"
    assert applied[-1].embedding_labels == frozenset()


def test_broken_yaml_keeps_the_previous_capabilities(tmp_path) -> None:
    applied: list[Capabilities] = []
    path, watcher = _watcher(tmp_path, applied)
    watcher.check_once()

    path.write_text("pipeline: [esto: no cierra\n", encoding="utf-8")
    watcher._mtime = None

    assert watcher.check_once() == "invalid"
    assert len(applied) == 1, "no debe aplicarse nada con un documento roto"


def test_a_missing_file_changes_nothing(tmp_path) -> None:
    applied: list[Capabilities] = []
    watcher = ContractWatcher(
        tmp_path / "no-existe.yaml", applied.append, env_embedding=ENV_EMBEDDING,
        env_attribute=ENV_ATTRIBUTE)

    assert watcher.check_once() == "invalid"
    assert applied == []
