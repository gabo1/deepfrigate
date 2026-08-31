import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import Heading from "@/components/ui/heading";
import { useIsAdmin } from "@/hooks/use-is-admin";
import type { SettingsPageProps } from "@/views/settings/SingleSectionPage";
import axios from "axios";
import { useState } from "react";
import { toast } from "sonner";
import useSWR from "swr";

type Tensor = {
  name: string;
  data_type: string;
  dims: number[];
};

type ManagedModel = {
  name: string;
  versions: string[];
  state: string;
  reason?: string;
  required: boolean;
  platform?: string;
  backend?: string;
  max_batch_size?: number;
  inputs: Tensor[];
  outputs: Tensor[];
  gpu_ids: number[];
  dynamic_batching?: {
    preferred_batch_size?: number[];
    max_queue_delay_microseconds?: number;
  };
  inference_count: number;
  execution_count: number;
  success_count: number;
  failure_count: number;
  average_inference_ms?: number | null;
  last_inference?: number | null;
  can_unload: boolean;
};

type ModelResponse = {
  allow_unload: boolean;
  items: ManagedModel[];
};

function shape(tensor: Tensor) {
  return tensor.dims.join("×");
}

function modelIdentity(name: string) {
  if (name === "object-detector") {
    return { title: "Object Detector", family: "YOLO26s" };
  }
  if (name === "vehicle-embedding") {
    return { title: "Vehicle Embedding", family: "PP-ShiTuV2" };
  }
  return { title: name, family: undefined };
}

export default function DeepFrigateModelsSettingsView(
  _props: SettingsPageProps,
) {
  const isAdmin = useIsAdmin();
  const { data, error, isLoading, mutate } = useSWR<ModelResponse>(
    "deepfrigate/v1/models",
    { refreshInterval: 5000 },
  );
  const [pending, setPending] = useState<string>();

  const load = async (model: ManagedModel) => {
    const action = model.state === "READY" ? "recargar" : "cargar";
    if (
      model.state === "READY" &&
      !window.confirm(
        `¿Quieres recargar ${model.name}? Su pipeline puede pausarse brevemente.`,
      )
    ) {
      return;
    }
    setPending(model.name);
    try {
      await axios.post(
        `deepfrigate/v1/models/${encodeURIComponent(model.name)}/load`,
      );
      await mutate();
      toast.success(`${model.name}: ${action} completado`);
    } catch {
      toast.error(`No se pudo ${action} ${model.name}`);
    } finally {
      setPending(undefined);
    }
  };

  const unload = async (model: ManagedModel) => {
    if (!window.confirm(`¿Quieres descargar ${model.name} de Triton?`)) return;
    setPending(model.name);
    try {
      await axios.post(
        `deepfrigate/v1/models/${encodeURIComponent(model.name)}/unload`,
      );
      await mutate();
      toast.success(`${model.name}: descargado`);
    } catch {
      toast.error(`No se pudo descargar ${model.name}`);
    } finally {
      setPending(undefined);
    }
  };

  return (
    <div className="mx-auto w-full max-w-6xl space-y-5 p-4 md:p-6">
      <div>
        <Heading as="h2">Modelos de IA</Heading>
        <p className="text-sm text-muted-foreground">
          Estado operativo y métricas del repositorio Triton de DeepFrigate.
        </p>
      </div>

      {isLoading && <ActivityIndicator className="m-8" />}
      {error && (
        <div className="rounded-md border border-destructive p-4 text-sm text-destructive">
          Triton no está disponible.
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {data?.items.map((model) => {
          const identity = modelIdentity(model.name);
          return (
            <section
              className="rounded-lg border bg-card p-5"
              key={model.name}
            >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-lg font-semibold">{identity.title}</h3>
                  <Badge
                    variant={
                      model.state === "READY" ? "default" : "destructive"
                    }
                  >
                    {model.state === "READY" ? "ACTIVE" : model.state}
                  </Badge>
                  {model.required && (
                    <Badge variant="secondary">requerido</Badge>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {identity.family ? `${identity.family} · ` : ""}
                  versión {model.versions.join(", ") || "sin cargar"} ·{" "}
                  {model.platform ?? model.backend ?? "backend desconocido"}
                </p>
                {identity.title !== model.name && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Triton: {model.name}
                  </p>
                )}
              </div>
              {isAdmin && (
                <div className="flex gap-2">
                  <Button
                    disabled={pending === model.name}
                    onClick={() => load(model)}
                    size="sm"
                    variant="outline"
                  >
                    {pending === model.name
                      ? "Procesando…"
                      : model.state === "READY"
                        ? "Recargar"
                        : "Cargar"}
                  </Button>
                  {model.can_unload && (
                    <Button
                      disabled={pending === model.name}
                      onClick={() => unload(model)}
                      size="sm"
                      variant="destructive"
                    >
                      Descargar
                    </Button>
                  )}
                </div>
              )}
            </div>

            {model.reason && (
              <p className="mt-3 text-sm text-destructive">{model.reason}</p>
            )}

            <div className="mt-4 grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
              <div>
                <div className="text-muted-foreground">GPU</div>
                <div>{model.gpu_ids.join(", ") || "—"}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Batch máximo</div>
                <div>{model.max_batch_size ?? "—"}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Inferencias</div>
                <div>{model.inference_count.toLocaleString()}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Media GPU</div>
                <div>
                  {model.average_inference_ms == null
                    ? "—"
                    : `${model.average_inference_ms.toFixed(2)} ms`}
                </div>
              </div>
            </div>

            <div className="mt-4 grid gap-4 text-sm md:grid-cols-2">
              <div>
                <div className="mb-1 font-medium">Entradas</div>
                {model.inputs.map((input) => (
                  <div className="text-muted-foreground" key={input.name}>
                    {input.name}: {input.data_type} [{shape(input)}]
                  </div>
                ))}
              </div>
              <div>
                <div className="mb-1 font-medium">Salidas</div>
                {model.outputs.map((output) => (
                  <div className="text-muted-foreground" key={output.name}>
                    {output.name}: {output.data_type} [{shape(output)}]
                  </div>
                ))}
              </div>
            </div>

            {model.dynamic_batching && (
              <div className="mt-4 text-xs text-muted-foreground">
                Dynamic batching:{" "}
                {model.dynamic_batching.preferred_batch_size?.join(", ") ||
                  "activo"}
                {model.dynamic_batching.max_queue_delay_microseconds
                  ? ` · cola ${model.dynamic_batching.max_queue_delay_microseconds} µs`
                  : ""}
              </div>
            )}
          </section>
          );
        })}
      </div>

      {data && !data.allow_unload && (
        <p className="text-xs text-muted-foreground">
          La descarga de modelos está deshabilitada para evitar interrumpir los
          pipelines activos.
        </p>
      )}
    </div>
  );
}
