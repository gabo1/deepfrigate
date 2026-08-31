import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Badge } from "@/components/ui/badge";
import Heading from "@/components/ui/heading";
import { useNavigate, useSearchParams } from "react-router-dom";
import useSWR from "swr";

type ObjectDetails = {
  object_id: string;
  camera_id: string;
  label: string;
  first_seen: number;
  last_seen: number;
};

type SimilarObject = {
  object_id: string;
  vector_id: string;
  score: number;
  camera_id?: string;
  frame_timestamp?: number;
  width?: number;
  height?: number;
  model?: string;
  has_events: boolean;
};

type SimilarResponse = {
  metric: "Cosine";
  threshold_validated: boolean;
  source: {
    object_id: string;
    camera_id?: string;
    frame_timestamp?: number;
    model?: string;
  };
  items: SimilarObject[];
};

function timestamp(value?: number) {
  return value ? new Date(value * 1000).toLocaleString() : "Fecha desconocida";
}

export default function DeepFrigateVisualSearch() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const objectId = searchParams.get("deep_object_id") ?? "";
  const { data: source, error: sourceError } = useSWR<ObjectDetails>(
    objectId
      ? `deepfrigate/v1/objects/${encodeURIComponent(objectId)}`
      : null,
  );
  const {
    data: results,
    error: resultsError,
    isLoading,
  } = useSWR<SimilarResponse>(
    objectId
      ? `deepfrigate/v1/objects/${encodeURIComponent(objectId)}/similar?limit=24`
      : null,
  );

  const selectObject = (candidateId: string) => {
    setSearchParams({ deep_object_id: candidateId });
  };

  return (
    <div className="flex size-full flex-col overflow-hidden p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <Heading as="h1">Búsqueda visual DeepFrigate</Heading>
          <p className="text-sm text-muted-foreground">
            Resultados PP-ShiTuV2 recuperados por similitud coseno en Qdrant.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className="rounded-md bg-secondary px-3 py-2 text-sm"
            onClick={() =>
              navigate(`/deepfrigate?object=${encodeURIComponent(objectId)}`)
            }
          >
            Ver objeto
          </button>
          <button
            className="rounded-md bg-secondary px-3 py-2 text-sm"
            onClick={() => navigate("/explore")}
          >
            Cerrar búsqueda visual
          </button>
        </div>
      </div>

      <section className="mb-4 rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-xs uppercase text-muted-foreground">
              Objeto de referencia
            </div>
            <div className="font-medium">{source?.label ?? objectId}</div>
            <div className="text-xs text-muted-foreground">
              {source?.camera_id ?? results?.source.camera_id ?? "cámara desconocida"}
              {source ? ` · ${timestamp(source.last_seen)}` : ""}
            </div>
          </div>
          <div className="flex gap-2">
            <Badge variant="secondary">PP-ShiTuV2</Badge>
            <Badge variant="secondary">{results?.metric ?? "Cosine"}</Badge>
          </div>
        </div>
      </section>

      {(sourceError || resultsError) && (
        <div className="rounded-md border border-destructive p-4 text-sm text-destructive">
          No se pudo ejecutar la búsqueda visual para este objeto.
        </div>
      )}
      {isLoading && <ActivityIndicator className="m-8" />}

      {results && !results.threshold_validated && (
        <p className="mb-3 text-xs text-amber-500">
          Los porcentajes expresan similitud visual; todavía no representan una
          identidad confirmada.
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {results?.items.map((candidate) => (
            <button
              className="rounded-lg border bg-card p-4 text-left transition-colors hover:bg-accent"
              key={candidate.vector_id}
              onClick={() => selectObject(candidate.object_id)}
            >
              <div className="mb-4 flex items-start justify-between gap-3">
                <div>
                  <div className="font-medium">
                    {candidate.object_id || "Objeto sin identificador"}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {candidate.camera_id ?? "cámara desconocida"}
                  </div>
                </div>
                <Badge>{(candidate.score * 100).toFixed(1)}%</Badge>
              </div>
              <div className="space-y-1 text-xs text-muted-foreground">
                <div>{timestamp(candidate.frame_timestamp)}</div>
                <div>
                  {candidate.width && candidate.height
                    ? `Crop ${candidate.width}×${candidate.height}`
                    : "Dimensiones desconocidas"}
                </div>
                <div>{candidate.model ?? "vehicle-embedding"}</div>
              </div>
            </button>
          ))}
        </div>
        {results && results.items.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            No se encontraron objetos visualmente similares.
          </div>
        )}
      </div>
    </div>
  );
}
