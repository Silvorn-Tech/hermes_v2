#!/usr/bin/bash
# One-time (idempotent) installer for hermes_v2's pull-based deploy on
# ROMEO: a systemd timer runs deploy.sh every 5 minutes that pulls
# ghcr.io/silvorn-tech/hermes_v2, compares the image digest, and
# recreates the container on change. hermes_front_end's own
# deploy/romeo-setup.sh mirrors this exact mechanism.
#
# This file documents, in version control, the setup that was already
# running on ROMEO before this file existed -- every value below
# (compose.yaml, can-deploy, the systemd units) was copied verbatim from
# the live server, not invented. Re-running this script is safe and
# idempotent; it will simply recreate what's already there.
#
# Run this manually, with sudo, after reviewing it:
#   sudo bash deploy/romeo-setup.sh
#
# Does NOT create or touch /opt/hermes-v2/.env -- that file holds real
# secrets (DB password, Binance credentials, session secret, etc.) and
# must already exist; this script only ever writes non-secret
# infrastructure files.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash deploy/romeo-setup.sh)." >&2
  exit 1
fi

readonly APP_DIR="/opt/hermes-v2"
readonly SERVICE_NAME="hermes-v2-deploy"
# ntfy.sh push notification when deploy.sh actually deploys (or fails) --
# not a secret (ntfy.sh topics are unauthenticated by default: anyone who
# knows this string could publish/subscribe to it), just unguessable
# enough that nobody stumbles onto it by chance. Same topic
# hermes_front_end's own romeo-setup.sh uses, so both services' deploy
# notifications land in one place. Subscribe via the ntfy app or
# https://ntfy.sh/<topic>. Rotate by changing this constant (in both
# repos, to keep them sharing one feed) and re-running each script.
readonly NTFY_TOPIC="hermes-deploys-600e21984f3a"

mkdir -p "$APP_DIR"

cat > "$APP_DIR/compose.yaml" <<'COMPOSE'
services:
  hermes-v2:
    container_name: hermes-v2
    image: ghcr.io/silvorn-tech/hermes_v2:latest
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    logging:
      driver: local
      options:
        max-size: "10m"
        max-file: "3"

  postgres:
    image: postgres:18
    container_name: hermes-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - hermes_postgres_data:/var/lib/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  hermes_postgres_data:
COMPOSE

cat > "$APP_DIR/deploy.sh" <<'DEPLOY'
#!/usr/bin/bash
set -Eeuo pipefail

readonly DOCKER_BIN="/usr/bin/docker"
readonly COMPOSE_FILE="/opt/hermes-v2/compose.yaml"
readonly SERVICE_NAME="hermes-v2"
readonly CONTAINER_NAME="hermes-v2"
readonly IMAGE="ghcr.io/silvorn-tech/hermes_v2:latest"
readonly PRE_DEPLOY_HOOK="/opt/hermes-v2/can-deploy"
readonly LOCK_FILE="/run/hermes-v2-deploy/deploy.lock"
readonly NTFY_TOPIC="hermes-deploys-600e21984f3a"

log() {
  printf '%s %s\n' "$(/usr/bin/date --iso-8601=seconds)" "$*"
}

# Best-effort: a notification failure (ntfy.sh unreachable, DNS hiccup,
# etc.) must never fail the deploy itself, so errors here are swallowed.
notify() {
  /usr/bin/curl -fsS -m 10 -H "Title: $1" -d "$2" \
    "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

fail() {
  log "decision=error result=failure reason=$1"
  notify "hermes-v2 deploy FAILED" "$1"
  exit 1
}

# Safety net for every command below that ISN'T already explicitly checked
# (e.g. `docker compose pull`) -- without this, a failure like the
# `.env` permission bug that motivated this trap (docker compose
# couldn't read /opt/hermes-v2/.env) aborts the script via `set -e`
# *without* ever calling fail()/notify(), so it fails completely
# silently, tick after tick, with no push and nothing but a systemd
# exit code to notice it by. `-E` above (errtrace) makes this trap
# apply inside functions too. Safe from double-firing: `exit` inside
# fail() is not itself a failing command, and every already-explicit
# `fail "..."` call site is either a direct call or already exempted
# from ERR by being on the "checked" side of `||`/`if`.
trap 'fail "unexpected command failure (exit $?) at line ${LINENO}"' ERR

image_id() {
  "$DOCKER_BIN" image inspect --format '{{.Id}}' "$IMAGE"
}

if [[ ! -f "$COMPOSE_FILE" ]]; then
  fail "compose-file-missing"
fi

if [[ ! -x "$PRE_DEPLOY_HOOK" ]]; then
  fail "pre-deploy-hook-missing-or-not-executable"
fi

exec 9>"$LOCK_FILE"

if ! /usr/bin/flock -n 9; then
  log "decision=skipped result=success reason=deployment-already-running"
  exit 0
fi

previous_digest="$(
  "$DOCKER_BIN" inspect --format '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null || true
)"

previous_digest="${previous_digest:-none}"

log "decision=check previous_digest=$previous_digest"

"$DOCKER_BIN" compose -f "$COMPOSE_FILE" pull "$SERVICE_NAME"

new_digest="$(image_id)"

if [[ "$previous_digest" == "$new_digest" ]]; then
  log "decision=no-change previous_digest=$previous_digest new_digest=$new_digest result=success"
  exit 0
fi

if ! "$PRE_DEPLOY_HOOK"; then
  log "decision=blocked previous_digest=$previous_digest new_digest=$new_digest result=success reason=pre-deploy-check"
  exit 0
fi

log "decision=deploy previous_digest=$previous_digest new_digest=$new_digest"

"$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d "$SERVICE_NAME"

deployed_digest="$("$DOCKER_BIN" inspect --format '{{.Image}}' "$CONTAINER_NAME")"

if [[ "$deployed_digest" != "$new_digest" ]]; then
  fail "deployed-image-mismatch expected=$new_digest actual=$deployed_digest"
fi

log "decision=deployed previous_digest=$previous_digest new_digest=$new_digest result=success"
notify "hermes-v2 deployed" "${previous_digest:0:12} -> ${new_digest:0:12}"
DEPLOY

cat > "$APP_DIR/can-deploy" <<'HOOK'
#!/usr/bin/bash

# Future extension point:
# block deployment while Hermes v2 has an open trading position.
# It intentionally permits deployments until that logic exists.

exit 0
HOOK

chown -R root:root "$APP_DIR"
chmod 755 "$APP_DIR" "$APP_DIR/deploy.sh" "$APP_DIR/can-deploy"
chmod 644 "$APP_DIR/compose.yaml"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Hermes v2 image update and deployment check
Wants=docker.service network-online.target
After=docker.service network-online.target

[Service]
Type=oneshot
User=hermes-deploy
Group=hermes-deploy
SupplementaryGroups=docker
Environment=HOME=/home/hermes-deploy
ExecStart=${APP_DIR}/deploy.sh

RuntimeDirectory=${SERVICE_NAME}
RuntimeDirectoryMode=0750

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
ReadOnlyPaths=${APP_DIR}
UNIT

cat > "/etc/systemd/system/${SERVICE_NAME}.timer" <<TIMER
[Unit]
Description=Run Hermes v2 deployment check every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"

echo
echo "Done. Status:"
systemctl status "${SERVICE_NAME}.timer" --no-pager

cat <<NEXT

Subscribe to deploy notifications: install the ntfy app (or open
https://ntfy.sh/${NTFY_TOPIC} in a browser) and subscribe to the topic
"${NTFY_TOPIC}" -- you'll get a push the moment deploy.sh actually
deploys something (or fails). Routine "no-change" ticks stay silent.
This is the same topic hermes_front_end's deploy.sh posts to.

NEXT

cat <<'NEXT'
Reminders:
- This script never creates or touches /opt/hermes-v2/.env -- it must
  already exist with real secrets (DB password, Binance credentials,
  session secret, HERMES_ALLOWED_ORIGINS, etc.) before deploy.sh's first
  `docker compose up -d` can start the container successfully.
- If `docker compose pull` fails with "unauthorized" or "manifest
  unknown" on the first tick, GHCR either has no image yet or the
  hermes-deploy user's stored GHCR login lacks read:packages on
  hermes_v2 specifically.
NEXT
