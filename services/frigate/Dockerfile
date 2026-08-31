ARG FRIGATE_IMAGE=ghcr.io/blakeblackshear/frigate:0.18.0-beta3-tensorrt

FROM node:20-bookworm-slim AS web-build

WORKDIR /src
ENV NODE_OPTIONS=--max-old-space-size=3072
COPY frigate/web/package.json frigate/web/package-lock.json ./
COPY frigate/web/patches ./patches
RUN npm ci
COPY frigate/web/ ./
COPY services/frigate/web/DeepFrigate.tsx ./src/pages/DeepFrigate.tsx
COPY services/frigate/web/DeepFrigateModelsSettingsView.tsx ./src/views/settings/DeepFrigateModelsSettingsView.tsx
COPY services/frigate/web/DeepFrigateWorkflowSettingsView.tsx ./src/views/settings/DeepFrigateWorkflowSettingsView.tsx
COPY services/frigate/web/DeepFrigateVisualSearch.tsx ./src/views/search/DeepFrigateVisualSearch.tsx
COPY services/frigate/web/DeepFrigatePersonAttributes.tsx ./src/components/overlay/detail/DeepFrigatePersonAttributes.tsx
COPY services/frigate/web/DeepFrigateClothingColor.tsx ./src/components/overlay/detail/DeepFrigateClothingColor.tsx
COPY services/frigate/patch_web.mjs /tmp/patch_web.mjs
RUN node /tmp/patch_web.mjs && npm run build

FROM ${FRIGATE_IMAGE}

COPY services/frigate/patch_nginx.py /tmp/patch_deepfrigate_nginx.py
RUN python3 /tmp/patch_deepfrigate_nginx.py \
    && rm /tmp/patch_deepfrigate_nginx.py
COPY --from=web-build /src/dist /opt/frigate/web
