#!/bin/sh
# Root only long enough to hand the data dir to the service user, then drop
# for good. Images before this one ran as root, so an existing volume is
# root-owned; chown once (skipped when ownership already matches) keeps its
# admin.db and mailboxes readable. Started with --user, it just runs.
set -eu

APP_UID=10001
DATA="${AI_AGENT_CHANNEL_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  if [ -n "$(find "$DATA" \( ! -user "$APP_UID" -o ! -group "$APP_UID" \) -print -quit)" ]; then
    chown -R "$APP_UID:$APP_UID" "$DATA"
  fi
  chmod 700 "$DATA"
  exec setpriv --reuid="$APP_UID" --regid="$APP_UID" --clear-groups --no-new-privs -- "$@"
fi
exec "$@"
