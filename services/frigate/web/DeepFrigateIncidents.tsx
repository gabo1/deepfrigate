import { baseUrl } from "@/api/baseUrl";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Button } from "@/components/ui/button";
import Heading from "@/components/ui/heading";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { FrigateConfig } from "@/types/frigateConfig";
import axios from "axios";
import { useCallback, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import useSWR from "swr";

/**
 * Incidentes: la bandeja de operación de DeepFrigate.
 *
 * Sustituye al Review de Frigate (apagado: sin decode en Frigate su
 * mantenedor no publica y su episodio por cámara nunca cierra con tráfico
 * continuo). Dos vistas sobre `deepfrigate.events` vía platform-api:
 *  - Alertas: un ítem por regla disparada (`rule_matched`), foto del
 *    instante cortada de la grabación, acuse por operador.
 *  - Actividad: episodios por cámara de N minutos con conteo por etiqueta,
 *    zonas, placas y clip.
 */

type Ack = { by: string; at: number; note?: string | null } | null;

type Incident = {
  id: string;
  camera_id: string;
  object_id: string;
  timestamp: number;
  iso: string;
  severity: "info" | "warning" | "critical";
  rule?: string | null;
  message?: string | null;
  label?: string | null;
  zone?: string | null;
  line?: string | null;
  direction?: string | null;
  count?: number | null;
  dwell_time?: number | null;
  frigate_event_id?: string | null;
  acked: Ack;
  frame_url: string;
};

type Episode = {
  id: string;
  camera_id: string;
  start: number;
  end: number;
  first_seen?: number | null;
  last_seen?: number | null;
  objects: number;
  labels: Record<string, number>;
  zones: string[];
  plates: string[];
  alerts: number;
  critical: number;
  frigate_event_id?: string | null;
  thumbnail_url?: string | null;
  clip_url: string;
};

type IncidentObject = {
  n: number;
  object_id: string;
  label?: string | null;
  bbox?: { x: number; y: number; width: number; height: number } | null;
  first_seen?: number | null;
  last_seen?: number | null;
  since_alert_s?: number | null;
  until_alert_s?: number | null;
  frigate_event_id?: string | null;
  sub_label?: string | null;
  attributes?: Record<string, { value: string; score: number }> | null;
  plate?: string | null;
  thumbnail_url?: string | null;
  explore_url?: string | null;
};

type IncidentDetail = Incident & {
  objects: IncidentObject[];
  scene_url: string;
  clip_url: string;
};

type Summary = {
  hours: number;
  by_severity: Record<string, { total: number; pending: number }>;
};

const API = "deepfrigate/v1";
const WINDOWS = [1, 5, 15, 60];

function timeAgo(epoch: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  if (seconds < 60) return `hace ${seconds} s`;
  if (seconds < 3600) return `hace ${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `hace ${Math.round(seconds / 3600)} h`;
  return `hace ${Math.round(seconds / 86400)} d`;
}

function clock(epoch: number) {
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function dayAndClock(epoch: number) {
  return new Date(epoch * 1000).toLocaleString([], {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function severityBadge(severity: string) {
  const tone =
    severity === "critical"
      ? "df-badge-crit"
      : severity === "warning"
        ? "df-badge-warn"
        : "df-badge-info";
  const text =
    severity === "critical"
      ? "crítica"
      : severity === "warning"
        ? "alerta"
        : "info";
  return <span className={`df-badge ${tone}`}>{text}</span>;
}

export default function DeepFrigateIncidents() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<"alerts" | "activity">("alerts");
  const [camera, setCamera] = useState("all");
  const [onlyPending, setOnlyPending] = useState(true);
  const [minutes, setMinutes] = useState(5);
  const [selected, setSelected] = useState<string | null>(null);
  const { data: detail } = useSWR<IncidentDetail>(
    selected ? `${API}/incidents/${encodeURIComponent(selected)}` : null,
  );

  const { data: config } = useSWR<FrigateConfig>("config", {
    revalidateOnFocus: false,
  });
  const cameras = useMemo(
    () => Object.keys(config?.cameras ?? {}).sort(),
    [config?.cameras],
  );

  const alertsKey = useMemo(() => {
    const params = new URLSearchParams({ limit: "200" });
    if (camera !== "all") params.set("camera_id", camera);
    if (onlyPending) params.set("acked", "false");
    return `${API}/incidents?${params.toString()}`;
  }, [camera, onlyPending]);
  const {
    data: alerts,
    isLoading: alertsLoading,
    mutate: refreshAlerts,
  } = useSWR<{ items: Incident[] }>(alertsKey, { refreshInterval: 10000 });
  const { data: summary, mutate: refreshSummary } = useSWR<Summary>(
    `${API}/incidents/summary?hours=24`,
    { refreshInterval: 30000 },
  );

  const activityKey = useMemo(() => {
    const params = new URLSearchParams({
      minutes: String(minutes),
      limit: "200",
    });
    if (camera !== "all") params.set("camera_id", camera);
    return `${API}/activity?${params.toString()}`;
  }, [camera, minutes]);
  const { data: activity, isLoading: activityLoading } = useSWR<{
    items: Episode[];
  }>(tab === "activity" ? activityKey : null, { refreshInterval: 30000 });

  const ack = useCallback(
    async (ids: string[]) => {
      await Promise.all(
        ids.map((id) =>
          axios.post(`${API}/incidents/${encodeURIComponent(id)}/ack`, {}),
        ),
      );
      await Promise.all([refreshAlerts(), refreshSummary()]);
    },
    [refreshAlerts, refreshSummary],
  );

  const unack = useCallback(
    async (id: string) => {
      await axios.delete(`${API}/incidents/${encodeURIComponent(id)}/ack`);
      await Promise.all([refreshAlerts(), refreshSummary()]);
    },
    [refreshAlerts, refreshSummary],
  );

  const pendingCritical = summary?.by_severity?.critical?.pending ?? 0;
  const pendingWarning = summary?.by_severity?.warning?.pending ?? 0;
  const visiblePending = (alerts?.items ?? []).filter((item) => !item.acked);

  return (
    <div className="flex size-full flex-col gap-3 overflow-hidden p-2 md:p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Heading as="h2" className="mb-0">
            Incidentes
          </Heading>
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant={tab === "alerts" ? "select" : "default"}
              onClick={() => setTab("alerts")}
            >
              Alertas
              {pendingCritical + pendingWarning > 0 && (
                <span className="df-badge df-badge-crit ml-2">
                  {pendingCritical + pendingWarning}
                </span>
              )}
            </Button>
            <Button
              size="sm"
              variant={tab === "activity" ? "select" : "default"}
              onClick={() => setTab("activity")}
            >
              Actividad
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={camera} onValueChange={setCamera}>
            <SelectTrigger className="w-44">
              <SelectValue placeholder="Cámara" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Todas las cámaras</SelectItem>
              {cameras.map((name) => (
                <SelectItem key={name} value={name}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {tab === "alerts" ? (
            <label className="flex items-center gap-2 text-sm text-primary-variant">
              <Switch checked={onlyPending} onCheckedChange={setOnlyPending} />
              Solo pendientes
            </label>
          ) : (
            <Select
              value={String(minutes)}
              onValueChange={(value) => setMinutes(Number(value))}
            >
              <SelectTrigger className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOWS.map((value) => (
                  <SelectItem key={value} value={String(value)}>
                    ventana {value} min
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {tab === "alerts" && visiblePending.length > 0 && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => ack(visiblePending.map((item) => item.id))}
            >
              Acusar {visiblePending.length} visibles
            </Button>
          )}
        </div>
      </div>

      <div className="df-label">
        últimas 24 h · críticas pendientes {pendingCritical} · alertas
        pendientes {pendingWarning}
      </div>

      <div className="flex-1 overflow-y-auto">
        {tab === "alerts" ? (
          alertsLoading && !alerts ? (
            <ActivityIndicator />
          ) : (alerts?.items.length ?? 0) === 0 ? (
            <div className="p-8 text-center text-secondary-foreground">
              Sin alertas {onlyPending ? "pendientes" : ""}. Las alertas
              nacen de las reglas en <code>config/rules/rules.yaml</code>.
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {alerts?.items.map((item) => (
                <AlertCard
                  key={item.id}
                  item={item}
                  onAck={() => ack([item.id])}
                  onUnack={() => unack(item.id)}
                  onOpen={() => setSelected(item.id)}
                />
              ))}
            </div>
          )
        ) : activityLoading && !activity ? (
          <ActivityIndicator />
        ) : (activity?.items.length ?? 0) === 0 ? (
          <div className="p-8 text-center text-secondary-foreground">
            Sin actividad en las últimas 6 h.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {activity?.items.map((episode) => (
              <EpisodeCard
                key={episode.id}
                episode={episode}
                onExplore={() =>
                  navigate(
                    `/explore?cameras=${encodeURIComponent(episode.camera_id)}&after=${Math.floor(episode.start)}&before=${Math.ceil(episode.end)}`,
                  )
                }
              />
            ))}
          </div>
        )}
      </div>
      <IncidentDialog
        detail={selected ? detail : undefined}
        open={selected !== null}
        onClose={() => setSelected(null)}
        onAck={(id) => ack([id])}
        onExplore={(url) => navigate(url)}
      />
    </div>
  );
}

function attributeSummary(attrs?: IncidentObject["attributes"]): string {
  if (!attrs) return "";
  const order = ["upper_color", "lower_color", "gender", "age", "sleeve", "glasses", "orientation", "color", "make_model", "body_type"];
  return order
    .filter((key) => attrs[key]?.value)
    .map((key) => `${attrs[key].value}`)
    .join(" · ");
}

function IncidentDialog({
  detail,
  open,
  onClose,
  onAck,
  onExplore,
}: {
  detail?: IncidentDetail;
  open: boolean;
  onClose: () => void;
  onAck: (id: string) => void;
  onExplore: (url: string) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-h-[92vh] w-[min(96vw,1100px)] max-w-none overflow-y-auto">
        {!detail ? (
          <ActivityIndicator />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle className="flex flex-wrap items-center gap-2">
                {severityBadge(detail.severity)}
                <span className="font-mono text-sm text-primary-variant">{detail.rule}</span>
                <span className="text-sm text-secondary-foreground">
                  {detail.camera_id} · {dayAndClock(detail.timestamp)} · {clock(detail.timestamp)}
                </span>
              </DialogTitle>
              <DialogDescription className="text-primary">{detail.message}</DialogDescription>
            </DialogHeader>
            <div className="relative overflow-hidden rounded border border-border bg-black">
              <img
                className="max-h-[52vh] w-full object-contain"
                src={`${baseUrl}api/deepfrigate${detail.scene_url}`}
                alt="escena"
              />
            </div>
            <div className="df-label">
              {detail.objects.length} objeto{detail.objects.length === 1 ? "" : "s"} en el instante de la alerta ·
              tiempos relativos a la alerta
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="df-label text-left">
                  <tr>
                    <th className="px-2 py-1">#</th>
                    <th className="px-2 py-1">foto</th>
                    <th className="px-2 py-1">objeto</th>
                    <th className="px-2 py-1">atributos</th>
                    <th className="px-2 py-1">aparece</th>
                    <th className="px-2 py-1">se va</th>
                    <th className="px-2 py-1"></th>
                  </tr>
                </thead>
                <tbody>
                  {detail.objects.map((obj) => (
                    <tr key={obj.object_id} className="border-t border-border align-middle">
                      <td className="px-2 py-1 font-mono text-primary-variant">{obj.n}</td>
                      <td className="px-2 py-1">
                        {obj.thumbnail_url ? (
                          <img
                            className="h-14 w-14 rounded object-cover"
                            loading="lazy"
                            src={`${baseUrl}api/deepfrigate${obj.thumbnail_url}`}
                            alt={obj.label ?? ""}
                          />
                        ) : (
                          <div className="flex h-14 w-14 items-center justify-center rounded bg-secondary text-[10px] text-secondary-foreground">
                            sin Event
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1 text-primary">
                        <div>{obj.label}</div>
                        {obj.plate && <div className="font-mono text-xs text-primary-variant">{obj.plate}</div>}
                        {!obj.plate && obj.sub_label && (
                          <div className="text-xs text-secondary-foreground">{obj.sub_label}</div>
                        )}
                        <div className="font-mono text-[10px] text-secondary-foreground">{obj.object_id}</div>
                      </td>
                      <td className="px-2 py-1 text-xs text-secondary-foreground">{attributeSummary(obj.attributes)}</td>
                      <td className="px-2 py-1 font-mono text-xs text-primary-variant">
                        {obj.since_alert_s == null ? "-" : `${obj.since_alert_s > 0 ? "+" : ""}${obj.since_alert_s} s`}
                      </td>
                      <td className="px-2 py-1 font-mono text-xs text-primary-variant">
                        {obj.until_alert_s == null ? "sigue" : `${obj.until_alert_s > 0 ? "+" : ""}${obj.until_alert_s} s`}
                      </td>
                      <td className="px-2 py-1 text-right">
                        {obj.explore_url && (
                          <Button size="sm" variant="ghost" onClick={() => onExplore(obj.explore_url!)}>
                            Explore
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <span className="text-xs text-secondary-foreground">
                {detail.acked
                  ? `acusó ${detail.acked.by} · ${dayAndClock(detail.acked.at)}`
                  : "pendiente de acuse"}
              </span>
              <div className="flex gap-1">
                <Button size="sm" variant="outline" asChild>
                  <a href={`${baseUrl}${detail.clip_url.replace(/^\//, "")}`} target="_blank" rel="noreferrer">
                    Clip ±30 s
                  </a>
                </Button>
                {detail.frigate_event_id && (
                  <Button size="sm" variant="ghost" onClick={() => onExplore(`/explore?event_id=${encodeURIComponent(detail.frigate_event_id!)}`)}>
                    Explore
                  </Button>
                )}
                {!detail.acked && (
                  <Button size="sm" variant="secondary" onClick={() => onAck(detail.id)}>
                    Acusar
                  </Button>
                )}
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function AlertCard({
  item,
  onAck,
  onUnack,
  onOpen,
}: {
  item: Incident;
  onAck: () => void;
  onUnack: () => void;
  onOpen: () => void;
}) {
  const edge =
    item.severity === "critical"
      ? "df-edge-crit"
      : item.severity === "warning"
        ? "df-edge-warn"
        : "df-edge-info";
  return (
    <div
      className={`flex flex-col overflow-hidden rounded border bg-card ${item.acked ? "border-border opacity-70" : edge}`}
    >
      <button
        type="button"
        className="relative block aspect-video w-full overflow-hidden bg-black"
        onClick={onOpen}
        title="Ver detalle"
      >
        <img
          className="size-full object-contain"
          loading="lazy"
          src={`${baseUrl}api/${item.frame_url.replace(/^\//, "").replace(/^v1\//, "deepfrigate/v1/")}`}
          alt={item.message ?? item.rule ?? "alerta"}
        />
        <div className="absolute left-2 top-2 flex gap-1">
          {severityBadge(item.severity)}
          {item.acked && <span className="df-badge df-badge-ok">acusada</span>}
        </div>
        <div className="absolute bottom-2 right-2 rounded bg-background/80 px-1.5 py-0.5 font-mono text-[11px] text-primary">
          {clock(item.timestamp)}
        </div>
      </button>
      <div className="flex flex-col gap-1 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-xs text-primary-variant">
            {item.rule}
          </span>
          <span className="text-xs text-secondary-foreground">
            {item.camera_id} · {timeAgo(item.timestamp)}
          </span>
        </div>
        <div className="text-sm text-primary">{item.message}</div>
        <div className="flex flex-wrap gap-1 text-[11px] text-secondary-foreground">
          {item.label && <span className="df-badge">{item.label}</span>}
          {item.zone && <span className="df-badge">zona {item.zone}</span>}
          {item.line && <span className="df-badge">línea {item.line}</span>}
          {item.direction && (
            <span className="df-badge">dir {item.direction}</span>
          )}
          {item.count != null && (
            <span className="df-badge">{item.count} objetos</span>
          )}
          {item.dwell_time != null && (
            <span className="df-badge">{Math.round(item.dwell_time)} s</span>
          )}
        </div>
        <div className="mt-1 flex items-center justify-between gap-2">
          <span className="text-[11px] text-secondary-foreground">
            {item.acked
              ? `acusó ${item.acked.by} · ${dayAndClock(item.acked.at)}`
              : dayAndClock(item.timestamp)}
          </span>
          {item.acked ? (
            <Button size="sm" variant="ghost" onClick={onUnack}>
              Reabrir
            </Button>
          ) : (
            <Button size="sm" variant="secondary" onClick={onAck}>
              Acusar
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function EpisodeCard({
  episode,
  onExplore,
}: {
  episode: Episode;
  onExplore: () => void;
}) {
  const labels = Object.entries(episode.labels);
  return (
    <div
      className={`flex flex-col overflow-hidden rounded border bg-card ${episode.critical > 0 ? "df-edge-crit" : episode.alerts > 0 ? "df-edge-warn" : "border-border"}`}
    >
      <div className="relative block aspect-video w-full overflow-hidden bg-black">
        {episode.thumbnail_url ? (
          <img
            className="size-full object-contain"
            loading="lazy"
            src={`${baseUrl}api/deepfrigate${episode.thumbnail_url}`}
            alt={episode.camera_id}
          />
        ) : (
          <div className="flex size-full items-center justify-center text-xs text-secondary-foreground">
            sin foto
          </div>
        )}
        <div className="absolute bottom-2 right-2 rounded bg-background/80 px-1.5 py-0.5 font-mono text-[11px] text-primary">
          {clock(episode.start)} – {clock(episode.end)}
        </div>
        {episode.alerts > 0 && (
          <div className="absolute left-2 top-2">
            <span
              className={`df-badge ${episode.critical > 0 ? "df-badge-crit" : "df-badge-warn"}`}
            >
              {episode.alerts} alerta{episode.alerts === 1 ? "" : "s"}
            </span>
          </div>
        )}
      </div>
      <div className="flex flex-col gap-1 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm text-primary">{episode.camera_id}</span>
          <span className="text-xs text-secondary-foreground">
            {episode.objects} objeto{episode.objects === 1 ? "" : "s"} ·{" "}
            {timeAgo(episode.end)}
          </span>
        </div>
        <div className="flex flex-wrap gap-1">
          {labels.map(([label, count]) => (
            <span key={label} className="df-badge">
              {label} {count}
            </span>
          ))}
        </div>
        {(episode.zones.length > 0 || episode.plates.length > 0) && (
          <div className="flex flex-wrap gap-1 text-[11px]">
            {episode.zones.map((zone) => (
              <span key={zone} className="df-badge df-badge-info">
                {zone}
              </span>
            ))}
            {episode.plates.map((plate) => (
              <span key={plate} className="df-badge font-mono">
                {plate}
              </span>
            ))}
          </div>
        )}
        <div className="mt-1 flex items-center justify-end gap-1">
          <Button size="sm" variant="ghost" onClick={onExplore}>
            Explore
          </Button>
          <Button size="sm" variant="outline" asChild>
            <a
              href={`${baseUrl}${episode.clip_url.replace(/^\//, "")}`}
              target="_blank"
              rel="noreferrer"
            >
              Clip
            </a>
          </Button>
        </div>
      </div>
    </div>
  );
}
