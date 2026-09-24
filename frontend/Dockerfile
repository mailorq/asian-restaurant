# syntax=docker/dockerfile:1

# deps: install once, reused by other stages
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

# dev: vite dev server (hmr)
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS dev
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# build: static assets
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# prod: nginx serves the spa and proxies the api
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS prod
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/proxy_params.conf /etc/nginx/proxy_params.conf
COPY nginx/security_headers.conf /etc/nginx/security_headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
