#!/usr/bin/env bash
#
# Build the AgentCore Runtime direct-code-deployment package for the Ficus agent.
# Produces dist/ficus-agent-agentcore.zip with main.py and all dependencies at the
# zip root, built as Linux ARM64 wheels (AgentCore Runtime runs on Graviton).
#
# Usage: ./build_zip.sh [python-version]   (default 3.12)
set -euo pipefail

PY_VERSION="${1:-3.12}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build/package"
DIST="$HERE/dist"
ZIP="$DIST/ficus-agent-agentcore.zip"

rm -rf "$HERE/build" "$ZIP"
mkdir -p "$BUILD" "$DIST"

uv pip install \
  --target "$BUILD" \
  --python-platform aarch64-manylinux2014 \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  -r "$HERE/requirements.txt"

cp "$HERE/main.py" "$BUILD/"

# Drop caches and make every file world-readable so the runtime can load it.
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} +
chmod -R a+rX "$BUILD"

(cd "$BUILD" && zip -qr -X "$ZIP" .)
echo "Built $ZIP ($(du -h "$ZIP" | cut -f1))"
