FROM nginx:stable-alpine
RUN apk add --no-cache openssl
# Compose가 값을 넘기지 않아도 예전과 같은 로컬 설정으로 뜬다.
ENV PORTAL_SERVER_NAME=localhost
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 15-local-cert.sh /docker-entrypoint.d/15-local-cert.sh
