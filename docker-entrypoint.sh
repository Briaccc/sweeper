#!/bin/sh
# Stop early with a clear message, rather than a Python traceback, when the
# config volume is not writable by the user the container runs as.
set -e
if ! ( : > /config/.write-test ) 2>/dev/null; then
  echo "sweeper: /config is not writable by uid $(id -u), gid $(id -g)." >&2
  echo "         Check it is not mounted read-only, and give that user the folder" >&2
  echo "         mounted on /config — on the host: chown -R $(id -u):$(id -g) <folder>" >&2
  exit 1
fi
rm -f /config/.write-test
exec "$@"
