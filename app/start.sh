#!/bin/sh
set -eu
flask --app app init-admin
flask --app app seed-demo
(
    while :; do
        flask --app app maintain || echo 'Desk maintenance will restart.' >&2
        sleep 4
    done
) &
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app
