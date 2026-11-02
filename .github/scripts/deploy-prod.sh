#!/usr/bin/env bash
set -euo pipefail

# Deploy talentscope-python into the VM production stack.
# Layout:
#   /opt/talentscope/docker-compose.azure.yml
#   /opt/talentscope/.env.local
#   /opt/talentscope/python/   <- this repo synced here

if [[ "${GITHUB_REF:-}" != "refs/heads/main" ]]; then
  echo "Production deployment is allowed from main only." >&2
  exit 1
fi

if [[ -z "${GITHUB_SHA:-}" ]]; then
  echo "GITHUB_SHA is required; run this script from the production workflow." >&2
  exit 1
fi

SRC="$(git rev-parse --show-toplevel)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/talentscope}"
APP_DIR="${APP_DIR:-$DEPLOY_ROOT/python}"
ENV_FILE="${ENV_FILE:-$DEPLOY_ROOT/.env.local}"
COMPOSE_FILE="${COMPOSE_FILE:-$DEPLOY_ROOT/docker-compose.azure.yml}"
RECORDINGS_ENV=prod
export RECORDINGS_ENV
COMPOSE_OVERRIDE="$APP_DIR/compose.recordings.yml"
SERVICE=python
IMAGE=talentscope-prod-api:latest
ROLLBACK_IMAGE=talentscope-prod-api:rollback
SHA="${GITHUB_SHA:0:12}"
CANDIDATE_IMAGE="talentscope-prod-api:deploy-$SHA"
LOCK_FILE=/tmp/talentscope-prod-python-deploy.lock

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another TalentScope production Python deployment is running; retry after it finishes." >&2
  exit 1
fi

[[ -f "$ENV_FILE" ]] || { echo "Missing production environment file: $ENV_FILE" >&2; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo "Missing production Compose file: $COMPOSE_FILE" >&2; exit 1; }

sudo mkdir -p /opt/logs/prod /opt/logs/dev
LOG_COMPOSE_DIR="$(mktemp -d)"
LOG_COMPOSE_FILE="$LOG_COMPOSE_DIR/docker-compose.logging.yml"
trap 'rm -f "$LOG_COMPOSE_FILE"; rmdir "$LOG_COMPOSE_DIR"' EXIT
cat > "$LOG_COMPOSE_FILE" <<EOF
services:
  python:
    environment:
      NODE_ENV: production
      LOG_DIR: /opt/logs
    volumes:
      - /opt/logs:/opt/logs
EOF

echo "== Sync API $SHA -> $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown -R "$(id -un):$(id -gn)" "$APP_DIR"
rsync -a --delete --checksum \
  --exclude='/.env*' --exclude='/.git/' --exclude='/.github/' \
  --exclude='/uploads/' --exclude='/recordings/' --exclude='/logs/' --exclude='/venv/' --exclude='/.venv/' \
  --exclude='/.pytest_cache/' --exclude='/**/__pycache__/' --exclude='*.pyc' \
  "$SRC/" "$APP_DIR/"
[[ -f "$COMPOSE_OVERRIDE" ]] || { echo "Missing recordings Compose override: $COMPOSE_OVERRIDE" >&2; exit 1; }

sudo install -d -m 0750 "/opt/recordings/$RECORDINGS_ENV"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -f "$LOG_COMPOSE_FILE" -f "$COMPOSE_OVERRIDE")
cd "$DEPLOY_ROOT"
"${COMPOSE[@]}" config --services | grep -Fxq "$SERVICE" || {
  echo "Compose service '$SERVICE' was not found in $COMPOSE_FILE." >&2
  exit 1
}
"${COMPOSE[@]}" config --images | grep -Fxq "$IMAGE" || {
  echo "Compose service '$SERVICE' must use image $IMAGE." >&2
  exit 1
}

PREVIOUS_CONTAINER="$("${COMPOSE[@]}" ps -q "$SERVICE" || true)"
PREVIOUS_IMAGE_REF=""
if [[ -n "${PREVIOUS_CONTAINER:-}" ]]; then
  PREVIOUS_IMAGE_ID="$(docker inspect --format='{{.Image}}' "$PREVIOUS_CONTAINER")"
  PREVIOUS_IMAGE_REF="$(docker inspect --format='{{.Config.Image}}' "$PREVIOUS_CONTAINER")"
  docker image tag "$PREVIOUS_IMAGE_ID" "$ROLLBACK_IMAGE"
fi

echo "== Build API image (context=$DEPLOY_ROOT, dockerfile=python/Dockerfile)"
if ! docker build --tag "$CANDIDATE_IMAGE" -f "$APP_DIR/Dockerfile" "$DEPLOY_ROOT"; then
  echo "API image build failed; the running service was not changed." >&2
  exit 1
fi
docker image tag "$CANDIDATE_IMAGE" "$IMAGE"

bash "$APP_DIR/scripts/prepare_recordings_storage.sh" "$RECORDINGS_ENV" "$PREVIOUS_CONTAINER"

healthy() {
  for _ in $(seq 1 60); do
    if "${COMPOSE[@]}" exec -T "$SERVICE" curl -fsS --max-time 5 \
      http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

rollback() {
  if [[ -z "$PREVIOUS_IMAGE_REF" ]]; then
    echo "No prior API container was available to roll back to." >&2
    return 1
  fi
  echo "== Restoring previous API image"
  docker image tag "$ROLLBACK_IMAGE" "$PREVIOUS_IMAGE_REF"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"
  if healthy; then
    echo "Previous API version restored."
    return 0
  fi
  echo "Previous API version did not become healthy; inspect the container logs." >&2
  return 1
}

verify_upload_mount() {
  local container source
  container="$("${COMPOSE[@]}" ps -q "$SERVICE" || true)"
  if [[ -z "$container" ]]; then
    echo "The production API container is missing after recreation." >&2
    return 1
  fi
  source="$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/app/uploads"}}{{.Source}}{{end}}{{end}}' "$container")" || return 1
  if [[ "$source" != "/opt/recordings/$RECORDINGS_ENV" ]]; then
    echo "Production uploads are mounted from '$source', expected /opt/recordings/$RECORDINGS_ENV." >&2
    return 1
  fi
  return 0
}

if ! "${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE"; then
  echo "API container recreation failed." >&2
  if ! rollback; then
    echo "Automatic API rollback failed; manual intervention is required." >&2
  fi
  exit 1
fi

if ! verify_upload_mount; then
  if ! rollback; then
    echo "Automatic API rollback failed; manual intervention is required." >&2
  fi
  exit 1
fi

if ! healthy; then
  echo "API health check failed; recent logs follow." >&2
  if ! "${COMPOSE[@]}" logs --tail=80 "$SERVICE"; then
    echo "Could not retrieve API container logs." >&2
  fi
  if ! rollback; then
    echo "Automatic API rollback failed; manual intervention is required." >&2
  fi
  exit 1
fi

if ! printf '%s\n' "$SHA" > "$DEPLOY_ROOT/DEPLOYED_COMMIT-python"; then
  echo "Warning: could not write the API deployment marker." >&2
fi
docker image rm "$CANDIDATE_IMAGE" >/dev/null 2>&1 || true
if [[ -n "$PREVIOUS_IMAGE_REF" ]]; then
  docker image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
fi
echo "== API production deployment OK: $SHA is live on :8000"
