# Glosario: los detectores de evento que se confunden

Documento para **no volver a preguntarlo**. Tres detectores suenan igual —«está
un rato ahí», «no se mueve», «hay muchos»— y miden cosas distintas. Estos son
los nombres que usan hoy la consola (menú *Pipeline*: canvas y matriz de
analíticas) y el `event_type` que escribe el pipeline.

Fecha: **15 sep 2026**.

| Nombre en la consola | Qué mide | Necesita zona | `event_type` |
|---|---|---|---|
| **Tiempo en zona** | Cuánto lleva el objeto **dentro** de una zona. No mira si se mueve: un coche que cruza despacio acumula tiempo igual. | sí | `dwell_time` |
| **Aforo (a la vez)** | Cuántos objetos hay **simultáneamente** dentro de la zona. No le importa cuánto lleven. | sí | `overcrowding` |
| **Detenido (sin zona)** | El objeto **dejó de moverse**, en cualquier parte del cuadro. | no | `object_stationary` |

Nombres anteriores, por si aparecen en capturas viejas: «Merodeo en zona» →
Tiempo en zona, «Aforo por zona» → Aforo (a la vez), «Objeto detenido» →
Detenido (sin zona).

## La diferencia en una línea

*Tiempo en zona* cuenta reloj dentro de un polígono aunque el objeto camine.
*Detenido* sólo mira si se movió, y no necesita polígono ninguno.

## Detalles que muerden

**Merodeo no es un detector.** Es el nombre que una **regla** le pone a
`dwell_time` cuando pasa de su umbral:

```yaml
- name: merodeo_calle
  when: {event_type: [dwell_time], label: [person], zone: [calle], min_dwell_seconds: 30}
  then: {sub_label: "Merodeo"}
```

Sin regla encendida, el evento se escribe en `deepfrigate.events` y **no genera
aviso**. La matriz lo marca `◐` en esa cámara.

**`dwell_time` no es una fila por segundo.** El adapter lo emite cada segundo,
pero el `event-engine` calcula la identidad desde la hora de ENTRADA, así que
todos los latidos de una visita colapsan en **una sola fila**, que se va
actualizando. El total que manda es `object_exited_zone.seconds`.

**`object_stationary` es una transición, no un estado continuo.** Trae
`stationary: true|false`. El tracker lo marca cuando el objeto lleva más de
`max(int(detect_fps * 10), 1)` frames quieto —hoy **50**, unos 10 s a 5 fps— y
emite otro con `false` en cuanto arranca.

**Aforo compara contra un umbral por zona.** El evento lleva `count`,
`threshold` y `zone`. Una regla sin `camera` ni `zone` captura el aforo de
TODAS las cámaras: así `aforo_excedido` disparaba junto con
`estacionamiento_lleno` para el mismo suceso, hasta que se le acotó la lista de
cámaras.

## Dónde se enciende cada uno

No se encienden desde la consola: hacen falta las dos mitades.

1. **Geometría** — la zona (o la línea, o la dirección) se dibuja en la
   configuración de Frigate; el `detection-adapter` la recarga por MQTT.
   `Detenido` es la excepción: no necesita geometría.
2. **Regla** — en `config/rules/rules.yaml`, que el `event-engine` relee cada
   2 s.

La matriz de analíticas (SAiMON → *Pipeline* → *Analíticas*) lo resume por
cámara: `●` corre y avisa · `◐` se calcula y ninguna regla encendida lo consume
· `·` la cámara no tiene esa geometría dibujada.

Ver también: `docs/OPERACION.md` §6d (reglas) y §6c-bis (enriquecedores por
cámara).
