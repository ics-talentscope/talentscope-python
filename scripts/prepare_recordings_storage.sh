#!/usr/bin/env bash
set -euo pipefail

environment="${1:-}"
container_id="${2:-}"

case "$environment" in
  prod|dev) ;;
  *)
    echo "Usage: $0 <prod|dev> [running-container-id]" >&2
    exit 2
    ;;
esac

case "$environment" in
  dev)
    target="/opt/recordings/dev"
    marker="/opt/recordings/.dev-uploads-migrated"
    ;;
  prod)
    target="/opt/recording/prod"
    marker="/opt/recording/.prod-uploads-migrated"
    ;;
esac
sudo install -d -m 0750 "$target"

if [[ -f "$marker" ]]; then
  echo "The $environment uploads migration has already been completed."
  exit 0
fi

source=""
if [[ -n "$container_id" ]]; then
  source="$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/app/uploads"}}{{.Source}}{{end}}{{end}}' "$container_id")"
  if [[ -z "$source" ]]; then
    echo "Cannot find the existing /app/uploads mount for container $container_id; refusing to recreate it." >&2
    exit 1
  fi
else
  legacy_volume="talentscope-${environment}_uploads"
  if source="$(docker volume inspect --format='{{.Mountpoint}}' "$legacy_volume" 2>/dev/null)"; then
    :
  else
    legacy_directory=""
    if [[ "$environment" == "prod" && -d /opt/recordings/prod ]]; then
      legacy_directory=/opt/recordings/prod
    fi
    if [[ -n "$legacy_directory" ]]; then
      source="$legacy_directory"
    else
      echo "No legacy upload volume found for $environment; preparing an empty persistent directory."
      sudo install -m 0600 /dev/null "$marker"
      exit 0
    fi
  fi
fi

if [[ "$source" == "$target" ]]; then
  echo "Uploads are already stored in $target."
  sudo install -m 0600 /dev/null "$marker"
  exit 0
fi

if ! sudo test -d "$source"; then
  echo "The existing upload source is unavailable: $source" >&2
  exit 1
fi

echo "== Copying existing $environment uploads from $source to $target (source retained)"
sudo rsync -a --checksum -- "$source/" "$target/"
sudo install -m 0600 /dev/null "$marker"
