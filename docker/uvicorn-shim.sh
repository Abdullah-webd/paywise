#!/bin/sh
# uvicorn shim — installed over /usr/local/bin/uvicorn in the Docker image.
#
# Railway runs a dashboard "Custom Start Command" in exec form (no shell), so a
# command like `uvicorn ... --port ${PORT:-8000}` hands uvicorn the LITERAL
# string "${PORT:-8000}", uvicorn rejects it, and the container crash-loops
# (502 "Application failed to respond"). This wrapper expands any such
# unexpanded $PORT token itself, then execs the real uvicorn unchanged.
# Preferred start command is still `python -m app` (see app/__main__.py).
n=$#
i=0
while [ "$i" -lt "$n" ]; do
  a=$1; shift
  case "$a" in
    '${PORT'*|'$PORT'|'${PORT}'|'${{PORT}}') a="${PORT:-8000}" ;;
  esac
  set -- "$@" "$a"
  i=$((i+1))
done
exec /usr/local/bin/uvicorn-real "$@"
