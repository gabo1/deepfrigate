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
        { limit: 24 },
      ];
    }

    // no search parameters
`,
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
              if (!config?.semantic_search?.enabled) {
                navigate(
                  \`/explore?search_type=similarity&event_id=\${encodeURIComponent(
                    searchResult.id,
                  )}&deep_search=1\`,
                );
                return;
              }
              findSimilar();
            }}
          >
            <span>{t("itemMenu.findSimilar.label")}</span>
          </MenuItem>
        )}`,
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
                  if (!config?.semantic_search.enabled) {
                    navigate(
                      \`/explore?search_type=similarity&event_id=\${encodeURIComponent(
                        search.id,
                      )}&deep_search=1\`,
                    );
                    return;
                  }
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
