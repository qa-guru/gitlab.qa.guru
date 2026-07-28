#!/usr/bin/env bash
# Re-apply SSOT gitlab.rb.template to Box 3, preserving managed OmniAuth block.
# Usage (laptop): ./apply-gitlab-rb.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${GITLAB_DEPLOY_HOST:-box3}"
REMOTE_RB="/tmp/gitlab.rb.qaguru"
TEMPLATE="${SCRIPT_DIR}/gitlab.rb.template"

echo "==> Upload ${TEMPLATE} → ${HOST}:${REMOTE_RB}"
scp "${TEMPLATE}" "${HOST}:${REMOTE_RB}"

echo "==> Merge OmniAuth block + install /etc/gitlab/gitlab.rb + reconfigure"
ssh "${HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo python3 <<'PY'
from pathlib import Path

begin = "# BEGIN github omniauth (managed)"
end = "# END github omniauth (managed)"
live = Path("/etc/gitlab/gitlab.rb")
template = Path("/tmp/gitlab.rb.qaguru")
text = template.read_text(encoding="utf-8")

omniauth = ""
if live.exists():
    cur = live.read_text(encoding="utf-8")
    if begin in cur and end in cur:
        block = cur.split(begin, 1)[1].split(end, 1)[0]
        omniauth = f"{begin}{block}{end}\n"

# Drop placeholder managed block from template (commented stubs)
if begin in text and end in text:
    pre, rest = text.split(begin, 1)
    _, post = rest.split(end, 1)
    text = pre.rstrip() + "\n" + post.lstrip("\n")

if omniauth:
    text = text.rstrip() + "\n\n" + omniauth

live.write_text(text, encoding="utf-8")
print("wrote", live, "bytes", live.stat().st_size)
PY

sudo gitlab-ctl reconfigure
sudo gitlab-ctl restart sidekiq
sleep 20
sudo gitlab-ctl status sidekiq
echo "==> health port:"
ss -tlnp | grep -E '8094|8092' || true
echo -n "==> SIDEKIQ_MEMORY_KILLER_MAX_RSS="
sudo cat /opt/gitlab/etc/gitlab-rails/env/SIDEKIQ_MEMORY_KILLER_MAX_RSS
echo
REMOTE

echo "==> Done. Watch: ssh ${HOST} 'sudo gitlab-ctl tail sidekiq'"
