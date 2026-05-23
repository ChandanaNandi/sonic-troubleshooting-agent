#!/usr/bin/env bash
# Bring sonic-vs-troubleshoot into an operational SONiC state.
#
# Why this script exists: docker-sonic-vs-fixed's image entrypoint is
# /usr/local/bin/supervisord, which auto-starts the operational SONiC
# service stack (redis-server, syncd, orchagent, portsyncd, all
# *mgrd/*syncd processes, and FRR zebra/mgmtd/staticd). A handful of
# programs are STOPPED by design (arp_update, restore_neighbors,
# gbsyncd, gearsyncd, pathd, redis-chassis). bgpd is configured with
# autostart=false in supervisord, so the image leaves it STOPPED. This
# script starts the container with the default entrypoint, waits until
# core services are ready and CONFIG_DB is populated, then ensures bgpd
# is RUNNING.
#
# Idempotent: a pre-existing container with the same name is removed
# and recreated, so re-running this returns the container to a known
# clean state.
#
# Usage:
#   ./scripts/bringup.sh
#   CONTAINER=other-name ./scripts/bringup.sh
set -euo pipefail

CONTAINER="${CONTAINER:-sonic-vs-troubleshoot}"
IMAGE="${IMAGE:-docker-sonic-vs-fixed:latest}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-120}"

log() { printf '[bringup] %s\n' "$*" >&2; }

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    log "removing existing container: $CONTAINER"
    docker rm -f "$CONTAINER" >/dev/null
fi

log "starting $CONTAINER from $IMAGE with default supervisord entrypoint"
# Docker prints an amd64-on-arm warning on Apple Silicon (SONiC VS is
# amd64-only). The warning is harmless: the runtime readiness gates
# below (redis ping, CONFIG_DB populated, bgpd RUNNING) prove the
# container is actually usable regardless.
docker run -dit \
    --name "$CONTAINER" \
    --privileged \
    --security-opt label=disable \
    "$IMAGE" >/dev/null

log "waiting up to ${READY_TIMEOUT_SECONDS}s for redis-server to respond"
deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
    if docker exec "$CONTAINER" redis-cli ping 2>/dev/null | grep -qx PONG; then
        log "redis-server ready"
        break
    fi
    sleep 1
done
if (( SECONDS >= deadline )); then
    log "ERROR: redis-server did not respond within ${READY_TIMEOUT_SECONDS}s"
    docker logs --tail 30 "$CONTAINER" >&2 || true
    exit 1
fi

log "waiting for start.sh to EXIT (signals SONiC boot orchestration complete)"
# supervisorctl status returns exit 3 when the queried program is not RUNNING
# (including the success-state EXITED that we want), so tolerate non-zero exit.
deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
    line=$(docker exec "$CONTAINER" supervisorctl status start.sh 2>/dev/null || true)
    if echo "$line" | grep -Eq '[[:space:]]EXITED[[:space:]]'; then
        log "start.sh has EXITED; SONiC services orchestration complete"
        break
    fi
    sleep 2
done
if (( SECONDS >= deadline )); then
    log "ERROR: start.sh did not reach EXITED within ${READY_TIMEOUT_SECONDS}s"
    docker exec "$CONTAINER" supervisorctl status >&2 || true
    exit 1
fi

log "verifying CONFIG_DB is populated (PORT|Ethernet4 must exist)"
# Redis can answer PONG before swss has finished loading port data into
# CONFIG_DB. Without this gate, Phase 1 fault injection could fail in
# confusing ways. Poll EXISTS until it returns 1.
deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
    exists=$(docker exec "$CONTAINER" redis-cli -n 4 EXISTS 'PORT|Ethernet4' 2>/dev/null || true)
    if [[ "$exists" == "1" ]]; then
        log "CONFIG_DB populated (PORT|Ethernet4 present)"
        break
    fi
    sleep 1
done
if (( SECONDS >= deadline )); then
    log "ERROR: CONFIG_DB not populated within ${READY_TIMEOUT_SECONDS}s (PORT|Ethernet4 missing)"
    exit 1
fi

log "ensuring bgpd is RUNNING (image default is autostart=false)"
# Idempotent: supervisorctl start on an already-RUNNING program fails
# with exit 1, which would kill the script under set -e. Check first,
# only start if needed.
bgpd_status=$(docker exec "$CONTAINER" supervisorctl status bgpd 2>/dev/null || true)
if ! echo "$bgpd_status" | grep -q RUNNING; then
    docker exec "$CONTAINER" supervisorctl start bgpd >/dev/null
    sleep 2
    bgpd_status=$(docker exec "$CONTAINER" supervisorctl status bgpd 2>/dev/null || true)
fi
if ! echo "$bgpd_status" | grep -q RUNNING; then
    log "ERROR: bgpd did not enter RUNNING: $bgpd_status"
    exit 1
fi
log "bgpd: $bgpd_status"

log "$CONTAINER is operational. supervisor summary:"
# supervisorctl status returns exit 3 when any program is STOPPED (expected:
# arp_update, restore_neighbors, gbsyncd, etc. are STOPPED by design), so
# tolerate non-zero exit here.
{ docker exec "$CONTAINER" supervisorctl status 2>/dev/null || true; } \
    | awk '{printf "    %-22s %s\n", $1, $2}' >&2
