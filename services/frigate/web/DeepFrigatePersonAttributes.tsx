import { SearchResult } from "@/types/search";
import { ClothingColorChip } from "@/components/overlay/detail/DeepFrigateClothingColor";

type AttributeEntry = {
  value: string;
  score: number;
  uncertain?: boolean;
  also?: string[];
};

const LABELS: Record<string, string> = {
  gender: "Género",
  age: "Edad",
  orientation: "Orientación",
  sleeve: "Manga",
  lower: "Prenda",
  glasses: "Gafas",
  hat: "Gorro",
  holding_object: "En la mano",
  bag: "Bolso",
  upper_color: "Arriba",
  lower_color: "Abajo",
  color: "Color",
  body_type: "Tipo",
};

const ORDER = [
  "color",
  "body_type",
  "gender",
  "age",
  "orientation",
  "sleeve",
  "lower",
  "upper_color",
  "lower_color",
  "glasses",
  "hat",
  "bag",
  "holding_object",
] as const;

const BOOLEAN_FIELDS = new Set(["glasses", "hat", "holding_object"]);
const COLOR_FIELDS = new Set(["upper_color", "lower_color", "color"]);

function isEntry(value: unknown): value is AttributeEntry {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as AttributeEntry).value === "string" &&
    typeof (value as AttributeEntry).score === "number"
  );
}

function readAttributes(
  search: SearchResult,
): Record<string, AttributeEntry> {
  const data = search.data as Record<string, unknown>;
  const entries: Record<string, AttributeEntry> = {};
  for (const key of ["vehicle_attributes", "person_attributes"] as const) {
    const raw = data[key];
    if (!raw || typeof raw !== "object") {
      continue;
    }
    for (const [name, value] of Object.entries(raw)) {
      if (name !== "updated_at" && isEntry(value) && !(name in entries)) {
        entries[name] = value;
      }
    }
  }
  return entries;
}

export default function DeepFrigatePersonAttributes({
  search,
}: {
  search: SearchResult;
}) {
  const attributes = readAttributes(search);
  const names = [
    ...ORDER.filter((name) => name in attributes),
    ...Object.keys(attributes).filter(
      (name) => !ORDER.includes(name as (typeof ORDER)[number]),
    ),
  ];
  if (names.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-sm text-primary/40">Atributos</div>
      <div className="flex flex-col gap-1 text-sm">
        {names.map((name) => {
          const entry = attributes[name];
          const label = LABELS[name] ?? name;
          const percent = Math.round(entry.score * 100);
          const showValue = !(BOOLEAN_FIELDS.has(name) && entry.value === name);
          return (
            <div key={name}>
              <div className="flex items-center gap-1.5">
                {COLOR_FIELDS.has(name) ? (
                  <ClothingColorChip name={entry.value} />
                ) : null}
                <span>
                  {label}
                  {showValue ? ` ${entry.value}` : ""} {percent}%
                </span>
              </div>
              {entry.uncertain && entry.also?.length ? (
                <div className="text-xs text-primary/40">
                  también {entry.also.join(", ")}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
