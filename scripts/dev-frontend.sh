#!/usr/bin/env bash
# Hot-reload the DeepFrigate Frigate UI without rebuilding the Frigate image.
# Edits under services/frigate/web/*.tsx are picked up by Vite.
set -euo pipefail

if [[ -z "${UPSTREAM:-}" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  UPSTREAM="${ROOT}/frigate/web"
  PATCHES="${ROOT}/services/frigate"
  WEB_DEV="${ROOT}/.web-dev"
  DEEPFRIGATE_WEB_SRC="${PATCHES}/web"
fi

PROXY_HOST="${PROXY_HOST:-127.0.0.1:${WEB_PORT:-3002}}"
PORT="${WEB_DEV_PORT:-5173}"
DEEPFRIGATE_WEB_SRC="${DEEPFRIGATE_WEB_SRC:-${PATCHES}/web}"

if [[ ! -d "${UPSTREAM}/src" ]]; then
  echo "Frigate web source not found at ${UPSTREAM}" >&2
  exit 1
fi

mkdir -p "${WEB_DEV}"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude node_modules --exclude dist "${UPSTREAM}/" "${WEB_DEV}/"
else
  find "${WEB_DEV}" -mindepth 1 -maxdepth 1 \
    ! -name node_modules ! -name dist -exec rm -rf {} +
  cp -a "${UPSTREAM}/." "${WEB_DEV}/"
  rm -rf "${WEB_DEV}/dist"
fi

mkdir -p \
  "${WEB_DEV}/src/pages" \
  "${WEB_DEV}/src/views/settings" \
  "${WEB_DEV}/src/views/search" \
  "${WEB_DEV}/src/components/overlay/detail"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigate.tsx" \
  "${WEB_DEV}/src/pages/DeepFrigate.tsx"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigatePersonAttributes.tsx" \
  "${WEB_DEV}/src/components/overlay/detail/DeepFrigatePersonAttributes.tsx"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigateClothingColor.tsx" \
  "${WEB_DEV}/src/components/overlay/detail/DeepFrigateClothingColor.tsx"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigateModelsSettingsView.tsx" \
  "${WEB_DEV}/src/views/settings/DeepFrigateModelsSettingsView.tsx"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigateWorkflowSettingsView.tsx" \
  "${WEB_DEV}/src/views/settings/DeepFrigateWorkflowSettingsView.tsx"
ln -sfn "${DEEPFRIGATE_WEB_SRC}/DeepFrigateVisualSearch.tsx" \
  "${WEB_DEV}/src/views/search/DeepFrigateVisualSearch.tsx"

WEB_DEV="${WEB_DEV}" node --input-type=commonjs - <<'JS'
const { readFileSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");
const path = join(process.env.WEB_DEV, "vite.config.ts");
const text = readFileSync(path, "utf8");
if (text.includes("DEEPFRIGATE_WEB_SRC")) process.exit(0);
const needle = "  server: {\n    proxy: {";
const insert = `  server: {
    fs: {
      allow: [
        path.resolve(__dirname),
        process.env.DEEPFRIGATE_WEB_SRC || path.resolve(__dirname),
      ],
    },
    proxy: {`;
if (!text.includes(needle)) {
  console.error("Unsupported vite.config.ts layout");
  process.exit(1);
}
writeFileSync(path, text.replace(needle, insert));
JS

export WEB_ROOT="${WEB_DEV}"
node "${PATCHES}/patch_web.mjs"

cd "${WEB_DEV}"
if [[ ! -x node_modules/.bin/vite ]]; then
  npm ci
fi

export PROXY_HOST DEEPFRIGATE_WEB_SRC
echo "DeepFrigate UI dev server: http://127.0.0.1:${PORT} (API proxy -> ${PROXY_HOST})"
exec npm run dev -- --host --port "${PORT}" --strictPort
