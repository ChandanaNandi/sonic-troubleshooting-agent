#!/usr/bin/env bash
#
# Set up / tear down the two-container BGP lab for Phase 2 BGP scenarios.
#
# Reference:
#   phase2/2B_DECISION.md           — the Option C architectural decision
#   phase2/2B_TOPOLOGY_FINDINGS.md  — the manual procedure this script
#                                     automates, with captured evidence
#
# Verbs:
#   up      Cold-start the lab. Create the network, start the peer,
#           enable bgpd on the peer, configure BGP on both sides, and
#           wait until the SUT sees the peer in Established state.
#   down    Remove SUT BGP config, disconnect SUT from the lab network,
#           remove the peer container, remove the network. Returns
#           sonic-vs-troubleshoot to a `{}` BGP state.
#   status  Report current state (network, peer, SUT peer state) and one
#           of three verdicts: "up and Established", "down (clean)", or
#           "partial state - run down then up".
#
# vtysh vs CONFIG_DB caveat:
#   This script configures the SUT side via vtysh, not via CONFIG_DB
#   BGP_NEIGHBOR + bgpcfgd. CONFIG_DB is the canonical SONiC path and
#   was the recommendation in phase2/2B_DECISION.md, but that path is
#   not yet validated for this project. vtysh works (proven by
#   phase2/2B_TOPOLOGY_FINDINGS.md). If Phase 2C scenarios need
#   CONFIG_DB-mediated state — for example, fault scripts that mutate
#   BGP_NEIGHBOR through bgpcfgd — this script's SUT configuration will
#   need to be revisited and validated against bgpcfgd behavior.
#
# Architectural rule:
#   The peer container (sonic-bgp-peer) is a test fixture, NOT part of
#   the SONiC system under test. The blackboard and diagnosis agent only
#   ever see evidence collected from sonic-vs-troubleshoot. This script
#   does not feed peer-side output anywhere; it only configures the
#   peer so the SUT has a real neighbor to talk to.
#
# Discovered process model (peer container):
#   The frrouting/frr image's PID 1 is /sbin/tini, which invokes
#   /usr/lib/frr/docker-start (the FRR project's container startup
#   script). docker-start launches watchfrr, which supervises the
#   daemons listed in /etc/frr/daemons. The image ships with bgpd=no.
#   To enable bgpd cleanly under watchfrr, this script edits the
#   daemons file to bgpd=yes and runs `docker restart`. The restart is
#   safe because PID 1 is tini (not watchfrr); tini handles SIGTERM
#   forwarding cleanly. After restart, watchfrr is observed running as
#   `watchfrr zebra bgpd staticd`. The script verifies bgpd is actually
#   running before configuring BGP and exits non-zero if it does not
#   appear within BGPD_READY_TIMEOUT_SECONDS.
#
# Partial state handling:
#   This script does NOT auto-clean partial state on `up`. If `up` is
#   called when partial state exists (network or peer present, or SUT
#   already has BGP config), it exits non-zero and tells the user to
#   run `down` first. This preserves useful failure state for
#   inspection rather than silently masking it.
set -euo pipefail

NETWORK="sonic-bgp-lab"
SUBNET="10.10.10.0/24"
PEER_CONTAINER="sonic-bgp-peer"
PEER_IMAGE="frrouting/frr:latest"
PEER_IP="10.10.10.2"
SUT_CONTAINER="sonic-vs-troubleshoot"
SUT_IP="10.10.10.3"
SUT_ASN="65000"
PEER_ASN="65001"
ESTABLISHED_TIMEOUT_SECONDS=60
BGPD_READY_TIMEOUT_SECONDS=30

log() { printf '[configure_bgp] %s\n' "$*" >&2; }

# Query the SUT for its BGP peer state for PEER_IP. Returns one of:
#   - "Established", "Active", "Idle", "Connect", "OpenSent",
#     "OpenConfirm" — the FRR BGP FSM state if PEER_IP is configured
#   - "none" — no peer entry under PEER_IP (router bgp exists but
#     does not have this neighbor)
#   - "noinstance" — `show bgp summary json` returned {} or failed
sut_peer_state() {
    local raw
    raw=$(docker exec "$SUT_CONTAINER" vtysh -c "show bgp summary json" 2>/dev/null || true)
    if [ -z "$raw" ] || [ "$raw" = "{}" ]; then
        echo "noinstance"
        return
    fi
    echo "$raw" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('noinstance'); sys.exit(0)
peer = d.get('ipv4Unicast', {}).get('peers', {}).get('$PEER_IP', {})
print(peer.get('state', 'none'))
" 2>/dev/null || echo "none"
}

network_exists() {
    docker network inspect "$NETWORK" >/dev/null 2>&1
}

peer_container_exists() {
    docker ps -a --filter "name=$PEER_CONTAINER" --format '{{.Names}}' \
        | grep -qx "$PEER_CONTAINER"
}

peer_container_running() {
    docker ps --filter "name=$PEER_CONTAINER" --format '{{.Names}}' \
        | grep -qx "$PEER_CONTAINER"
}

sut_on_lab_network() {
    docker network inspect "$NETWORK" \
        --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null \
        | grep -qw "$SUT_CONTAINER"
}

peer_bgpd_running() {
    docker exec "$PEER_CONTAINER" sh -c \
        'ps -ef | grep -q "[/]usr/lib/frr/bgpd"' 2>/dev/null
}

# True if the SUT's running-config contains a `router bgp $SUT_ASN`
# block. An EMPTY `router bgp` block (no neighbors configured) still
# returns `{}` from `show bgp summary json`, so sut_peer_state would
# report "noinstance" even though config is present. This helper
# detects that case so status/up/down do not mistake a leftover empty
# block for a clean state.
sut_bgp_config_present() {
    docker exec "$SUT_CONTAINER" vtysh -c "show running-config" 2>/dev/null \
        | grep -q "^router bgp $SUT_ASN"
}

peer_watchfrr_supervises_bgpd() {
    docker exec "$PEER_CONTAINER" sh -c \
        'ps -ef | grep "[w]atchfrr" | grep -q bgpd' 2>/dev/null
}

yesno() {
    if "$@"; then echo yes; else echo no; fi
}

verb_status() {
    local net peer sutstate sutbgp verdict
    net=$(network_exists && echo present || echo absent)
    if peer_container_exists; then
        if peer_container_running; then
            peer="running"
        else
            peer="stopped"
        fi
    else
        peer="absent"
    fi
    sutstate=$(sut_peer_state)
    sutbgp=$(yesno sut_bgp_config_present)

    echo "  network ($NETWORK):                $net"
    echo "  peer ($PEER_CONTAINER):            $peer"
    echo "  SUT peer state ($PEER_IP):         $sutstate"
    echo "  SUT BGP config (router bgp $SUT_ASN): $sutbgp"

    if [ "$net" = "present" ] && [ "$peer" = "running" ] && [ "$sutstate" = "Established" ]; then
        verdict="up and Established"
    elif [ "$net" = "absent" ] && [ "$peer" = "absent" ] && [ "$sutstate" = "noinstance" ] && [ "$sutbgp" = "no" ]; then
        verdict="down (clean)"
    else
        verdict="partial state - run down then up"
    fi
    echo "  verdict: $verdict"
}

verb_up() {
    # Idempotency: already fully up. Established implies config in
    # practice, but require sut_bgp_config_present explicitly so the
    # up-check uses the same clean/up-state definition as status and
    # down (observed peer state PLUS actual SUT config).
    if network_exists && peer_container_running \
        && sut_bgp_config_present \
        && [ "$(sut_peer_state)" = "Established" ]; then
        log "already up"
        exit 0
    fi

    # Partial-state detection. Do NOT auto-clean — surface the state
    # so the user can inspect it before tearing down. An empty
    # `router bgp $SUT_ASN` block also counts as partial state because
    # it would otherwise be invisible to `sut_peer_state` (which reads
    # only `show bgp summary json`).
    local sutstate
    sutstate=$(sut_peer_state)
    if network_exists || peer_container_exists || [ "$sutstate" != "noinstance" ] || sut_bgp_config_present; then
        log "ERROR: partial state detected. Run '$0 down' first."
        log "  network exists:        $(yesno network_exists)"
        log "  peer container exists: $(yesno peer_container_exists)"
        log "  SUT peer state:        $sutstate (expected noinstance)"
        log "  SUT BGP config present: $(yesno sut_bgp_config_present) (expected no)"
        exit 2
    fi

    # Clean start
    log "creating network $NETWORK ($SUBNET)"
    docker network create "$NETWORK" --subnet "$SUBNET" >/dev/null

    log "starting peer $PEER_CONTAINER from $PEER_IMAGE at $PEER_IP"
    docker run -d --name "$PEER_CONTAINER" \
        --network "$NETWORK" --ip "$PEER_IP" \
        --privileged "$PEER_IMAGE" >/dev/null

    log "enabling bgpd in peer's /etc/frr/daemons"
    docker exec "$PEER_CONTAINER" sh -c \
        "sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons"

    log "restarting peer so watchfrr picks up bgpd"
    docker restart "$PEER_CONTAINER" >/dev/null

    log "waiting up to ${BGPD_READY_TIMEOUT_SECONDS}s for peer bgpd to start"
    local deadline
    deadline=$(($(date +%s) + BGPD_READY_TIMEOUT_SECONDS))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if peer_bgpd_running; then
            break
        fi
        sleep 1
    done
    if ! peer_bgpd_running; then
        log "ERROR: peer bgpd did not start within ${BGPD_READY_TIMEOUT_SECONDS}s"
        docker exec "$PEER_CONTAINER" ps -ef >&2 || true
        log "  run '$0 down' to clean up, then '$0 up' to retry"
        exit 3
    fi
    if ! peer_watchfrr_supervises_bgpd; then
        log "ERROR: watchfrr is not supervising bgpd on peer"
        docker exec "$PEER_CONTAINER" ps -ef >&2 || true
        log "  run '$0 down' to clean up, then '$0 up' to retry"
        exit 3
    fi
    log "peer bgpd running, supervised by watchfrr"

    log "configuring peer BGP: router bgp $PEER_ASN, neighbor $SUT_IP remote-as $SUT_ASN"
    docker exec "$PEER_CONTAINER" vtysh \
        -c "configure terminal" \
        -c "router bgp $PEER_ASN" \
        -c "neighbor $SUT_IP remote-as $SUT_ASN" >/dev/null

    log "connecting SUT to $NETWORK at $SUT_IP"
    docker network connect "$NETWORK" --ip "$SUT_IP" "$SUT_CONTAINER"

    log "configuring SUT BGP via vtysh: router bgp $SUT_ASN, neighbor $PEER_IP remote-as $PEER_ASN"
    docker exec "$SUT_CONTAINER" vtysh \
        -c "configure terminal" \
        -c "router bgp $SUT_ASN" \
        -c "neighbor $PEER_IP remote-as $PEER_ASN" >/dev/null

    log "polling SUT BGP for Established (up to ${ESTABLISHED_TIMEOUT_SECONDS}s)"
    deadline=$(($(date +%s) + ESTABLISHED_TIMEOUT_SECONDS))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local state
        state=$(sut_peer_state)
        if [ "$state" = "Established" ]; then
            log "BGP session Established"
            exit 0
        fi
        sleep 2
    done
    log "ERROR: BGP did not reach Established within ${ESTABLISHED_TIMEOUT_SECONDS}s"
    log "  current SUT peer state: $(sut_peer_state)"
    log "  partial state remains. Run '$0 down' to clean up, then '$0 up' to retry."
    exit 4
}

verb_down() {
    # Idempotency: already fully down
    if ! network_exists && ! peer_container_exists \
        && [ "$(sut_peer_state)" = "noinstance" ] \
        && ! sut_bgp_config_present; then
        log "already down"
        exit 0
    fi

    # Remove SUT BGP config first so peer disconnect doesn't dangle a
    # session. Trigger on either peer-state present OR empty router bgp
    # block present; the latter is invisible to sut_peer_state.
    if [ "$(sut_peer_state)" != "noinstance" ] || sut_bgp_config_present; then
        log "removing SUT BGP config (no router bgp $SUT_ASN)"
        docker exec "$SUT_CONTAINER" vtysh \
            -c "configure terminal" \
            -c "no router bgp $SUT_ASN" >/dev/null
    fi

    # Disconnect SUT from lab network before removing the network
    if network_exists && sut_on_lab_network; then
        log "disconnecting SUT from $NETWORK"
        docker network disconnect "$NETWORK" "$SUT_CONTAINER"
    fi

    # Remove peer container (force, since it may be running)
    if peer_container_exists; then
        log "removing peer container $PEER_CONTAINER"
        docker rm -f "$PEER_CONTAINER" >/dev/null
    fi

    # Remove network
    if network_exists; then
        log "removing network $NETWORK"
        docker network rm "$NETWORK" >/dev/null
    fi

    # Verify clean
    local sutstate
    sutstate=$(sut_peer_state)
    if [ "$sutstate" != "noinstance" ] || network_exists || peer_container_exists || sut_bgp_config_present; then
        log "ERROR: down did not produce clean state"
        log "  SUT peer state:        $sutstate (expected noinstance)"
        log "  network exists:        $(yesno network_exists) (expected no)"
        log "  peer container exists: $(yesno peer_container_exists) (expected no)"
        log "  SUT BGP config present: $(yesno sut_bgp_config_present) (expected no)"
        exit 5
    fi
    log "down: clean state confirmed"
}

case "${1:-}" in
    up)     verb_up ;;
    down)   verb_down ;;
    status) verb_status ;;
    *)
        log "usage: $0 {up|down|status}"
        exit 64
        ;;
esac
