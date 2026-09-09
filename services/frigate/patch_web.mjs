import { readFileSync, writeFileSync } from "node:fs";

const webRoot = process.env.WEB_ROOT || "/src";
const root = `${webRoot}/src`;

function replaceOnce(path, oldValue, newValue) {
  const text = readFileSync(path, "utf8");
  if (text.includes(newValue)) return;
  if (!text.includes(oldValue)) {
    throw new Error(`Unsupported upstream layout in ${path}`);
  }
  writeFileSync(path, text.replace(oldValue, newValue));
}


function replaceAll(path, oldValue, newValue) {
  const text = readFileSync(path, "utf8");
  if (!text.includes(oldValue)) {
    if (text.includes(newValue)) return;
    throw new Error(`Unsupported upstream layout in ${path}`);
  }
  writeFileSync(path, text.split(oldValue).join(newValue));
}

replaceOnce(
  `${root}/App.tsx`,
  'const Events = lazy(() => import("@/pages/Events"));\n',
  'const Events = lazy(() => import("@/pages/Events"));\n' +
    'const DeepFrigate = lazy(() => import("@/pages/DeepFrigate"));\n',
);
replaceOnce(
  `${root}/App.tsx`,
  '              <Route path="/review" element={<Events />} />\n',
  '              <Route path="/review" element={<Events />} />\n' +
    '              <Route path="/deepfrigate" element={<DeepFrigate />} />\n',
);

replaceOnce(
  `${root}/types/search.ts`,
  `  limit?: number;\n`,
  `  limit?: number;\n  offset?: number;\n`,
);

const explore = `${root}/pages/Explore.tsx`;
replaceOnce(
  explore,
  'import SearchView from "@/views/search/SearchView";\n',
  'import SearchView from "@/views/search/SearchView";\n' +
    'import DeepFrigateVisualSearch from "@/views/search/DeepFrigateVisualSearch";\n',
);
replaceOnce(
  explore,
  "export default function Explore() {\n",
  `export default function Explore() {
  const deepSearchParams = new URLSearchParams(window.location.search);
  const deepObjectId = deepSearchParams.get(
    "deep_object_id",
  );
  const deepSimilarityEventId =
    deepSearchParams.get("search_type") === "deep" ||
    deepSearchParams.get("deep_search") === "1"
      ? deepSearchParams.get("event_id")
      : null;
`,
);
replaceOnce(
  explore,
  `  const searchQuery: SearchQuery = useMemo(() => {
    // no search parameters
`,
  `  const searchQuery: SearchQuery = useMemo(() => {
    if (deepSimilarityEventId) {
      return [
        \`deepfrigate/v1/frigate-events/\${encodeURIComponent(
          deepSimilarityEventId,
        )}/similar\`,
        { limit: 25 },
      ];
    }

    // no search parameters
`,
);
replaceOnce(
  explore,
  `  const similaritySearch = useMemo(
    () => searchSearchParams["search_type"] == "similarity",
    [searchSearchParams],
  );`,
  `  const similaritySearch = useMemo(
    () =>
      searchSearchParams["search_type"] == "similarity" ||
      searchSearchParams["search_type"] == "deep",
    [searchSearchParams],
  );`,
);
replaceOnce(
  explore,
  `    if (pageIndex > 0 && previousPageData) {
      const lastDate = previousPageData[previousPageData.length - 1].start_time;
      return [
        url,
        {
          ...params,
          [isAscending ? "after" : "before"]: lastDate.toString(),
          limit: API_LIMIT,
        },
      ];
    }`,
  `    if (pageIndex > 0 && previousPageData) {
      if (url === "events/search" || url.startsWith("deepfrigate/v1/frigate-events/")) {
        const lastResult = previousPageData[previousPageData.length - 1] as {
          deepfrigate_next_offset?: number;
        };
        return [
          url,
          {
            ...params,
            limit: API_LIMIT,
            offset:
              lastResult.deepfrigate_next_offset ?? pageIndex * API_LIMIT,
          },
        ];
      }
      const lastDate = previousPageData[previousPageData.length - 1].start_time;
      return [
        url,
        {
          ...params,
          [isAscending ? "after" : "before"]: lastDate.toString(),
          limit: API_LIMIT,
        },
      ];
    }`,
);
replaceOnce(
  explore,
  `      ) : (
        <SearchView
`,
  `      ) : deepObjectId ? (
        <DeepFrigateVisualSearch />
      ) : (
        <SearchView
`,
);

const searchResultActions = `${root}/components/menu/SearchResultActions.tsx`;
replaceOnce(
  searchResultActions,
  `      {config?.semantic_search?.enabled &&
        searchResult.data.type == "object" && (
          <MenuItem
            aria-label={t("itemMenu.findSimilar.aria")}
            onClick={findSimilar}
          >
            <span>{t("itemMenu.findSimilar.label")}</span>
          </MenuItem>
        )}`,
  `      {searchResult.data.type == "object" && (
          <MenuItem
            aria-label={t("itemMenu.findSimilar.aria")}
            onClick={() => {
              navigate(
                \`/explore?search_type=deep&event_id=\${encodeURIComponent(
                  searchResult.id,
                )}\`,
              );
            }}
          >
            <span>{t("itemMenu.findSimilar.label")}</span>
          </MenuItem>
        )}`,
);
replaceOnce(
  searchResultActions,
  `  const { data: config } = useSWR<FrigateConfig>("config");\n`,
  `  const { data: config } = useSWR<FrigateConfig>("config");\n` +
    "  void findSimilar;\n",
);

const detailActionsMenu = `${root}/components/overlay/detail/DetailActionsMenu.tsx`;
replaceOnce(
  detailActionsMenu,
  `  const hasSemanticSearchOption =
    config?.semantic_search.enabled &&
    setSimilarity !== undefined &&
    search.data?.type === "object";`,
  `  const hasSemanticSearchOption = search.data?.type === "object";`,
);
replaceOnce(
  detailActionsMenu,
  `          {config?.semantic_search.enabled &&
            setSimilarity != undefined &&
            search.data?.type == "object" && (
              <DropdownMenuItem
                onClick={() => {
                  setIsOpen(false);
                  setTimeout(() => {
                    setSearch?.(undefined);
                    setSimilarity?.();
                  }, 0);
                }}
              >
                <div className="flex cursor-pointer items-center gap-2">
                  <span>{t("itemMenu.findSimilar.label")}</span>
                </div>
              </DropdownMenuItem>
            )}`,
  `          {search.data?.type == "object" && (
              <DropdownMenuItem
                onClick={() => {
                  setIsOpen(false);
                  navigate(
                    \`/explore?search_type=deep&event_id=\${encodeURIComponent(
                      search.id,
                    )}\`,
                  );
                }}
              >
                <div className="flex cursor-pointer items-center gap-2">
                  <span>{t("itemMenu.findSimilar.label")}</span>
                </div>
              </DropdownMenuItem>
            )}`,
);
replaceOnce(
  detailActionsMenu,
  `  const navigate = useNavigate();\n`,
  `  const navigate = useNavigate();\n` +
    "  void setSearch;\n" +
    "  void setSimilarity;\n",
);

const navigation = `${root}/hooks/use-navigation.ts`;
replaceOnce(
  navigation,
  'import { LuConstruction } from "react-icons/lu";\n',
  'import { LuConstruction, LuCpu } from "react-icons/lu";\n',
);
replaceOnce(
  navigation,
  "export const ID_CHAT = 8;\n",
  "export const ID_CHAT = 8;\nexport const ID_DEEPFRIGATE = 9;\n",
);
replaceOnce(
  navigation,
  `        {
          id: ID_EXPLORE,
`,
  `        {
          id: ID_DEEPFRIGATE,
          variant,
          icon: LuCpu,
          title: "DeepFrigate",
          url: "/deepfrigate",
        },
        {
          id: ID_EXPLORE,
`,
);

const searchDetailDialog = `${root}/components/overlay/detail/SearchDetailDialog.tsx`;
replaceOnce(
  searchDetailDialog,
  'import { Event } from "@/types/event";\n',
  'import { Event } from "@/types/event";\n' +
    'import DeepFrigatePersonAttributes from "@/components/overlay/detail/DeepFrigatePersonAttributes";\n' +
    'import DeepFrigateSnapshotColorSwatches, { readClothingColors } from "@/components/overlay/detail/DeepFrigateClothingColor";\n',
);
replaceOnce(
  searchDetailDialog,
  `                    <div className="text-sm">{formattedDate}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          {search?.data.recognized_license_plate && (`,
  `                    <div className="text-sm">{formattedDate}</div>
                  </div>
                  <DeepFrigatePersonAttributes search={search} />
                </div>
              </div>
            </div>
          </div>
          {search?.data.recognized_license_plate && (`,
);

replaceOnce(
  searchDetailDialog,
  `              {search?.id && (
                <div className="relative mx-auto flex h-full">
                  <img
                    ref={imgRef}
                    className="mx-auto max-h-[60dvh] rounded-lg bg-background object-contain"
                    src={\`\${baseUrl}api/events/\${search?.id}/snapshot.jpg?crop=0&bbox=1&timestamp=0\`}
                    alt={\`\${search?.label}\`}
                    loading={isSafari ? "eager" : "lazy"}
                    onLoad={() => {
                      onImgLoad();
                    }}
                  />
                </div>
              )}`,
  `              {search?.id && (
                <div className="relative mx-auto flex h-full">
                  <div className="relative mx-auto">
                    <img
                      ref={imgRef}
                      className="mx-auto max-h-[60dvh] rounded-lg bg-background object-contain"
                      src={\`\${baseUrl}api/events/\${search?.id}/snapshot.jpg?crop=0&bbox=1&timestamp=0\`}
                      alt={\`\${search?.label}\`}
                      loading={isSafari ? "eager" : "lazy"}
                      onLoad={() => {
                        onImgLoad();
                      }}
                    />
                    <DeepFrigateSnapshotColorSwatches
                      box={search.data?.box}
                      {...readClothingColors(search.data)}
                    />
                  </div>
                </div>
              )}`,
);

const objectTrackOverlay = `${root}/components/overlay/ObjectTrackOverlay.tsx`;
replaceOnce(
  objectTrackOverlay,
  'import { Event } from "@/types/event";\n',
  'import { Event } from "@/types/event";\n' +
    'import {\n' +
    '  DeepFrigateBoxColorSwatchesSvg,\n' +
    '  readClothingColors,\n' +
    '} from "@/components/overlay/detail/DeepFrigateClothingColor";\n',
);
replaceOnce(
  objectTrackOverlay,
  `  currentBox?: number[];
  currentAttributeBox?: number[];
};`,
  `  currentBox?: number[];
  currentAttributeBox?: number[];
  upperColor?: string;
  lowerColor?: string;
};`,
);
replaceOnce(
  objectTrackOverlay,
  `        return {
          objectId,
          label,
          color,
          pathPoints: combinedPoints,
          currentZones,
          currentBox,
          currentAttributeBox,
        };`,
  `        const clothing = readClothingColors(eventData?.data);
        return {
          objectId,
          label,
          color,
          pathPoints: combinedPoints,
          currentZones,
          currentBox,
          currentAttributeBox,
          upperColor: clothing.upper,
          lowerColor: clothing.lower,
        };`,
);
replaceOnce(
  objectTrackOverlay,
  `                  opacity="1"
                />
              </g>
            )}
            {objData.currentAttributeBox && showBoundingBoxes && (`,
  `                  opacity="1"
                />
                <DeepFrigateBoxColorSwatchesSvg
                  box={objData.currentBox}
                  videoWidth={videoWidth}
                  videoHeight={videoHeight}
                  upper={objData.upperColor}
                  lower={objData.lowerColor}
                />
              </g>
            )}
            {objData.currentAttributeBox && showBoundingBoxes && (`,
);

const settings = `${root}/pages/Settings.tsx`;
replaceOnce(
  settings,
  'import DetectorsAndModelSettingsView from "@/views/settings/DetectorsAndModelSettingsView";\n',
  'import DetectorsAndModelSettingsView from "@/views/settings/DetectorsAndModelSettingsView";\n' +
    'import DeepFrigateModelsSettingsView from "@/views/settings/DeepFrigateModelsSettingsView";\n' +
    'import DeepFrigateWorkflowSettingsView from "@/views/settings/DeepFrigateWorkflowSettingsView";\n',
);
replaceOnce(
  settings,
  '  "systemDetectorsAndModel",\n',
  '  "systemDetectorsAndModel",\n  "deepFrigateModels",\n  "deepFrigateWorkflow",\n',
);
replaceOnce(
  settings,
  `  {
    label: "system",
`,
  `  {
    label: "aiModels",
    items: [
      {
        key: "deepFrigateModels",
        component: DeepFrigateModelsSettingsView,
      },
      {
        key: "deepFrigateWorkflow",
        component: DeepFrigateWorkflowSettingsView,
      },
    ],
  },
  {
    label: "system",
`,
);
replaceOnce(
  settings,
  'const ALLOWED_VIEWS_FOR_VIEWER = ["uiSettings", "notifications"];\n',
  `const ALLOWED_VIEWS_FOR_VIEWER = [
  "uiSettings",
  "deepFrigateModels",
  "deepFrigateWorkflow",
  "notifications",
];
`,
);

for (const [locale, groupLabel, itemLabel, workflowLabel] of [
  ["en", "DeepFrigate", "AI Models", "Visual Workflow"],
  ["es", "DeepFrigate", "Modelos de IA", "Workflow visual"],
]) {
  const path = `${webRoot}/public/locales/${locale}/views/settings.json`;
  const translations = JSON.parse(readFileSync(path, "utf8"));
  translations.menu.aiModels = groupLabel;
  translations.menu.deepFrigateModels = itemLabel;
  translations.menu.deepFrigateWorkflow = workflowLabel;
  writeFileSync(path, `${JSON.stringify(translations, null, 2)}\n`);
}

// ---------------------------------------------------------------------------
// Obsidiana Táctica (design system). Tokens: themes/theme-default.css (copied
// over upstream's), utilities: src/obsidiana.css, chart colors:
// src/lib/hooks/useChartColors.ts. Fonts: Archivo + Geist Mono (fontsource,
// bundled, offline).
// ---------------------------------------------------------------------------
replaceOnce(
  `${root}/index.css`,
  '@import "/themes/tailwind-base.css";\n',
  '@import "@fontsource-variable/archivo";\n' +
    '@import "@fontsource-variable/geist-mono";\n' +
    '@import "./obsidiana.css";\n' +
    '@import "/themes/tailwind-base.css";\n',
);

const tailwind = `${webRoot}/tailwind.config.cjs`;
replaceOnce(
  tailwind,
  `    fontFamily: {
      sans: ['"Inter"', "sans-serif"],
      mono: [
        "ui-monospace",
        "SFMono-Regular",
        "Menlo",
        "Monaco",
        "Consolas",
        '"Liberation Mono"',
        '"Courier New"',
        "monospace",
      ],
    },
`,
  `    fontFamily: {
      sans: ['"Archivo Variable"', "Archivo", "system-ui", "sans-serif"],
      mono: ['"Geist Mono Variable"', '"Geist Mono"', "ui-monospace", "monospace"],
    },
    // Obsidiana: radios 2/4 px (full se conserva para puntos y avatares).
    borderRadius: {
      none: "0px",
      sm: "2px",
      DEFAULT: "2px",
      md: "4px",
      lg: "4px",
      xl: "4px",
      "2xl": "4px",
      "3xl": "4px",
      full: "9999px",
    },
    // Obsidiana: sin glow. Elevación baja = hairline; overlays = una sombra.
    boxShadow: {
      none: "none",
      sm: "0 0 0 1px hsl(var(--border))",
      DEFAULT: "0 0 0 1px hsl(var(--border))",
      md: "0 0 0 1px hsl(var(--border))",
      lg: "var(--df-shadow-overlay)",
      xl: "var(--df-shadow-overlay)",
      "2xl": "var(--df-shadow-overlay)",
      inner: "none",
      overlay: "var(--df-shadow-overlay)",
      "search-console": "var(--df-shadow-search-console)",
    },
`,
);
replaceOnce(
  tailwind,
  `    extend: {
      animation: {
`,
  `    extend: {
      fontSize: {
        "2xs": ["10px", { lineHeight: "1.2" }],
        xs: ["11px", { lineHeight: "1.35" }],
        sm: ["12.5px", { lineHeight: "1.4" }],
        base: ["14px", { lineHeight: "1.45" }],
        lg: ["18px", { lineHeight: "1.3" }],
        display: ["28px", { lineHeight: "1" }],
      },
      transitionTimingFunction: {
        inst: "var(--df-ease-inst)",
      },
      animation: {
`,
);
replaceOnce(
  tailwind,
  `        danger: "#ef4444",
        success: "#22c55e",
        unsaved: "#f59e0b",
`,
  `        danger: "hsl(var(--crit))",
        success: "hsl(var(--ok))",
        unsaved: "hsl(var(--warn))",
`,
);

// Charts: hex only inside useChartColors (SVG attributes).
const graphs = `${root}/components/graph`;
const hookImport = 'import { useChartColors } from "@/lib/hooks/useChartColors";\n';
const useThemeLine = "  const { theme, systemTheme } = useTheme();\n";
const withColors = useThemeLine + "  const chartColors = useChartColors();\n";

replaceOnce(
  `${graphs}/LineGraph.tsx`,
  'const GRAPH_COLORS = ["#5C7CFA", "#ED5CFA", "#FAD75C"];\n',
  hookImport,
);
replaceAll(`${graphs}/LineGraph.tsx`, useThemeLine, withColors);
replaceAll(`${graphs}/LineGraph.tsx`, "      colors: GRAPH_COLORS,\n", "      colors: chartColors.series,\n");
replaceAll(`${graphs}/LineGraph.tsx`, "GRAPH_COLORS[labelIdx]", "chartColors.series[labelIdx]");
replaceAll(`${graphs}/LineGraph.tsx`, '            colors: "#6B6B6B",\n', "            colors: chartColors.axis,\n");

replaceOnce(
  `${graphs}/SystemGraph.tsx`,
  'import { useTheme } from "@/context/theme-provider";\n',
  'import { useTheme } from "@/context/theme-provider";\n' + hookImport,
);
replaceAll(`${graphs}/SystemGraph.tsx`, useThemeLine, withColors);
replaceOnce(`${graphs}/SystemGraph.tsx`, '            return "#FA5252";\n', "            return chartColors.crit;\n");
replaceOnce(`${graphs}/SystemGraph.tsx`, '            return "#FF9966";\n', "            return chartColors.warn;\n");
replaceOnce(`${graphs}/SystemGraph.tsx`, '            return "#217930";\n', "            return chartColors.ok;\n");
replaceAll(`${graphs}/SystemGraph.tsx`, '            colors: "#6B6B6B",\n', "            colors: chartColors.axis,\n");

for (const file of ["StorageGraph.tsx", "CombinedStorageGraph.tsx"]) {
  replaceOnce(
    `${graphs}/${file}`,
    'import { useTheme } from "@/context/theme-provider";\n',
    'import { useTheme } from "@/context/theme-provider";\n' + hookImport,
  );
  replaceAll(`${graphs}/${file}`, useThemeLine, withColors);
  replaceAll(
    `${graphs}/${file}`,
    '(systemTheme || theme) == "dark" ? "#404040" : "#E5E5E5"',
    "chartColors.edge",
  );
}
// Only the combined graph has the "Other" slice.
replaceAll(
  `${graphs}/CombinedStorageGraph.tsx`,
  '(systemTheme || theme) == "dark" ? "#606060" : "#D5D5D5"',
  "chartColors.bar",
);
