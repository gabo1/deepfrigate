"""Excepciones por cámara: "de ésta, solo la placa".

Hasta ahora el router decidía por ETIQUETA y para todas las cámaras a la vez:
`ATTRIBUTE_LABELS=person,car` significaba atributos y placa en todas. Probar un
modelo sobre un video, o pedir solo la lectura de placa en una cámara, obligaba
a apagarlo en el resto.

Dos cosas hacían falta y estas pruebas las fijan: que la placa sea una
capacidad propia —hoy viajaba pegada a los atributos— y que las capacidades se
resuelvan por cámara.
"""
from app.pipeline_contract import (
    Capabilities,
    camera_capabilities,
    overrides_from_document,
)

BASE = Capabilities(frozenset({"person", "car"}), frozenset({"person", "car"}))


def test_solo_placa_deja_pasar_el_coche_pero_no_lo_clasifica():
    """El objeto tiene que LLEGAR al router para que se le lea la placa, y eso
    lo decide `attribute_labels`; lo que no debe correr es el clasificador."""
    caps = camera_capabilities(["license-plate"], base=BASE)
    assert caps.attribute_labels == {"car"}, "el coche entra"
    assert caps.infers_attributes == frozenset(), "sin clasificador de atributos"
    assert caps.plates == {"car"}
    assert caps.embedding_labels == frozenset(), "sin ReID"


def test_lo_que_no_se_nombra_queda_apagado():
    """El documento dice lo que esa cámara DEBE correr, no lo que le falta."""
    caps = camera_capabilities(["person-attribute"], base=BASE)
    assert caps.infers_attributes == {"person"}
    assert caps.plates == frozenset()


def test_las_demas_camaras_no_se_enteran():
    caps = overrides_from_document(
        {"cameras": {"sandbox_1": {"enrichments": ["license-plate"]}}}, base=BASE)
    assert caps.for_camera("user").attribute_labels == BASE.attribute_labels
    assert caps.for_camera("user").plates == BASE.attribute_labels
    assert caps.for_camera("sandbox_1").plates == {"car"}


def test_sin_excepciones_todo_sigue_igual():
    """La ruta normal no cambia de comportamiento: sin documento, la cámara
    recibe exactamente el objeto global."""
    assert overrides_from_document({}, base=BASE) is BASE
    assert overrides_from_document({"cameras": {}}, base=BASE) is BASE
    assert overrides_from_document("basura", base=BASE) is BASE
    assert BASE.for_camera("user") is BASE


def test_un_nombre_desconocido_no_apaga_lo_demas():
    caps = camera_capabilities(["license-plate", "modelo-que-no-existe"], base=BASE)
    assert caps.plates == {"car"}


def test_la_placa_sigue_pegada_a_los_atributos_si_nadie_dice_lo_contrario():
    """Compatibilidad: `plate_labels=None` es el comportamiento de siempre."""
    assert BASE.plates == BASE.attribute_labels
    assert BASE.infers_attributes == BASE.attribute_labels
