#!/bin/sh
set -eu
cert_dir=/etc/nginx/local-tls
host="${PORTAL_SERVER_NAME:-localhost}"
mkdir -p "$cert_dir"
# cert.pem·key.pem이 이미 있으면(공인 인증서를 넣어 둔 경우 포함) 만들지 않는다.
if [ ! -s "$cert_dir/cert.pem" ] || [ ! -s "$cert_dir/key.pem" ]; then
    san='DNS:localhost,IP:127.0.0.1'
    [ "$host" = localhost ] || san="DNS:$host,$san"
    umask 077
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -subj "/CN=$host" -addext "subjectAltName=$san" \
        -keyout "$cert_dir/key.pem" -out "$cert_dir/cert.pem"
fi
