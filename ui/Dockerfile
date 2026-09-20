# A minimal image that just serves the already-built React (Vite) bundle
# through nginx and forwards API calls to the backend. Build and push it to
# your registry like any other container image.
#
# React is compiled outside of Docker so this image stays small and has no
# build-time dependencies. Build the bundle first, then build this image:
#     cd ui && npm install && npm run build   # -> ui/dist/
#     docker build -t chatdemo-ui ./ui
FROM nginx:alpine

# Where API calls get forwarded — override per environment (e.g. an ECS/k8s service URL).
ENV BACKEND_URL=http://backend:8000
# DNS used to resolve BACKEND_URL at request time (not baked in at startup).
# 127.0.0.11 is Docker's built-in resolver; swap in your cluster's DNS for k8s/ECS
# (ECS awsvpc typically uses 169.254.169.253).
ENV DNS_RESOLVER=127.0.0.11

# nginx:alpine expands *.template files with envsubst on container start, so
# ${BACKEND_URL} above gets substituted into the real nginx config here.
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY dist/ /usr/share/nginx/html/

EXPOSE 80
