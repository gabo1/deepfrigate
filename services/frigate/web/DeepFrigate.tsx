import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Badge } from "@/components/ui/badge";
import Heading from "@/components/ui/heading";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import useSWR from "swr";

type DeepFrigateEvent = {
  id: string;
  event_type: string;
  object_id: string;
  camera_id: string;
  track_id: number;
  timestamp: number;
  severity: "info" | "warning" | "critical";
  data: Record<string, unknown>;
};

type EventList = {
  items: DeepFrigateEvent[];
};

type ObjectDetails = {
  object_id: string;
  frigate_event_id?: string;
  camera_id: string;
  track_id: number;
  label: string;
  first_seen: number;
  last_seen: number;
  zones: string[];
  events: DeepFrigateEvent[];
  embeddings: Array<{
    vector_id: string;
    model?: string;
    width?: number;
    height?: number;
    frame_timestamp?: number;
  }>;
};

type ObjectSummary = {
  object_id: string;
  camera_id: string;
  track_id: number;
  label: string;
  latest: number;
  zones: string[];
  events: DeepFrigateEvent[];
};

type SimilarObjects = {
  metric: "Cosine";
  threshold_validated: boolean;
  items: Array<{
    object_id: string;
    vector_id: string;
    score: number;
    camera_id?: string;
    frame_timestamp?: number;
    width?: number;
    height?: number;
    has_events: boolean;
  }>;
};

function eventLabel(value: string) {
  return value.replaceAll("_", " ");
}

function timestamp(value: number) {
  return new Date(value * 1000).toLocaleString();
}

export default function DeepFrigate() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [camera, setCamera] = useState("all");
  const [selected, setSelected] = useState<string>();
  const [similarSource, setSimilarSource] = useState<string>();
  const { data, isLoading } = useSWR<EventList>(
    "deepfrigate/v1/events?limit=200",
    { refreshInterval: 5000 },
  );
  const { data: details, isLoading: detailsLoading } = useSWR<ObjectDetails>(
    selected
      ? `deepfrigate/v1/objects/${encodeURIComponent(selected)}`
      : null,
  );
  const { data: similar, isLoading: similarLoading } = useSWR<SimilarObjects>(
    selected && similarSource === selected
      ? `deepfrigate/v1/objects/${encodeURIComponent(selected)}/similar?limit=10`
      : null,
  );

  const objects = useMemo(() => {
    const grouped = new Map<string, ObjectSummary>();
    for (const event of data?.items ?? []) {
      const current = grouped.get(event.object_id);
      const zone =
        typeof event.data.zone === "string" ? event.data.zone : undefined;
      if (current) {
        current.events.push(event);
        current.latest = Math.max(current.latest, event.timestamp);
        if (zone && !current.zones.includes(zone)) current.zones.push(zone);
        continue;
      }
      grouped.set(event.object_id, {
        object_id: event.object_id,
        camera_id: event.camera_id,
        track_id: event.track_id,
        label:
          typeof event.data.label === "string" ? event.data.label : "object",
        latest: event.timestamp,
        zones: zone ? [zone] : [],
        events: [event],
      });
    }
    return [...grouped.values()]
      .filter((object) => camera === "all" || object.camera_id === camera)
      .sort((left, right) => right.latest - left.latest);
  }, [camera, data?.items]);

  const cameras = useMemo(
    () =>
      [
        "all",
        ...new Set((data?.items ?? []).map((event) => event.camera_id)),
      ].sort((left, right) =>
        left === "all" ? -1 : left.localeCompare(right),
      ),
    [data?.items],
  );

  useEffect(() => {
    const objectId = searchParams.get("object");
    if (objectId) setSelected(objectId);
  }, [searchParams]);

  return (
    <div className="flex size-full flex-col overflow-hidden p-4 md:p-6">
      <div className="mb-4">
        <Heading as="h1">DeepFrigate</Heading>
        <p className="text-sm text-muted-foreground">
          Objetos, zonas y embeddings producidos por DeepStream.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        {cameras.map((value) => (
          <button
            className={
              value === camera
                ? "rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground"
                : "rounded-md bg-secondary px-3 py-1.5 text-sm"
            }
            key={value}
            onClick={() => setCamera(value)}
          >
            {value === "all" ? "Todas" : value}
          </button>
        ))}
      </div>

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
        <div className="overflow-y-auto">
          {isLoading && <ActivityIndicator className="m-8" />}
          <div className="grid gap-3 xl:grid-cols-2">
            {objects.map((object) => (
              <button
                className="rounded-lg border bg-card p-4 text-left transition-colors hover:bg-accent"
                key={object.object_id}
                onClick={() => {
                  setSelected(object.object_id);
                  setSimilarSource(undefined);
                }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-medium">{object.label}</div>
                    <div className="text-xs text-muted-foreground">
                      {object.camera_id} · track {object.track_id}
                    </div>
                  </div>
                  <Badge variant="secondary">
                    {eventLabel(object.events[0].event_type)}
                  </Badge>
                </div>
                <div className="mt-3 text-sm">{timestamp(object.latest)}</div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {object.zones.map((zone) => (
                    <Badge key={zone}>{zone}</Badge>
                  ))}
                </div>
              </button>
            ))}
          </div>
        </div>

        <aside className="overflow-y-auto rounded-lg border bg-card p-4">
          {!selected && (
            <p className="text-sm text-muted-foreground">
              Selecciona un objeto para ver su historial.
            </p>
          )}
          {detailsLoading && <ActivityIndicator className="m-8" />}
          {details && (
            <div className="space-y-5">
              <div>
                <Heading as="h2">{details.label}</Heading>
                <p className="text-sm text-muted-foreground">
                  {details.object_id} · {details.camera_id}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-muted-foreground">Primera detección</div>
                  <div>{timestamp(details.first_seen)}</div>
                </div>
                <div>
                  <div className="text-muted-foreground">Última actividad</div>
                  <div>{timestamp(details.last_seen)}</div>
                </div>
              </div>
              <section>
                <h3 className="mb-2 font-medium">Zonas</h3>
                <div className="flex flex-wrap gap-1">
                  {details.zones.length ? (
                    details.zones.map((zone) => (
                      <Badge key={zone}>{zone}</Badge>
                    ))
                  ) : (
                    <span className="text-sm text-muted-foreground">
                      Sin zonas
                    </span>
                  )}
                </div>
              </section>
              <section>
                <h3 className="mb-2 font-medium">Embeddings</h3>
                {details.embeddings.length ? (
                  details.embeddings.map((embedding) => (
                    <div
                      className="mb-2 rounded-md bg-secondary p-3 text-sm"
                      key={embedding.vector_id}
                    >
                      <div>{embedding.model ?? "vehicle-embedding"}</div>
                      <div className="break-all text-xs text-muted-foreground">
                        {embedding.vector_id}
                      </div>
                      {embedding.width && embedding.height && (
                        <div className="text-xs text-muted-foreground">
                          crop {embedding.width}×{embedding.height}
                        </div>
                      )}
                    </div>
                  ))
                ) : (
                  <span className="text-sm text-muted-foreground">
                    Sin embedding
                  </span>
                )}
              </section>
              <section>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="font-medium">Similitud visual</h3>
                  <div className="flex flex-wrap gap-2">
                    <button
                      className="rounded-md bg-secondary px-3 py-1.5 text-sm disabled:opacity-50"
                      disabled={
                        !details.embeddings.length || !details.frigate_event_id
                      }
                      onClick={() =>
                        navigate(
                          `/explore?search_type=similarity&event_id=${encodeURIComponent(details.frigate_event_id ?? "")}&deep_search=1`,
                        )
                      }
                    >
                      Buscar en Explore
                    </button>
                    <button
                      className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
                      disabled={!details.embeddings.length || similarLoading}
                      onClick={() => setSimilarSource(details.object_id)}
                    >
                      {similarLoading ? "Buscando…" : "Buscar aquí"}
                    </button>
                  </div>
                </div>
                {similar && !similar.threshold_validated && (
                  <p className="mb-3 text-xs text-amber-500">
                    Puntajes coseno sin umbral de identidad calibrado.
                  </p>
                )}
                <div className="space-y-2">
                  {similar?.items.map((candidate) => (
                    <button
                      className="flex w-full items-center justify-between gap-3 rounded-md bg-secondary p-3 text-left disabled:cursor-default"
                      disabled={!candidate.has_events}
                      key={candidate.vector_id}
                      onClick={() => {
                        if (!candidate.has_events) return;
                        setSelected(candidate.object_id);
                        setSimilarSource(undefined);
                      }}
                    >
                      <div>
                        <div className="text-sm">{candidate.object_id}</div>
                        <div className="text-xs text-muted-foreground">
                          {candidate.camera_id ?? "cámara desconocida"}
                          {candidate.width && candidate.height
                            ? ` · ${candidate.width}×${candidate.height}`
                            : ""}
                        </div>
                      </div>
                      <Badge variant="secondary">
                        {(candidate.score * 100).toFixed(1)}%
                      </Badge>
                    </button>
                  ))}
                </div>
              </section>
              <section>
                <h3 className="mb-2 font-medium">Historial</h3>
                <div className="space-y-2">
                  {details.events.map((event) => (
                    <div
                      className="flex items-center justify-between gap-3 rounded-md bg-secondary p-2 text-sm"
                      key={event.id}
                    >
                      <span>{eventLabel(event.event_type)}</span>
                      <span className="text-xs text-muted-foreground">
                        {timestamp(event.timestamp)}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
