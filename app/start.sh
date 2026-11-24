#!/bin/sh
set -eu
flask --app app seed-demo
(
    while :; do
        flask --app app purge || echo 'Retention cleanup will retry.' >&2
        sleep 30
    done
) &
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app
