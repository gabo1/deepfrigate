import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import Heading from "@/components/ui/heading";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/use-is-admin";
import type { SettingsPageProps } from "@/views/settings/SingleSectionPage";
import axios from "axios";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import useSWR from "swr";

type Camera = {
  id: string;
  source_env: string;
  gpu: number;
};

type Detection = {
  model: string;
  version: number;
  config_path: string;
  gpu: number;
};

type Tracker = {
  type: string;
  config_path: string;
  width: number;
  height: number;
  gpu: number;
};

type Enrichment = {
  model: string;
  family: string;
  labels: string[];
};

type Rule = {
  type: "zone";
  camera: string;
  zone: string;
};

type PipelineDocument = {
  api_version: "deepfrigate/v1";
  pipeline: {
    name: string;
    cameras: Camera[];
    detection: Detection;
    tracker: Tracker;
    frame_export?: { labels: string[] };
    enrichments?: Enrichment[];
    rules?: Rule[];
  };
};

type ActivePipelineResponse = {
  api_version: "deepfrigate/v1";
  name: string;
  source_sha256: string;
  restart_required_for_changes: boolean;
  pipeline: PipelineDocument["pipeline"];
};

type PipelineOptions = {
  models: string[];
  detection_models: string[];
  enrichment_models: string[];
  zones: Record<string, string[]>;
};

function labels(value: string) {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

function apiError(error: unknown) {
  if (axios.isAxiosError(error)) {
    return error.response?.data?.detail ?? error.message;
  }
  return "Error desconocido";
}

function WorkflowNode({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <section className="w-full max-w-2xl rounded-xl border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold">{title}</h3>
          <p className="text-xs text-muted-foreground">{subtitle}</p>
        </div>
        <Badge variant="secondary">nodo</Badge>
      </div>
      {children}
    </section>
  );
}

function Connector() {
  return <div className="py-1 text-center text-xl text-muted-foreground">↓</div>;
}

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="space-y-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

const selectClass =
  "h-10 w-full rounded-md border border-input bg-background px-3 text-sm";

export default function DeepFrigateWorkflowSettingsView(
  _props: SettingsPageProps,
) {
  const isAdmin = useIsAdmin();
  const { data, error, isLoading, mutate } = useSWR<ActivePipelineResponse>(
    "deepfrigate/v1/pipelines/active",
  );
  const { data: options } = useSWR<PipelineOptions>(
    "deepfrigate/v1/pipelines/options",
  );
  const [draft, setDraft] = useState<PipelineDocument>();
  const [pending, setPending] = useState<"validate" | "save">();
  const [validated, setValidated] = useState(false);

  useEffect(() => {
    if (!data) return;
    setDraft({
      api_version: data.api_version,
      pipeline: structuredClone(data.pipeline),
    });
    setValidated(false);
  }, [data]);

  const original = useMemo(
    () =>
      data
        ? JSON.stringify({
            api_version: data.api_version,
            pipeline: data.pipeline,
          })
        : "",
    [data],
  );
  const dirty = draft ? JSON.stringify(draft) !== original : false;
  const disabled = !isAdmin || pending !== undefined;

  const updatePipeline = (
    update: (pipeline: PipelineDocument["pipeline"]) => void,
  ) => {
    setDraft((current) => {
      if (!current) return current;
      const next = structuredClone(current);
      update(next.pipeline);
      return next;
    });
    setValidated(false);
  };

  const validate = async () => {
    if (!draft) return;
    setPending("validate");
    try {
      await axios.post("deepfrigate/v1/pipelines/validate", draft);
      setValidated(true);
      toast.success("Workflow válido");
    } catch (validationError) {
      setValidated(false);
      toast.error(apiError(validationError));
    } finally {
      setPending(undefined);
    }
  };

  const save = async () => {
    if (!draft || !data) return;
    setPending("save");
    try {
      await axios.put("deepfrigate/v1/pipelines/active", draft, {
        headers: { "If-Match": data.source_sha256 },
      });
      await mutate();
      toast.success("Workflow guardado; reinicia Video Engine para activarlo");
    } catch (saveError) {
      toast.error(apiError(saveError));
    } finally {
      setPending(undefined);
    }
  };

  if (isLoading || !draft) {
    return <ActivityIndicator className="m-8" />;
  }

  if (error) {
    return (
      <div className="m-6 rounded-md border border-destructive p-4 text-destructive">
        No se pudo cargar el workflow activo.
      </div>
    );
  }

  const pipeline = draft.pipeline;
  const enrichments = pipeline.enrichments ?? [];
  const rules = pipeline.rules ?? [];

  return (
    <div className="mx-auto w-full max-w-6xl space-y-5 p-4 md:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Heading as="h2">Workflow visual</Heading>
          <p className="text-sm text-muted-foreground">
            Configura el pipeline declarativo que alimenta DeepStream, Triton y
            las reglas de eventos.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {dirty && <Badge variant="secondary">cambios sin guardar</Badge>}
          {validated && <Badge>válido</Badge>}
          {!isAdmin && <Badge variant="outline">solo lectura</Badge>}
          <Button
            disabled={disabled || !dirty}
            onClick={() => {
              if (data) {
                setDraft({
                  api_version: data.api_version,
                  pipeline: structuredClone(data.pipeline),
                });
              }
            }}
            variant="outline"
          >
            Descartar
          </Button>
          <Button disabled={disabled} onClick={validate} variant="outline">
            {pending === "validate" ? "Validando…" : "Validar"}
          </Button>
          <Button disabled={disabled || !dirty} onClick={save}>
            {pending === "save" ? "Guardando…" : "Guardar"}
          </Button>
        </div>
      </div>

      <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm">
        Guardar actualiza el contrato. Para aplicar el cambio al pipeline GPU es
        necesario reiniciar Video Engine.
      </div>

      <div className="flex flex-col items-center">
        <WorkflowNode title="Pipeline" subtitle="Contrato deepfrigate/v1">
          <Field label="Nombre">
            <Input
              disabled={disabled}
              value={pipeline.name}
              onChange={(event) =>
                updatePipeline((item) => {
                  item.name = event.target.value;
                })
              }
            />
          </Field>
        </WorkflowNode>

        <Connector />

        <WorkflowNode title="Cámaras" subtitle="Fuentes RTSP por variable de entorno">
          <div className="grid gap-3 md:grid-cols-2">
            {pipeline.cameras.map((camera, index) => (
              <div className="rounded-md border p-3" key={camera.id}>
                <div className="mb-2 font-medium">{camera.id}</div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <Field label="Variable de fuente">
                    <Input
                      disabled={disabled}
                      value={camera.source_env}
                      onChange={(event) =>
                        updatePipeline((item) => {
                          item.cameras[index].source_env = event.target.value;
                        })
                      }
                    />
                  </Field>
                  <Field label="GPU">
                    <Input
                      disabled={disabled}
                      min={0}
                      type="number"
                      value={camera.gpu}
                      onChange={(event) =>
                        updatePipeline((item) => {
                          item.cameras[index].gpu = Number(event.target.value);
                        })
                      }
                    />
                  </Field>
                </div>
              </div>
            ))}
          </div>
        </WorkflowNode>

        <Connector />

        <WorkflowNode title="YOLO / Triton" subtitle="Inferencia primaria">
          <div className="grid gap-3 md:grid-cols-3">
            <Field label="Modelo">
              <select
                className={selectClass}
                disabled={disabled}
                value={pipeline.detection.model}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.detection.model = event.target.value;
                  })
                }
              >
                {(options?.detection_models ?? [pipeline.detection.model]).map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Versión">
              <Input
                disabled={disabled}
                min={1}
                type="number"
                value={pipeline.detection.version}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.detection.version = Number(event.target.value);
                  })
                }
              />
            </Field>
            <Field label="GPU">
              <Input
                disabled={disabled}
                min={0}
                type="number"
                value={pipeline.detection.gpu}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.detection.gpu = Number(event.target.value);
                  })
                }
              />
            </Field>
          </div>
        </WorkflowNode>

        <Connector />

        <WorkflowNode title="NvTracker" subtitle={pipeline.tracker.type}>
          <div className="grid gap-3 md:grid-cols-3">
            <Field label="Ancho">
              <Input
                disabled={disabled}
                min={1}
                type="number"
                value={pipeline.tracker.width}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.tracker.width = Number(event.target.value);
                  })
                }
              />
            </Field>
            <Field label="Alto">
              <Input
                disabled={disabled}
                min={1}
                type="number"
                value={pipeline.tracker.height}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.tracker.height = Number(event.target.value);
                  })
                }
              />
            </Field>
            <Field label="GPU">
              <Input
                disabled={disabled}
                min={0}
                type="number"
                value={pipeline.tracker.gpu}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.tracker.gpu = Number(event.target.value);
                  })
                }
              />
            </Field>
          </div>
        </WorkflowNode>

        <Connector />

        <div className="grid w-full max-w-4xl gap-4 md:grid-cols-2">
          <WorkflowNode title="FrameRef" subtitle="Etiquetas exportadas">
            <Field label="Labels separados por coma">
              <Input
                disabled={disabled}
                value={(pipeline.frame_export?.labels ?? []).join(", ")}
                onChange={(event) =>
                  updatePipeline((item) => {
                    item.frame_export = { labels: labels(event.target.value) };
                  })
                }
              />
            </Field>
          </WorkflowNode>

          <WorkflowNode title="Enriquecimientos" subtitle="Rama AI Router">
            <div className="space-y-3">
              {enrichments.map((enrichment, index) => (
                <div className="space-y-2 rounded-md border p-3" key={`${enrichment.model}-${index}`}>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <Field label="Modelo">
                      <select
                        className={selectClass}
                        disabled={disabled}
                        value={enrichment.model}
                        onChange={(event) =>
                          updatePipeline((item) => {
                            item.enrichments![index].model = event.target.value;
                          })
                        }
                      >
                        {(options?.enrichment_models ?? [enrichment.model]).map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label="Labels">
                      <Input
                        disabled={disabled}
                        value={enrichment.labels.join(", ")}
                        onChange={(event) =>
                          updatePipeline((item) => {
                            item.enrichments![index].labels = labels(
                              event.target.value,
                            );
                          })
                        }
                      />
                    </Field>
                  </div>
                  <Button
                    disabled={disabled}
                    onClick={() =>
                      updatePipeline((item) => {
                        item.enrichments!.splice(index, 1);
                      })
                    }
                    size="sm"
                    variant="destructive"
                  >
                    Quitar
                  </Button>
                </div>
              ))}
              <Button
                disabled={disabled}
                onClick={() =>
                  updatePipeline((item) => {
                    item.enrichments ??= [];
                    item.enrichments.push({
                      model:
                        options?.enrichment_models[0] ?? "vehicle-embedding",
                      family: "pp-shitu",
                      labels: ["car"],
                    });
                  })
                }
                size="sm"
                variant="outline"
              >
                Añadir PP-ShiTu
              </Button>
            </div>
          </WorkflowNode>
        </div>

        <Connector />

        <WorkflowNode title="Reglas de zona" subtitle="Event Engine">
          <div className="space-y-3">
            {rules.map((rule, index) => (
              <div className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_1fr_auto]" key={`${rule.camera}-${rule.zone}-${index}`}>
                <Field label="Cámara">
                  <select
                    className={selectClass}
                    disabled={disabled}
                    value={rule.camera}
                    onChange={(event) =>
                      updatePipeline((item) => {
                        const nextCamera = event.target.value;
                        item.rules![index].camera = nextCamera;
                        item.rules![index].zone =
                          options?.zones[nextCamera]?.[0] ?? "";
                      })
                    }
                  >
                    {pipeline.cameras.map((camera) => (
                      <option key={camera.id} value={camera.id}>
                        {camera.id}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Zona">
                  <select
                    className={selectClass}
                    disabled={disabled}
                    value={rule.zone}
                    onChange={(event) =>
                      updatePipeline((item) => {
                        item.rules![index].zone = event.target.value;
                      })
                    }
                  >
                    {(options?.zones[rule.camera] ?? [rule.zone]).map((zone) => (
                      <option key={zone} value={zone}>
                        {zone}
                      </option>
                    ))}
                  </select>
                </Field>
                <Button
                  className="self-end"
                  disabled={disabled}
                  onClick={() =>
                    updatePipeline((item) => {
                      item.rules!.splice(index, 1);
                    })
                  }
                  size="sm"
                  variant="destructive"
                >
                  Quitar
                </Button>
              </div>
            ))}
            <Button
              disabled={disabled || pipeline.cameras.length === 0}
              onClick={() =>
                updatePipeline((item) => {
                  const camera = item.cameras[0]?.id ?? "";
                  item.rules ??= [];
                  item.rules.push({
                    type: "zone",
                    camera,
                    zone: options?.zones[camera]?.[0] ?? "",
                  });
                })
              }
              size="sm"
              variant="outline"
            >
              Añadir regla
            </Button>
          </div>
        </WorkflowNode>

        <Connector />

        <div className="w-full max-w-2xl rounded-xl border border-dashed p-4 text-center text-sm text-muted-foreground">
          Webhook · próximo tipo de salida
        </div>
      </div>
    </div>
  );
}
