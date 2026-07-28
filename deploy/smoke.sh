#!/usr/bin/env bash
# Smoke: GitLab HTTPS + health + workhorse ACL + runners.
set -euo pipefail

URL="${GITLAB_URL:-https://gitlab.qa.guru}"
BOX2="${BOX2_CI:-89.248.193.83}"
BOX3="${BOX3_ZOO:-212.92.101.15}"
KEY="${GITLAB_DEPLOY_KEY:-$HOME/.ssh/qa_guru_ed25519}"
HOST="${GITLAB_DEPLOY_HOST:-box3}"
SSH_BOX2=(ssh -i "${KEY}" -o IdentitiesOnly=yes -o ConnectTimeout=10 "qaguru@${BOX2}")
SSH_BOX3=(ssh -o ConnectTimeout=10 "${HOST}")

echo "==> curl sign_in"
code=$(curl -sf -o /dev/null -w '%{http_code}' "${URL}/users/sign_in" || echo "000")
if [[ "${code}" != "200" ]]; then
  echo "FAIL: ${URL}/users/sign_in → ${code}"
  exit 1
fi
echo "OK: ${code}"

echo "==> health"
curl -sf "${URL}/-/health" && echo

echo "==> workhorse :8181 ACL (Box2 allow / public deny)"
box2_code=$("${SSH_BOX2[@]}" "curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: gitlab.qa.guru' http://${BOX3}:8181/" || echo "000")
if [[ "${box2_code}" != "302" && "${box2_code}" != "200" ]]; then
  echo "FAIL: Box2 → ${BOX3}:8181 → ${box2_code} (want 302/200)"
  exit 1
fi
echo "OK: Box2 → :8181 → ${box2_code}"

# Public probe from this host (not Box2). Expect timeout / connection fail.
set +e
public_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: gitlab.qa.guru' "http://${BOX3}:8181/" 2>/dev/null)
public_rc=$?
set -e
if [[ "${public_rc}" -eq 0 && -n "${public_code}" && "${public_code}" != "000" ]]; then
  echo "FAIL: public → ${BOX3}:8181 still open (HTTP ${public_code})"
  exit 1
fi
echo "OK: public → :8181 denied/timeout (rc=${public_rc})"

echo "==> sidekiq health :8094 (Box3; :8092 = Docker currency.niffler.dc)"
"${SSH_BOX3[@]}" 'sudo gitlab-ctl status sidekiq | grep -q "^run: sidekiq" && ss -tln | grep -q ":8094 "'

echo "==> runners (Box2)"
"${SSH_BOX2[@]}" 'sudo gitlab-runner verify' 2>&1

echo "==> smoke PASS"
