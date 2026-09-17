#!/bin/sh
# The gate for this repository: unit tests, version agreement, manifest validation, and the privacy scan.
# The privacy scan reads a list of terms that must never ship (names, private repositories, machine
# paths). The list lives outside the repository, since it would otherwise publish the very terms it
# guards: $PRIVATE_TERMS, or ~/.config/agulhas/private-terms.txt. One term per line, no blank lines.
set -eu
cd "$(dirname "$0")/.."

py=
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
if [ -z "$py" ]; then
  echo "check.sh: no Python 3 found as python3, python or py" >&2
  exit 1
fi
"$py" -m unittest discover -s plugins/orchestration/tests
"$py" -m unittest discover -s plugins/agent-cost/skills/report/tests
"$py" -m unittest discover -s plugins/cache-guard/tests

# A plugin's version is written twice, in its manifest and in the marketplace entry; they must agree.
"$py" - <<'EOF'
import glob, json, sys
with open(".claude-plugin/marketplace.json", encoding="utf-8") as f:
    listed = {entry["name"]: entry.get("version") for entry in json.load(f)["plugins"]}
ok = True
for manifest in sorted(glob.glob("plugins/*/.claude-plugin/plugin.json")):
    with open(manifest, encoding="utf-8") as f:
        plugin = json.load(f)
    name, version = plugin["name"], plugin.get("version")
    if version is None or version != listed.get(name):
        print(f"version mismatch for {name}: {manifest} says {version!r}, marketplace.json says {listed.get(name)!r}", file=sys.stderr)
        ok = False
    else:
        print(f"version: {name} {version}, listed the same")
sys.exit(0 if ok else 1)
EOF

if command -v claude >/dev/null 2>&1; then
  claude plugin validate . --strict
  for plugin in plugins/*/; do
    claude plugin validate "${plugin%/}" --strict
  done
else
  echo "manifest validation: claude CLI not found — skipped" >&2
fi

terms="${PRIVATE_TERMS:-$HOME/.config/agulhas/private-terms.txt}"
if [ -f "$terms" ]; then
  if grep -rniF -f "$terms" --exclude-dir=.git --exclude=.git --exclude-dir=__pycache__ --exclude-dir=.build . ; then
    echo "privacy gate: the lines above carry a private term" >&2
    exit 1
  fi
  echo "privacy gate: clean"
else
  echo "privacy gate: no terms file at $terms — skipped" >&2
fi
