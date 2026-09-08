import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

/**
 * Interactive map of the DeepFrigate pipeline (Settings → DeepFrigate →
 * Workflow visual). Nodes are draggable (positions kept per browser); the
 * editable bits live on the nodes themselves: camera on/off, detector model,
 * enrichment on/off. Everything else is shown, not edited, and the detailed
 * form below the canvas still covers it.
 *
 * Dark "signal-flow" look on purpose: the pipeline is read left to right,
 * cameras → DeepStream → Triton → two branches (events, enrichment).
 */

export type CanvasCamera = { id: string; enabled?: boolean; source_env: string };
export type CanvasEnrichment = { model: string; family: string; labels: string[]; enabled?: boolean };
export type CanvasPipeline = {
  name: string;
  cameras: CanvasCamera[];
  detection: { model: string; version: number };
  tracker: { type: string; width: number; height: number };
  frame_export?: { labels: string[] };
  enrichments?: CanvasEnrichment[];
  rules?: { type: string; camera: string; zone: string }[];
};
export type CanvasOptions = {
  detection_models: string[];
  enrichment_models: string[];
  zones: Record<string, string[]>;
};
export type CanvasStatus = {
  adapter_reachable?: boolean;
  cameras?: Record<string, { enabled: boolean; seen_by_adapter: boolean; active_objects: number }>;
  models?: Record<string, { ready: boolean }>;
  services?: Record<string, { ok: boolean }>;
};

type Actions = {
  disabled: boolean;
  toggleCamera: (index: number) => void;
  setDetectorModel: (model: string) => void;
  toggleEnrichment: (index: number) => void;
};

type Props = {
  pipeline: CanvasPipeline;
  options?: CanvasOptions;
  status?: CanvasStatus;
  actions: Actions;
};

const STORAGE_KEY = "deepfrigate.workflow.positions.v1";
const FAMILY_NAMES: Record<string, string> = {
  "pp-shitu": "PP-ShiTu · embeddings",
  "pulc-person": "PULC persona · atributos",
  "pulc-vehicle": "PULC coche · atributos",
};

// ---------------------------------------------------------------- visuals

function Dot({ state }: { state: "ok" | "off" | "warn" | "unknown" }) {
  const color =
    state === "ok" ? "bg-emerald-400" : state === "warn" ? "bg-amber-400" : state === "off" ? "bg-slate-500" : "bg-slate-600";
  return <span className={`inline-block size-2 rounded-full ${color}`} />;
}

function Card({
  accent,
  title,
  tag,
  children,
  dim,
  handles = "both",
}: {
  accent: string;
  title: string;
  tag?: string;
  children?: ReactNode;
  dim?: boolean;
  handles?: "both" | "left" | "right" | "none";
}) {
  return (
    <div
      className={`w-[220px] rounded-lg border bg-[#0d1424] font-mono text-[11px] text-slate-200 shadow-lg shadow-black/40 ${dim ? "opacity-50" : ""}`}
      style={{ borderColor: `${accent}55`, borderLeft: `3px solid ${accent}` }}
    >
      {(handles === "both" || handles === "left") && (
        <Handle className="!size-2 !border-0 !bg-slate-400" position={Position.Left} type="target" />
      )}
      <div className="flex items-center justify-between gap-2 px-3 pt-2">
        <span className="text-[12px] font-semibold tracking-wide text-slate-100">{title}</span>
        {tag && (
          <span className="rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wider" style={{ background: `${accent}22`, color: accent }}>
            {tag}
          </span>
        )}
      </div>
      <div className="px-3 pb-2.5 pt-1 text-slate-400">{children}</div>
      {(handles === "both" || handles === "right") && (
        <Handle className="!size-2 !border-0 !bg-slate-400" position={Position.Right} type="source" />
      )}
    </div>
  );
}

function Toggle({ on, disabled, onClick, label }: { on: boolean; disabled: boolean; onClick: () => void; label: string }) {
  return (
    <button
      aria-label={label}
      className={`nodrag relative h-4 w-8 rounded-full transition ${on ? "bg-cyan-500" : "bg-slate-600"} ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      type="button"
    >
      <span className={`absolute top-0.5 size-3 rounded-full bg-white transition ${on ? "left-4" : "left-0.5"}`} />
    </button>
  );
}

const CYAN = "#22d3ee";
const AMBER = "#fbbf24";
const VIOLET = "#a78bfa";
const EMERALD = "#34d399";
const ROSE = "#fb7185";

// ------------------------------------------------------------------- nodes

type CameraData = { camera: CanvasCamera; index: number; status?: CanvasStatus["cameras"] extends infer T ? (T extends Record<string, infer V> ? V : never) : never; actions: Actions };
function CameraNode({ data }: NodeProps<Node<CameraData>>) {
  const enabled = data.camera.enabled !== false;
  const seen = data.status?.seen_by_adapter;
  const objects = data.status?.active_objects ?? 0;
  return (
    <Card accent={CYAN} dim={!enabled} handles="right" tag="RTSP" title={data.camera.id}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate">{data.camera.source_env}</span>
        <Toggle disabled={data.actions.disabled} label={`Cámara ${data.camera.id}`} on={enabled} onClick={() => data.actions.toggleCamera(data.index)} />
      </div>
      <div className="mt-1 flex items-center gap-1.5">
        <Dot state={!enabled ? "off" : seen ? "ok" : "warn"} />
        <span>{!enabled ? "apagada" : seen ? `${objects} objetos activos` : "sin datos del adapter"}</span>
      </div>
    </Card>
  );
}

type DeepStreamData = { cameras: number; enabled: number; tracker: CanvasPipeline["tracker"] };
function DeepStreamNode({ data }: NodeProps<Node<DeepStreamData>>) {
  return (
    <Card accent={EMERALD} tag="GPU T4" title="DeepStream">
      <div>nvmultiurisrcbin · mux 1280×720</div>
      <div>{data.enabled}/{data.cameras} cámaras en el batch</div>
      <div className="mt-1 text-slate-500">{data.tracker.type} · NvDCF {data.tracker.width}×{data.tracker.height}</div>
    </Card>
  );
}

type DetectorData = { model: string; version: number; models: string[]; ready?: boolean; actions: Actions };
function DetectorNode({ data }: NodeProps<Node<DetectorData>>) {
  return (
    <Card accent={VIOLET} tag="Triton" title="Inferencia primaria">
      <select
        className="nodrag mt-0.5 w-full rounded border border-slate-700 bg-[#111a2e] px-1.5 py-1 text-[11px] text-slate-100"
        disabled={data.actions.disabled}
        onChange={(event) => data.actions.setDetectorModel(event.target.value)}
        value={data.model}
      >
        {(data.models.length ? data.models : [data.model]).map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
      </select>
      <div className="mt-1 flex items-center gap-1.5">
        <Dot state={data.ready === undefined ? "unknown" : data.ready ? "ok" : "warn"} />
        <span>v{data.version} · {data.ready === undefined ? "estado desconocido" : data.ready ? "listo en Triton" : "no cargado"}</span>
      </div>
    </Card>
  );
}

type PlainData = { title: string; tag?: string; lines: string[]; accent: string; state?: "ok" | "warn" | "unknown"; handles?: "both" | "left" | "right" | "none" };
function PlainNode({ data }: NodeProps<Node<PlainData>>) {
  return (
    <Card accent={data.accent} handles={data.handles} tag={data.tag} title={data.title}>
      {data.lines.map((line, i) => (
        <div className={i === 0 ? "" : "text-slate-500"} key={i}>
          {i === 0 && data.state ? (
            <span className="mr-1.5 inline-flex align-middle">
              <Dot state={data.state} />
            </span>
          ) : null}
          {line}
        </div>
      ))}
    </Card>
  );
}

type EnrichmentData = { enrichment: CanvasEnrichment; index: number; ready?: boolean; actions: Actions; note?: string };
function EnrichmentNode({ data }: NodeProps<Node<EnrichmentData>>) {
  const enabled = data.enrichment.enabled !== false;
  return (
    <Card accent={AMBER} dim={!enabled} handles="left" tag={data.enrichment.family} title={data.enrichment.model}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate">{FAMILY_NAMES[data.enrichment.family] ?? data.enrichment.family}</span>
        <Toggle disabled={data.actions.disabled} label={`Enriquecimiento ${data.enrichment.model}`} on={enabled} onClick={() => data.actions.toggleEnrichment(data.index)} />
      </div>
      <div className="mt-1 flex items-center gap-1.5">
        <Dot state={!enabled ? "off" : data.ready === undefined ? "unknown" : data.ready ? "ok" : "warn"} />
        <span>{data.enrichment.labels.join(" · ") || "sin labels"}{data.ready === false && enabled ? " · no cargado" : ""}</span>
      </div>
      {data.note && <div className="mt-1 text-[10px] text-slate-500">{data.note}</div>}
    </Card>
  );
}

const nodeTypes = {
  camera: CameraNode,
  deepstream: DeepStreamNode,
  detector: DetectorNode,
  plain: PlainNode,
  enrichment: EnrichmentNode,
};

// ------------------------------------------------------------------ layout

const COL = { cam: 0, ds: 300, det: 580, tee: 860, branch: 1140, out: 1420, far: 1700 };

function defaultPosition(id: string, index: number): { x: number; y: number } {
  if (id.startsWith("cam:")) return { x: COL.cam, y: 20 + index * 110 };
  switch (id) {
    case "deepstream":
      return { x: COL.ds, y: 80 };
    case "detector":
      return { x: COL.det, y: 80 };
    case "tee":
      return { x: COL.tee, y: 96 };
    case "adapter":
      return { x: COL.branch, y: 0 };
    case "event_engine":
      return { x: COL.out, y: 0 };
    case "frigate":
      return { x: COL.far, y: 0 };
    case "frame_store":
      return { x: COL.branch, y: 220 };
    case "ai_router":
      return { x: COL.out, y: 220 };
    case "alpr":
      return { x: COL.far, y: 140 };
  }
  if (id.startsWith("enr:")) return { x: COL.far, y: 250 + index * 105 };
  return { x: 0, y: 0 };
}

function loadPositions(): Record<string, { x: number; y: number }> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
  } catch {
    return {};
  }
}

export default function DeepFrigateWorkflowCanvas({ pipeline, options, status, actions }: Props) {
  const saved = useRef(loadPositions());
  const [nodes, setNodes] = useState<Node[]>([]);

  const built = useMemo<{ nodes: Node[]; edges: Edge[] }>(() => {
    const enabledCams = pipeline.cameras.filter((c) => c.enabled !== false).length;
    const enrichments = pipeline.enrichments ?? [];
    const zoneCount = Object.values(options?.zones ?? {}).reduce((n, z) => n + z.length, 0);
    const pos = (id: string, index = 0) => saved.current[id] ?? defaultPosition(id, index);

    const list: Node[] = [];
    pipeline.cameras.forEach((camera, index) => {
      list.push({ id: `cam:${camera.id}`, type: "camera", position: pos(`cam:${camera.id}`, index), data: { camera, index, status: status?.cameras?.[camera.id], actions } });
    });
    list.push({ id: "deepstream", type: "deepstream", position: pos("deepstream"), data: { cameras: pipeline.cameras.length, enabled: enabledCams, tracker: pipeline.tracker } });
    list.push({ id: "detector", type: "detector", position: pos("detector"), data: { model: pipeline.detection.model, version: pipeline.detection.version, models: options?.detection_models ?? [], ready: status?.models?.[pipeline.detection.model]?.ready, actions } });
    list.push({ id: "tee", type: "plain", position: pos("tee"), data: { title: "tee", accent: EMERALD, lines: ["MQTT detections", "crops RGB → SHM"] } });
    list.push({ id: "adapter", type: "plain", position: pos("adapter"), data: { title: "detection-adapter", tag: "eventos", accent: CYAN, state: status?.adapter_reachable === undefined ? "unknown" : status.adapter_reachable ? "ok" : "warn", lines: ["START/UPDATE/LOST/END", `${zoneCount} zonas de Frigate · ${(pipeline.rules ?? []).length} reglas`] } });
    list.push({ id: "event_engine", type: "plain", position: pos("event_engine"), data: { title: "event-engine", tag: "PG", accent: CYAN, lines: ["events · puente Frigate", "transiciones entre cámaras"] } });
    list.push({ id: "frigate", type: "plain", position: pos("frigate"), data: { title: "Frigate", tag: "UI + NVR", accent: CYAN, handles: "left", lines: ["Explore · Review · grabación", "zonas y líneas se dibujan aquí"] } });
    list.push({ id: "frame_store", type: "plain", position: pos("frame_store"), data: { title: "frame-store", tag: "SHM", accent: AMBER, state: status?.services?.frame_store ? (status.services.frame_store.ok ? "ok" : "warn") : "unknown", lines: [`FrameRef · ${(pipeline.frame_export?.labels ?? []).join(", ") || "sin labels"}`, "crops por track, no por frame"] } });
    list.push({ id: "ai_router", type: "plain", position: pos("ai_router"), data: { title: "ai-router", tag: "por crop", accent: AMBER, lines: ["reparte cada crop", "Triton · alpr-worker"] } });
    list.push({ id: "alpr", type: "plain", position: pos("alpr"), data: { title: "alpr-worker", tag: "OpenALPR", accent: ROSE, handles: "left", state: status?.services?.alpr_worker ? (status.services.alpr_worker.ok ? "ok" : "warn") : "unknown", lines: ["placa (voto entre pasadas)", "marca · modelo · color · tipo"] } });
    enrichments.forEach((enrichment, index) => {
      const note = enrichment.family === "pulc-vehicle" ? "sustituido por OpenALPR (VEHICLE_ATTRIBUTE_PROVIDER)" : undefined;
      list.push({ id: `enr:${enrichment.model}:${index}`, type: "enrichment", position: pos(`enr:${enrichment.model}:${index}`, index), data: { enrichment, index, ready: status?.models?.[enrichment.model]?.ready, actions, note } });
    });

    const main = { animated: true, style: { stroke: CYAN, strokeWidth: 1.6 }, labelStyle: { fill: "#94a3b8", fontSize: 10, fontFamily: "ui-monospace, monospace" }, labelBgStyle: { fill: "#0a0f1a", fillOpacity: 0.9 } };
    const side = { animated: false, style: { stroke: AMBER, strokeWidth: 1.2, strokeDasharray: "6 4" }, labelStyle: main.labelStyle, labelBgStyle: main.labelBgStyle };
    const edgeList: Edge[] = [];
    pipeline.cameras.forEach((camera) => {
      const on = camera.enabled !== false;
      edgeList.push({ id: `e:cam:${camera.id}`, source: `cam:${camera.id}`, target: "deepstream", ...main, animated: on, style: { ...main.style, opacity: on ? 1 : 0.25 } });
    });
    edgeList.push({ id: "e:ds-det", source: "deepstream", target: "detector", label: "gRPC · CUDA IPC", ...main });
    edgeList.push({ id: "e:det-tee", source: "detector", target: "tee", label: "bboxes + track", ...main });
    edgeList.push({ id: "e:tee-adapter", source: "tee", target: "adapter", label: "MQTT", ...main });
    edgeList.push({ id: "e:adapter-ee", source: "adapter", target: "event_engine", label: "ciclo de vida · zonas", ...main });
    edgeList.push({ id: "e:ee-frigate", source: "event_engine", target: "frigate", label: "Events · Timeline", ...main });
    edgeList.push({ id: "e:tee-fs", source: "tee", target: "frame_store", label: "crops", ...side });
    edgeList.push({ id: "e:fs-ai", source: "frame_store", target: "ai_router", label: "FrameRef", ...side });
    edgeList.push({ id: "e:ai-alpr", source: "ai_router", target: "alpr", label: "car", ...side });
    edgeList.push({ id: "e:ai-ee", source: "ai_router", target: "event_engine", label: "classification · plate", ...side });
    enrichments.forEach((enrichment, index) => {
      const on = enrichment.enabled !== false;
      edgeList.push({ id: `e:ai-enr:${index}`, source: "ai_router", target: `enr:${enrichment.model}:${index}`, label: enrichment.labels.join(","), ...side, style: { ...side.style, opacity: on ? 1 : 0.25 } });
    });
    return { nodes: list, edges: edgeList };
  }, [pipeline, options, status, actions]);

  useEffect(() => {
    setNodes((current) => {
      const positions = new Map(current.map((n) => [n.id, n.position]));
      return built.nodes.map((n) => ({ ...n, position: positions.get(n.id) ?? n.position }));
    });
  }, [built]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current);
      if (changes.some((c) => c.type === "position" && c.dragging === false)) {
        const store: Record<string, { x: number; y: number }> = {};
        next.forEach((n) => {
          store[n.id] = n.position;
        });
        saved.current = store;
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
        } catch {
          /* per-browser convenience only */
        }
      }
      return next;
    });
  }, []);

  const resetLayout = () => {
    saved.current = {};
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    setNodes(built.nodes.map((n) => ({ ...n, position: defaultPosition(n.id, Number(n.id.split(":").pop()) || (n.data as { index?: number }).index || 0) })));
  };

  return (
    <div className="overflow-hidden rounded-lg border border-slate-800 bg-[#0a0f1a]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-3 py-2 font-mono text-[11px] text-slate-400">
        <div>
          <span className="font-semibold text-slate-200">{pipeline.name}</span>
          <span className="ml-2">cámaras → DeepStream → Triton → eventos · enriquecimiento</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1"><Dot state="ok" /> vivo</span>
          <span className="flex items-center gap-1"><Dot state="warn" /> sin señal</span>
          <span className="flex items-center gap-1"><Dot state="off" /> apagado</span>
          <button className="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800" onClick={resetLayout} type="button">
            Reordenar
          </button>
        </div>
      </div>
      <div className="h-[620px] w-full">
        <ReactFlow
          colorMode="dark"
          edges={built.edges}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          maxZoom={1.5}
          minZoom={0.3}
          nodeTypes={nodeTypes}
          nodes={nodes}
          nodesConnectable={false}
          onNodesChange={onNodesChange}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#1e293b" gap={24} variant={BackgroundVariant.Dots} />
          <Controls showInteractive={false} />
          <MiniMap maskColor="rgba(10,15,26,0.7)" nodeColor="#1e3a5f" pannable zoomable />
        </ReactFlow>
      </div>
      <div className="border-t border-slate-800 px-3 py-2 font-mono text-[10px] text-slate-500">
        Guardar aplica: cámaras on/off en caliente · modelo o tracker reinician video-engine (~30 s) · enriquecimientos on/off: declarativo hasta que ai-router lea el contrato.
      </div>
    </div>
  );
}
