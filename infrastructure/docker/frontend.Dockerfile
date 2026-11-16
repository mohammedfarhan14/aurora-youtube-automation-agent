# syntax=docker/dockerfile:1
FROM node:24-alpine AS build

WORKDIR /workspace
RUN corepack enable && corepack prepare pnpm@12.18.0 --activate

COPY frontend/web/package.json frontend/web/pnpm-lock.yaml frontend/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/web/ ./
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN pnpm build

FROM ghcr.io/nginx/nginx-unprivileged:1.30.6-alpine3.24@sha256:c3fc8fa482be50ef709c6438eb1bc98d50dbfe5a9b48be90711b8814f4dfd701
COPY infrastructure/docker/frontend.nginx.conf /etc/nginx/conf.d/default.conf
COPY infrastructure/docker/frontend.security-headers.conf /etc/nginx/conf.d/aurora-security-headers.inc
COPY infrastructure/docker/frontend.api-proxy.conf /etc/nginx/conf.d/aurora-api-proxy.inc
COPY --from=build /workspace/dist /usr/share/nginx/html
EXPOSE 8080
