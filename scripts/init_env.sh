#!/bin/sh
# Create .env from .env.example with random secrets. Refuses to overwrite an existing .env.
set -eu
cd "$(dirname "$0")/.."
[ -e .env ] && { echo ".env exists, not overwriting" >&2; exit 1; }
umask 077  # .env holds secrets: owner-only
hex() { openssl rand -hex "$1"; }
sed -e "s/CHANGEME_HEX64/$(hex 32)/" .env.example | while IFS= read -r line; do
  case "$line" in *CHANGEME*) printf '%s\n' "$line" | sed "s/CHANGEME/$(hex 16)/" ;; *) printf '%s\n' "$line" ;; esac
done > .env
echo "wrote .env"
