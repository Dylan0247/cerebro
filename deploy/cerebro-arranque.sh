#!/bin/bash
# Generic launcher adapted from the development setup.
set -eu
: "${CEREBRO_DATA_DIR:?Configure CEREBRO_DATA_DIR before starting}"
mkdir -p "$CEREBRO_DATA_DIR"
exec 9>"$CEREBRO_DATA_DIR/windows-inicio.lock"
flock -n 9 || exit 0
systemctl --user start n8n-local.service n8n-obsidian-readonly.service n8n-cerebro-proposals.service n8n-cerebro-chat-sync.service
# A normal Linux process keeps WSL alive independently of terminal windows.
exec sleep infinity

