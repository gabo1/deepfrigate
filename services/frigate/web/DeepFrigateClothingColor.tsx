/** Clothing color chips — same hex map as /opt/analitica/viewer.html. */

export const CLOTHING_HEX: Record<string, string> = {
  black: "#1b1f24",
  white: "#f0f6fc",
  gray: "#8b949e",
  red: "#e5534b",
  orange: "#e8834a",
  yellow: "#e3c341",
  green: "#3fb950",
  cyan: "#39c5cf",
  blue: "#4c8dd8",
  purple: "#a371f7",
  pink: "#db61a2",
  negro: "#1b1f24",
  blanco: "#f0f6fc",
  gris: "#8b949e",
  rojo: "#e5534b",
  naranja: "#e8834a",
  amarillo: "#e3c341",
  verde: "#3fb950",
  cian: "#39c5cf",
  azul: "#4c8dd8",
  morado: "#a371f7",
  rosa: "#db61a2",
};

export type ClothingColors = {
  upper?: string;
  lower?: string;
};

type AttributeEntry = {
  value?: string;
};

export function clothingHex(name: string): string {
  return CLOTHING_HEX[name.toLowerCase()] ?? "#8b949e";
}

export function readClothingColors(data: unknown): ClothingColors {
  if (!data || typeof data !== "object") {
    return {};
  }
  const raw = (data as Record<string, unknown>).person_attributes;
  if (!raw || typeof raw !== "object") {
    return {};
  }
  const attrs = raw as Record<string, AttributeEntry | unknown>;
  const upper =
    typeof attrs.upper_color === "object" && attrs.upper_color
      ? String((attrs.upper_color as AttributeEntry).value || "")
      : "";
  const lower =
    typeof attrs.lower_color === "object" && attrs.lower_color
      ? String((attrs.lower_color as AttributeEntry).value || "")
      : "";
  return {
    upper: upper || undefined,
    lower: lower || undefined,
  };
}

export function ClothingColorChip({
  name,
  size = 9,
}: {
  name: string;
  size?: number;
}) {
  return (
    <span
      title={name}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        background: clothingHex(name),
        border: "1px solid rgba(0,0,0,.6)",
        verticalAlign: "middle",
        flexShrink: 0,
      }}
    />
  );
}

export function DeepFrigateBoxColorSwatchesSvg({
  box,
  videoWidth,
  videoHeight,
  upper,
  lower,
}: {
  box: number[];
  videoWidth: number;
  videoHeight: number;
  upper?: string;
  lower?: string;
}) {
  if (box.length < 4 || (!upper && !lower)) {
    return null;
  }
  const [x, y, width] = box;
  const flipLeft = x + width > 0.92;
  const left = flipLeft
    ? x * videoWidth - 12
    : (x + width) * videoWidth + 3;
  const top = y * videoHeight;
  const items = [upper, lower].filter((value): value is string => Boolean(value));
  return (
    <g pointerEvents="none">
      {items.map((name, index) => (
        <rect
          key={`${name}-${index}`}
          x={left}
          y={top + index * 11}
          width={9}
          height={9}
          fill={clothingHex(name)}
          stroke="rgba(0,0,0,.6)"
          strokeWidth={1}
        />
      ))}
    </g>
  );
}

export default function DeepFrigateSnapshotColorSwatches({
  box,
  upper,
  lower,
}: {
  box?: number[] | null;
  upper?: string;
  lower?: string;
}) {
  if (!box || box.length < 4 || (!upper && !lower)) {
    return null;
  }
  const [x, y, width] = box;
  const flipLeft = x + width > 0.92;
  const items = [upper, lower].filter((value): value is string => Boolean(value));
  return (
    <div className="pointer-events-none absolute inset-0" aria-hidden>
      <div
        style={{
          position: "absolute",
          left: flipLeft
            ? `calc(${x * 100}% - 12px)`
            : `calc(${(x + width) * 100}% + 3px)`,
          top: `${y * 100}%`,
          display: "flex",
          flexDirection: "column",
          gap: 2,
        }}
      >
        {items.map((name, index) => (
          <ClothingColorChip key={`${name}-${index}`} name={name} />
        ))}
      </div>
    </div>
  );
}
