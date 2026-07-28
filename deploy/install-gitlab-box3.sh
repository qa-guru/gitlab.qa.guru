#!/usr/bin/env bash
# Install GitLab CE Omnibus on Box 3 (BOX3_ZOO).
# Run from laptop: ./install-gitlab-box3.sh
# Requires: ssh box3, sudo on host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${GITLAB_DEPLOY_HOST:-box3}"
REMOTE_RB="/tmp/gitlab.rb.qaguru"
CGROUP_DROPIN="/etc/systemd/system/gitlab-runsvdir.service.d/memory-limit.conf"

echo "==> Preflight on ${HOST}"
ssh "${HOST}" 'free -h | head -2; df -h / | tail -1; which gitlab-ctl 2>/dev/null && echo "GitLab already installed — abort or uninstall first" && exit 1 || true'

echo "==> Upload gitlab.rb template"
scp "${SCRIPT_DIR}/gitlab.rb.template" "${HOST}:${REMOTE_RB}"

echo "==> Install GitLab CE Omnibus (may take 5–10 min)"
ssh "${HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v gitlab-ctl >/dev/null 2>&1; then
  curl -fsSL https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | sudo bash
  sudo EXTERNAL_URL="https://gitlab.qa.guru" apt-get install -y gitlab-ce
fi

sudo cp /tmp/gitlab.rb.qaguru /etc/gitlab/gitlab.rb
sudo gitlab-ctl reconfigure

# cgroup RAM cap (12G hard / 10G soft)
sudo mkdir -p /etc/systemd/system/gitlab-runsvdir.service.d
sudo tee /etc/systemd/system/gitlab-runsvdir.service.d/memory-limit.conf >/dev/null <<'CG'
[Service]
MemoryMax=12G
MemoryHigh=10G
CG
sudo systemctl daemon-reload
sudo systemctl restart gitlab-runsvdir.service || sudo gitlab-ctl restart

# backup cron
echo '0 2 * * * root gitlab-backup create CRON=1 SKIP=registry,artifacts,builds,pages,lfs,terraform_state,packages,ci_secure_files' | sudo tee /etc/cron.d/gitlab-backup

# ensure backup dir
sudo mkdir -p /var/opt/gitlab/backups
sudo chown git:git /var/opt/gitlab/backups

echo "==> GitLab workhorse listen:"
ss -tlnp | grep 8181 || true
REMOTE

echo "==> Done. Fetch initial root password (one-time):"
echo "    ssh ${HOST} 'sudo cat /etc/gitlab/initial_root_password'"
