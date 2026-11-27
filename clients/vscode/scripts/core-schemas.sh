#!/usr/bin/env bash
# Refresh the JSON schemas galaius-core checks in (galaius_core/schema/*.schema.json) from its contracts.
# usage: core-schemas.sh <galaius-core checkout>
# Deliberate and explicit: it writes ONLY into the checkout it is given, which is then committed in
# galaius-core. `generate-types.sh` (the pre-commit hook) never touches another checkout.
set -e

CORE_DIR="${1:?usage: core-schemas.sh <galaius-core checkout>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$(dirname "$(dirname "$HERE")")")"
CORE_DIR="$(cd "$CORE_DIR" && pwd)"
SCHEMA_DIR="$CORE_DIR/galaius_core/schema"
[ -f "$CORE_DIR/pyproject.toml" ] && [ -d "$SCHEMA_DIR" ] || { echo "[core-schemas] not a galaius-core checkout: $CORE_DIR" >&2; exit 1; }

# Same bundled-data rule as generate-types.sh: the catalog import never refreshes prices or probes Ollama.
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export OLLAMA_DISCOVERY="0"

for family in prompt account workflow admin; do
    uv run --directory "$REPO_DIR" --with-editable "$CORE_DIR" python "$HERE/schema/contracts.py" "$family" \
        > "$SCHEMA_DIR/$family-contracts.schema.json.new"
    mv "$SCHEMA_DIR/$family-contracts.schema.json.new" "$SCHEMA_DIR/$family-contracts.schema.json"
done
echo "[core-schemas] wrote $SCHEMA_DIR" >&2
