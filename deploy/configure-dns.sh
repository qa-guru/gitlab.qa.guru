#!/usr/bin/env bash
# Cloudflare A record: gitlab.qa.guru → Box2 edge (dark edge, same as jira/confluence).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${ZERO_DESIGN_SYSTEM:-}" ]]; then
  REPO_ROOT="${ZERO_DESIGN_SYSTEM}"
else
  REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"
fi
if [[ ! -f "${REPO_ROOT}/scripts/infra-dns/dns_audit.py" ]]; then
  echo "FAIL: monorepo scripts/infra-dns/dns_audit.py not found (set ZERO_DESIGN_SYSTEM)" >&2
  exit 1
fi
BOX2="${BOX2_CI:-89.248.193.83}"
FQDN="gitlab.qa.guru"
APPLY="${1:---apply}"

export REPO_ROOT
python - "${BOX2}" "${APPLY}" <<'PY'
import importlib.util
import json
import os
import sys
from pathlib import Path

repo = Path(os.environ["REPO_ROOT"])
spec = importlib.util.spec_from_file_location(
    "dns_audit", repo / "scripts/infra-dns/dns_audit.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ip = sys.argv[1]
apply = sys.argv[2] == "--apply"
result = mod.cf_upsert_a_grey("qa.guru", "gitlab.qa.guru", ip, apply=apply)
print(json.dumps(result, indent=2))
if apply and result.get("action") not in ("noop", "created", "updated"):
    raise SystemExit(1)
PY

echo "DNS ${FQDN} → ${BOX2}"
dig +short "${FQDN}" A
