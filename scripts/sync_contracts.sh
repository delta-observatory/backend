#!/bin/sh
# Copy contracts/ and migrations/ from one commit or tag of the database
# repository into vendor/. The API reads vendor/contracts/ at runtime.
# Tests apply vendor/migrations/ to a temporary PostgreSQL server.
# Usage: scripts/sync_contracts.sh <database-commit-or-tag>
set -eu
REF="${1:?database commit or tag required}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
DATABASE="$HERE/../database"
COMMIT=$(git -C "$DATABASE" rev-parse --verify "$REF^{commit}")
rm -rf "$HERE/vendor/contracts" "$HERE/vendor/migrations"
mkdir -p "$HERE/vendor"
git -C "$DATABASE" archive "$COMMIT" contracts migrations | tar -x -C "$HERE/vendor"
echo "$COMMIT" > "$HERE/vendor/contracts/SOURCE"
echo "$COMMIT" > "$HERE/vendor/migrations/SOURCE"
echo "vendored database@$COMMIT"
