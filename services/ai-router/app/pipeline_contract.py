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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Enrichment -> which capability it switches. Matched on `model` first and
# `family` second, because a deployment may rename the model but keep the family.
_EMBEDDING = ({"vehicle-embedding"}, {"pp-shitu"})
_PERSON_ATTRIBUTES = ({"person-attribute"}, {"pulc-person"})


@dataclass(frozen=True)
class Capabilities:
    """What the router is allowed to run. Replaced, never mutated.

    ``plate_labels`` es None cuando nadie lo ha dicho: entonces la placa va
    donde siempre, pegada a ``attribute_labels``. Separarlas es lo que permite
    pedir "de esta cámara solo la placa" sin apagar los atributos de las demás.

    ``per_camera`` es el mapa de excepciones. Vacío —el caso normal— significa
    que todas las cámaras comparten estas mismas capacidades.
    """

    embedding_labels: frozenset[str]
    attribute_labels: frozenset[str]
    plate_labels: frozenset[str] | None = None
    # Qué etiquetas pasan de verdad por el clasificador de atributos. Es otra
    # cosa que `attribute_labels`, que además decide si el objeto LLEGA al
    # router: una cámara "solo placa" tiene que dejar entrar al coche sin
    # correrle los atributos.
    attribute_infer_labels: frozenset[str] | None = None
    per_camera: Mapping[str, "Capabilities"] = field(default_factory=dict)
    source: str = "env"

    @property
    def infers_attributes(self) -> frozenset[str]:
        """Etiquetas a las que se les corre el clasificador de atributos."""
        return (self.attribute_labels if self.attribute_infer_labels is None
                else self.attribute_infer_labels)

    @property
    def plates(self) -> frozenset[str]:
        """Etiquetas con lectura de placa. Sin decir nada, las de atributos."""
        return self.attribute_labels if self.plate_labels is None else self.plate_labels

    def for_camera(self, camera_id: str) -> "Capabilities":
        """Las capacidades que aplican a UNA cámara.

        Sin excepción para ella, las globales. La excepción se devuelve sin
        `per_camera` para que nadie la vuelva a resolver por accidente.
        """
        propia = self.per_camera.get(str(camera_id))
        if propia is None:
            return self
        return Capabilities(
            embedding_labels=propia.embedding_labels,
            attribute_labels=propia.attribute_labels,
            plate_labels=propia.plate_labels,
            attribute_infer_labels=propia.attribute_infer_labels,
            source=propia.source,
        )


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


# Nombres con los que un documento de excepciones puede pedir cada capacidad, y
# las etiquetas que cada una admite cuando el contrato no las declara.
_ALIAS: dict[str, tuple[str, frozenset[str]]] = {
    "license-plate": ("plate", frozenset({"car"})),
    "alpr": ("plate", frozenset({"car"})),
    "openalpr": ("plate", frozenset({"car"})),
    "person-attribute": ("attribute", frozenset({"person"})),
    "pulc-person": ("attribute", frozenset({"person"})),
    "vehicle-attribute": ("attribute", frozenset({"car"})),
    "pulc-vehicle": ("attribute", frozenset({"car"})),
    "vehicle-embedding": ("embedding", frozenset({"car", "person"})),
    "pp-shitu": ("embedding", frozenset({"car", "person"})),
}


def camera_capabilities(pedidos: Any, *, base: Capabilities) -> Capabilities:
    """Lista de enriquecedores pedidos -> capacidades de UNA cámara.

    Lo que no se nombra queda APAGADO: el documento de excepciones dice lo que
    esa cámara debe correr, no lo que le falta a lo global. Es lo que permite
    "solo la placa" sin tener que enumerar todo lo demás.
    """
    embedding: set[str] = set()
    attribute: set[str] = set()
    plate: set[str] = set()
    for pedido in pedidos if isinstance(pedidos, list) else []:
        capacidad = _ALIAS.get(str(pedido).strip().lower())
        if capacidad is None:
            logger.warning("Enriquecedor desconocido en las excepciones: %s", pedido)
            continue
        destino, etiquetas = capacidad
        if destino == "embedding":
            embedding |= set(etiquetas) & set(base.embedding_labels or etiquetas)
        elif destino == "attribute":
            attribute |= set(etiquetas)
        else:
            plate |= set(etiquetas)
    return Capabilities(
        embedding_labels=frozenset(embedding),
        # La placa viaja aparte: si se pide sin atributos, el objeto tiene que
        # seguir llegando al router, y eso lo decide `attribute_labels`.
        attribute_labels=frozenset(attribute | plate),
        plate_labels=frozenset(plate),
        attribute_infer_labels=frozenset(attribute),
        source="overrides",
    )


def overrides_from_document(document: Any, *, base: Capabilities) -> Capabilities:
    """Documento de excepciones -> `base` con su mapa por cámara.

        cameras:
          sandbox_abc:
            enrichments: [license-plate]

    Un documento vacío o ilegible devuelve `base` intacto: la regla de esta
    casa es que un archivo roto no apaga nada.
    """
    if not isinstance(document, dict):
        return base
    camaras = document.get("cameras")
    if not isinstance(camaras, dict) or not camaras:
        return base
    mapa: dict[str, Capabilities] = {}
    for camera_id, ajustes in camaras.items():
        if not isinstance(ajustes, dict):
            continue
        mapa[str(camera_id)] = camera_capabilities(
            ajustes.get("enrichments"), base=base)
    if not mapa:
        return base
    return Capabilities(
        embedding_labels=base.embedding_labels,
        attribute_labels=base.attribute_labels,
        plate_labels=base.plate_labels,
        attribute_infer_labels=base.attribute_infer_labels,
        per_camera=mapa,
        source=base.source,
    )


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
        # OJO: no se puede llamar `_stop`. `threading.Thread` ya tiene un
        # método con ese nombre y pisarlo con un Event rompe `join()` e
        # `is_alive()` con «'Event' object is not callable».
        self._parar = threading.Event()
        self._mtime: float | None = None

    def stop(self) -> None:
        self._parar.set()

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
        while not self._parar.wait(self.interval):
            self.check_once()


class OverridesWatcher(threading.Thread):
    """Vigila el documento de EXCEPCIONES por cámara.

    Es otro archivo, no el contrato: el contrato lo escribe platform-api y
    describe el pipeline entero, mientras que las excepciones las escribe la
    consola para una cámara concreta —un trabajo del sandbox, una cámara a la
    que hoy solo se le quiere leer la placa—. Separarlos evita que la consola
    tenga que reescribir el contrato de producción para probar algo.

    Igual que el contrato: `stat()` cada `interval` segundos, y un archivo
    ilegible se registra y se IGNORA. Que el archivo desaparezca sí significa
    algo —ya no hay excepciones— y se aplica.
    """

    def __init__(
        self,
        path: str | Path,
        on_change: Callable[[Any], None],
        *,
        interval: float = 2.0,
    ) -> None:
        super().__init__(name="enrichment-overrides-watcher", daemon=True)
        self.path = Path(path)
        self.on_change = on_change
        self.interval = interval
        # OJO: no se puede llamar `_stop`. `threading.Thread` ya tiene un
        # método con ese nombre y pisarlo con un Event rompe `join()` e
        # `is_alive()` con «'Event' object is not callable».
        self._parar = threading.Event()
        self._mtime: float | None = None

    def stop(self) -> None:
        self._parar.set()

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return -1.0

    def read_once(self) -> Any:
        if not self.path.is_file():
            return {}
        try:
            import yaml

            return yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as error:  # noqa: BLE001 - un guardado a medias no tumba el router
            logger.warning("excepciones ilegibles, conservo las anteriores: %s", error)
            return None

    def check_once(self) -> str:
        mtime = self._stat()
        if mtime == self._mtime:
            return "unchanged"
        self._mtime = mtime
        documento = self.read_once()
        if documento is None:
            return "invalid"
        camaras = documento.get("cameras") if isinstance(documento, dict) else None
        logger.info("excepciones por cámara aplicadas: %s",
                    ",".join(sorted(camaras or {})) or "ninguna")
        self.on_change(documento)
        return "applied"

    def run(self) -> None:
        while not self._parar.wait(self.interval):
            self.check_once()
