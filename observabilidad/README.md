# Observabilidad — Prometheus + Grafana

Fuente de verdad de los dashboards, los datasources y el scrape config.
Detalle de qué mide cada cosa y por qué: [`../docs/ANALITICAS-FUENTES.md`](../docs/ANALITICAS-FUENTES.md).

## Qué hay

| Ruta | Qué |
|---|---|
| `prometheus/prometheus.yml` | 3 jobs. Solo `analitica_deepfrigate` (`:9110`) está vivo; `analitica_frigate` y `analitica_savant` quedan `down` a propósito, para no perder su histórico en el TSDB |
| `grafana/dashboards/analitica-deepfrigate.json` | Comportamiento: aforo, permanencia, cruces, overcrowding, Fila C SQL y heatmap |
| `grafana/dashboards/pulc-atributos.json` | Atributos PULC: ropa, colores, repetibilidad del modelo |
| `grafana/dashboards/analitica.json` | Legado. Archivo del histórico Savant del 26–31 ago. No tocar |
| `grafana/provisioning/datasources/` | Prometheus, Postgres del smoke, y `platform-api` usado **solo como proxy** |

El label que separa las series es **`motor`** (`deepfrigate` / `savant` /
`frigate`), no `fuente`. Los paneles del dashboard legado consultan nombres
desnudos, así que sin ese label se mezclarían tres motores en el mismo gráfico.

## Secretos: no se versionan

Dos, y ninguno está en el repo:

- **`grafana/gf_pw`** — contraseña de admin de Grafana, montada como fichero
  (`GF_SECURITY_ADMIN_PASSWORD__FILE`). Créala a mano.
- **`GRAFANA_RO_PASSWORD`** — del rol `grafana_ro` (solo `SELECT`). Va en
  `observabilidad/.env`; Grafana expande `${VAR}` en los ficheros de
  provisioning. Ver `.env.example`.

## Arrancar

```bash
cd observabilidad
cp .env.example .env && $EDITOR .env
printf '%s' 'LA_CONTRASEÑA_DE_ADMIN' > grafana/gf_pw
docker compose up -d
```

Grafana recoge los cambios de `grafana/dashboards/` solo, en ~10 s. Prometheus
**no**: no lleva `--web.enable-lifecycle`, así que un cambio en
`prometheus.yml` necesita `docker restart prometheus`. El TSDB está en un
volumen, no se pierde histórico.

## Despliegue: este directorio, desde el 8 sep 2026

Grafana y Prometheus corren desde aquí (`docker compose up -d` en
`observabilidad/`). El compose lleva `name: observabilidad`, el mismo nombre
de proyecto que tenía el despliegue original de `/opt/observabilidad`, así que
los volúmenes `observabilidad_prom_data` (TSDB con el histórico Savant) y
`observabilidad_graf_data` se reutilizaron sin migrar nada. La copia vieja
quedó parada en `/opt/observabilidad.migrated-20260908` y se puede borrar.

Cambios de dashboard: editar el JSON aquí (o con `build_*.py`), commit;
Grafana lo recoge solo. Prometheus: `docker restart prometheus` tras tocar
`prometheus.yml`. Secretos locales (no versionados): `.env` con
`GRAFANA_RO_PASSWORD` y `grafana/gf_pw` (propietario uid 472, modo 400).
