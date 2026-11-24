#!/bin/sh
set -eu
cert_dir=/etc/nginx/local-tls
mkdir -p "$cert_dir"
if [ ! -s "$cert_dir/cert.pem" ] || [ ! -s "$cert_dir/key.pem" ]; then
    umask 077
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
        -keyout "$cert_dir/key.pem" -out "$cert_dir/cert.pem"
fi
