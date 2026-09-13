"""Capabilities read from the live pipeline contract instead of the environment.

The router freezes its configuration at startup from env vars, so turning an
enrichment off means editing ``compose.yaml`` and restarting the container. The
pipeline contract already declares the same thing per enrichment::

    enrichments:
      - model: vehicle-embedding
        family: pp-shitu
        labels: [car, person]
      - model: person-attribute
        family: pulc-person
        labels: [person]
        enabled: false

but nothing read that ``enabled`` flag, so the console could not act on it.
This module closes that gap: it turns the contract into a ``Capabilities``
snapshot and watches the file for changes, the same way ``video-engine``
watches it with ``ConfigWatcher``.

**Off by default** (``PIPELINE_CONTRACT_PATH`` empty): without it, the router
behaves exactly as before.

**The contract can only switch off what it actually describes.** An enrichment
missing from the document leaves its env value untouched. Two capabilities are
deliberately NOT mapped:

* Vehicle attributes. The contract's ``vehicle-attribute`` entry is the PULC
  Triton head, which is off; the car attributes that reach the UI come from the
  ``alpr-worker``, which is not a Triton model and has no contract entry. The
  same ``attribute_labels`` set gates BOTH, so honouring the PULC flag here
  would silently switch off licence plates.
* ReID. There is no enrichment entry for it, only ``REID_ENABLED``.

Both stay on the environment until the contract grows entries for them.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Enrichment -> which capability it switches. Matched on `model` first and
# `family` second, because a deployment may rename the model but keep the family.
_EMBEDDING = ({"vehicle-embedding"}, {"pp-shitu"})
_PERSON_ATTRIBUTES = ({"person-attribute"}, {"pulc-person"})


@dataclass(frozen=True)
class Capabilities:
    """What the router is allowed to run, as a whole. Replaced, never mutated."""

    embedding_labels: frozenset[str]
    attribute_labels: frozenset[str]
    source: str = "env"


def _matches(enrichment: dict[str, Any], names: set[str], families: set[str]) -> bool:
    return (str(enrichment.get("model") or "") in names
            or str(enrichment.get("family") or "") in families)


def capabilities_from_contract(
    document: Any,
    *,
    env_embedding: frozenset[str],
    env_attribute: frozenset[str],
) -> Capabilities:
    """Contract document -> capabilities, falling back to the env values.

    An enrichment with no ``enabled`` key is ON: that is the contract default,
    and reading it the other way round would switch off a running pipeline the
    first time the watcher fires.
    """
    if not isinstance(document, dict):
        return Capabilities(env_embedding, env_attribute)
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict):
        return Capabilities(env_embedding, env_attribute)
    enrichments = pipeline.get("enrichments")
    if not isinstance(enrichments, list):
        return Capabilities(env_embedding, env_attribute)

    embedding = set(env_embedding)
    attribute = set(env_attribute)
    for enrichment in enrichments:
        if not isinstance(enrichment, dict):
            continue
        enabled = bool(enrichment.get("enabled", True))
        labels = {str(label) for label in (enrichment.get("labels") or [])}
        if _matches(enrichment, *_EMBEDDING):
            # The contract's labels are the authority on WHICH labels get
            # embedded, not just whether embedding runs at all.
            embedding = labels if enabled else set()
        elif _matches(enrichment, *_PERSON_ATTRIBUTES) and not enabled:
            attribute -= labels or {"person"}
    return Capabilities(frozenset(embedding), frozenset(attribute), source="contract")


class ContractWatcher(threading.Thread):
    """Re-reads the contract when its mtime changes and hands over new capabilities.

    Local ``stat()`` every ``interval`` seconds, no network: the file is written
    by platform-api and already mounted for ``video-engine``. A malformed or
    unreadable document is logged and IGNORED — the router keeps the last good
    capabilities rather than degrading to "nothing enabled".
    """

    def __init__(
        self,
        path: str | Path,
        on_change: Callable[[Capabilities], None],
        *,
        env_embedding: frozenset[str],
        env_attribute: frozenset[str],
        interval: float = 2.0,
    ) -> None:
        super().__init__(name="pipeline-contract-watcher", daemon=True)
        self.path = Path(path)
        self.on_change = on_change
        self.interval = interval
        self._env_embedding = env_embedding
        self._env_attribute = env_attribute
        self._stop = threading.Event()
        self._mtime: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def read_once(self) -> Capabilities | None:
        """Current capabilities, or None if the document cannot be used."""
        try:
            import yaml

            document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - a bad save must not kill the router
            logger.warning("pipeline contract unreadable, keeping previous: %s", error)
            return None
        return capabilities_from_contract(
            document,
            env_embedding=self._env_embedding,
            env_attribute=self._env_attribute,
        )

    def check_once(self) -> str:
        """Returns what happened: 'unchanged' | 'applied' | 'invalid'."""
        mtime = self._stat()
        if mtime == self._mtime:
            return "unchanged"
        self._mtime = mtime
        capabilities = self.read_once()
        if capabilities is None:
            return "invalid"
        logger.info(
            "pipeline contract applied: embedding=%s attributes=%s",
            ",".join(sorted(capabilities.embedding_labels)) or "none",
            ",".join(sorted(capabilities.attribute_labels)) or "none",
        )
        self.on_change(capabilities)
        return "applied"

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            self.check_once()
