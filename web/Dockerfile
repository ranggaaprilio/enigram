# Static build of the UI, served by nginx, which also proxies /api to the FastAPI service.
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
# The image's entrypoint renders templates/*.template into conf.d with envsubst (ENIGRAM_WEB_KEY).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
# Exposes the container's DNS server to the template as NGINX_LOCAL_RESOLVERS.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY --from=build /web/dist /usr/share/nginx/html
