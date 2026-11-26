#!/usr/bin/env bash
set -euo pipefail

apply=0
[[ "${1:-}" == "--apply" ]] && apply=1
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
flags=()
[[ $apply -eq 1 ]] && flags=(--apply)

run() {
  if [[ $apply -eq 1 ]]; then
    sudo "$@"
  else
    echo "DRY RUN: $*"
  fi
}

move_contents() {
  local source="$1" target="$2"
  [[ -d "$source" ]] || return 0
  run install -d -m 0750 "$target"
  run rsync -a --remove-source-files "$source/" "$target/"
  run find "$source" -depth -type d -empty -delete
}

for environment in dev prod; do
  run install -d -m 0750 "/opt/recordings/$environment"
  move_contents "/opt/recording/$environment" "/opt/recordings/$environment"
  [[ -f "/opt/recording/.$environment-uploads-migrated" ]] && run mv "/opt/recording/.$environment-uploads-migrated" "/opt/recordings/.$environment-uploads-migrated"
done
[[ -d /opt/recording ]] && run find /opt/recording -maxdepth 0 -type d -empty -delete

move_contents /opt/logs/prod/prod /opt/logs/prod
[[ -d /opt/logs/dev/dev ]] && run rm -rf /opt/logs/dev/dev

for environment in dev prod; do
  sudo python3 "$here/migrate_storage_layout.py" --recordings "/opt/recordings/$environment" "${flags[@]}"
done
sudo python3 "$here/migrate_storage_layout.py" --logs /opt/logs "${flags[@]}"
