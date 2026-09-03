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

## ⚠️ Hoy corren dos copias

El despliegue vivo del lab arrancó desde `/opt/observabilidad`, con la misma
estructura. **Este directorio es la fuente de verdad, pero el que corre es el
de `/opt`**, así que van a divergir si alguien edita solo uno.

Para pasar el despliegue a leer de aquí:

```bash
cd /home/agent/deepfrigate/observabilidad
cp /opt/observabilidad/grafana/gf_pw grafana/gf_pw
cp .env.example .env      # y poner GRAFANA_RO_PASSWORD
docker compose -f /opt/observabilidad/docker-compose.yml down
docker compose up -d
```

Los volúmenes `prom_data` y `graf_data` **cambian de nombre de proyecto** al
mover el compose (`observabilidad_*` → `deepfrigate_*` o el que corresponda),
así que el histórico del TSDB no viaja solo. Si importa conservarlo, declarar
los volúmenes como `external` apuntando a los actuales antes de levantar.
