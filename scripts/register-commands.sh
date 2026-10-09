#!/bin/bash
# Registers HTTP-mode commands. Gateway mode registers commands automatically.
#   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... ./scripts/register-commands.sh
set -e
: "${DISCORD_APP_ID:?set DISCORD_APP_ID}"; : "${DISCORD_BOT_TOKEN:?set DISCORD_BOT_TOKEN}"
curl -sS -X PUT "https://discord.com/api/v10/applications/$DISCORD_APP_ID/commands" \
  -H "Authorization: Bot $DISCORD_BOT_TOKEN" -H "Content-Type: application/json" \
  --data-binary @"$(dirname "$0")/../container/commands.json"
echo
