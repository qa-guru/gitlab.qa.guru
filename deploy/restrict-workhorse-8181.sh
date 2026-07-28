#!/usr/bin/env bash
# Restrict GitLab workhorse :8181 on Box3 → BOX2_CI + localhost only.
# Live + persist: iptables chain GITLAB_WH_8181 + /etc/iptables/rules.v4
# (netfilter-persistent). Does not enable/install UFW — on Ubuntu it conflicts
# with iptables-persistent and Box3 co-hosts Jira/Confluence.
# Does not touch Jira :8080 / Confluence ports. Avoids apt/gitlab-ce triggers.
set -euo pipefail

HOST="${GITLAB_DEPLOY_HOST:-box3}"
BOX2_CI="${BOX2_CI:-89.248.193.83}"
CHAIN="GITLAB_WH_8181"
PORT=8181

ssh "${HOST}" "BOX2_CI='${BOX2_CI}' CHAIN='${CHAIN}' PORT='${PORT}' bash -s" <<'REMOTE'
set -euo pipefail

BOX2_CI="${BOX2_CI:?}"
CHAIN="${CHAIN:?}"
PORT="${PORT:?}"

echo "==> iptables chain ${CHAIN} (tcp/${PORT})"

sudo iptables -D INPUT -p tcp --dport "${PORT}" -j "${CHAIN}" 2>/dev/null || true
if sudo iptables -L "${CHAIN}" -n >/dev/null 2>&1; then
  sudo iptables -F "${CHAIN}"
else
  sudo iptables -N "${CHAIN}"
fi

sudo iptables -A "${CHAIN}" -s 127.0.0.1/32 -j ACCEPT \
  -m comment --comment "gitlab-workhorse localhost"
sudo iptables -A "${CHAIN}" -s "${BOX2_CI}/32" -j ACCEPT \
  -m comment --comment "gitlab-workhorse BOX2_CI"
sudo iptables -A "${CHAIN}" -j DROP \
  -m comment --comment "gitlab-workhorse deny others"
sudo iptables -I INPUT -p tcp --dport "${PORT}" -j "${CHAIN}"

if command -v ip6tables >/dev/null 2>&1; then
  sudo ip6tables -D INPUT -p tcp --dport "${PORT}" -j DROP 2>/dev/null || true
  sudo ip6tables -I INPUT -p tcp --dport "${PORT}" -j DROP \
    -m comment --comment "gitlab-workhorse no ipv6"
fi

echo "==> persist /etc/iptables/rules.v{4,6}"
sudo mkdir -p /etc/iptables
sudo iptables-save | sudo tee /etc/iptables/rules.v4 >/dev/null
sudo ip6tables-save | sudo tee /etc/iptables/rules.v6 >/dev/null
if systemctl list-unit-files 2>/dev/null | grep -q '^netfilter-persistent'; then
  sudo systemctl enable netfilter-persistent >/dev/null 2>&1 || true
fi

echo "==> verify"
sudo iptables -L "${CHAIN}" -n -v --line-numbers
echo "--- INPUT (head) ---"
sudo iptables -L INPUT -n --line-numbers | head -15
REMOTE

echo "Workhorse :${PORT} restricted to 127.0.0.1 + ${BOX2_CI} on ${HOST}."
