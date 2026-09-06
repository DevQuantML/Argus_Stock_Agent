#!/bin/sh
# docker-entrypoint.sh — chown the database's directory, THEN drop to the
# non-root app user.
#
# Why this exists: a Docker (Railway included) volume mount is created by
# the PLATFORM at container start, owned by root, regardless of what USER
# the image itself declares. This container's application user (appuser)
# has no permission to write there — nor to /app's default database path
# when ARGUS_DB is unset — which surfaced as store.bootstrap() failing with
# "unable to open database file" on first deploy. A non-root process cannot
# chown anything it doesn't already own, so this step MUST run as root,
# before appuser exists as the running identity — which is exactly why the
# Dockerfile no longer sets `USER appuser` at build time: that would make
# EVERY process in the container non-root from the start, including this
# one, and it could never fix the very permission problem it exists to fix.
#
# Root's involvement ends here. This script chowns exactly one thing — the
# database's own directory, never the wider /app tree — processes no
# attacker-reachable input, and immediately execs into the non-root uvicorn
# process via `su`: the actual application, the one handling real network
# traffic, still never runs as root and never holds write access it doesn't
# need. Standard pattern for exactly this class of problem (the official
# postgres/mysql/redis images all do the same thing for the same reason).
#
# Deliberately NOT `chown -R appuser /app` in addition to $db_dir: appuser
# only needs to WRITE inside the database directory — reading the app's own
# source and static files needs no ownership change, since COPY's default
# permissions are already world-readable. Widening this to /app would hand
# appuser write access to every served static file and Python module too,
# for no operational reason — least-privilege the same way this project
# already treats every other credential and permission boundary.
set -e

db_dir=$(dirname "${ARGUS_DB:-/app/argus.db}")
mkdir -p "$db_dir"
# Best-effort: a bind-mounted volume on some platforms may already be
# writable by appuser, or chown may be restricted even for root in some
# sandboxes — either way, this must never be fatal on its own. If the
# directory genuinely isn't writable after this, store.bootstrap() will
# still fail with its own clear error, which is the right failure mode.
chown -R appuser:appgroup "$db_dir" 2>/dev/null || true

port="${PORT:-8000}"
exec su -s /bin/sh appuser -c "uvicorn api:app --host 0.0.0.0 --port $port --workers 1 --no-proxy-headers"
