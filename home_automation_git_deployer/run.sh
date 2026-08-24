#!/usr/bin/with-contenv bashio
# Version history:
# 1.3.0 - 2026-08-24 - Switched the add-on entry point to the flattened deployer.py runtime and removed the versioned wrapper chain.
# 1.2.0 - 2026-08-23 - Switched the local add-on entry point to deployer_v120.py.
# 1.1.0 - 2026-08-23 - Switched the local add-on entry point to deployer_v110.py.
set -euo pipefail

mkdir -p /data/ssh
if [ ! -f /data/ssh/known_hosts ]; then
  cp /github_known_hosts /data/ssh/known_hosts
  chmod 0644 /data/ssh/known_hosts
fi

exec python3 /deployer.py
