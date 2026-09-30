#!/bin/sh
# Find a working Python 3 and exec it with this script's own arguments. On Windows the usual installs
# provide `python`/`py`, not `python3` — and a bare `python3` there is often a Microsoft Store
# placeholder that fails, so each candidate is probed before use. If none works, exit 0 silently: a
# missing interpreter is never worth a hook error on every tool call.
set -eu

is_python3() {
  command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1
}

case "$(uname -s 2>/dev/null || true)" in
  MINGW*|MSYS*|CYGWIN*)
    for c in py python python3; do
      if is_python3 "$c"; then exec "$c" "$@"; fi
    done
    ;;
  *)
    if is_python3 python3; then exec python3 "$@"; fi
    if is_python3 python; then exec python "$@"; fi
    ;;
esac

exit 0
