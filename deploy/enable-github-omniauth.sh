#!/usr/bin/env bash
# Apply GitHub OmniAuth on Box 3 GitLab CE from local secrets.
#
# Prerequisites:
#   ~/.config/gitlab/github-oauth.env with:
#     GITHUB_APP_ID=...
#     GITHUB_APP_SECRET=...
#
# OAuth App (org qa-guru):
#   Homepage:  https://gitlab.qa.guru
#   Callback:  https://gitlab.qa.guru/users/auth/github/callback
#   Settings:  https://github.com/organizations/qa-guru/settings/applications
#
# Usage (laptop): ./enable-github-omniauth.sh
set -euo pipefail

HOST="${GITLAB_DEPLOY_HOST:-box3}"
SECRETS_FILE="${HOME}/.config/gitlab/github-oauth.env"

if [[ ! -f "${SECRETS_FILE}" ]]; then
  echo "ERROR: missing ${SECRETS_FILE}" >&2
  echo "Create a GitHub OAuth App (org qa-guru) and write GITHUB_APP_ID / GITHUB_APP_SECRET there." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${SECRETS_FILE}"
if [[ -z "${GITHUB_APP_ID:-}" || -z "${GITHUB_APP_SECRET:-}" ]]; then
  echo "ERROR: ${SECRETS_FILE} must define GITHUB_APP_ID and GITHUB_APP_SECRET" >&2
  exit 1
fi

echo "==> Upload credentials to ${HOST}"
ssh "${HOST}" "sudo tee /etc/gitlab/github-oauth.env >/dev/null && sudo chmod 600 /etc/gitlab/github-oauth.env" <<EOF
GITHUB_APP_ID=${GITHUB_APP_ID}
GITHUB_APP_SECRET=${GITHUB_APP_SECRET}
EOF

echo "==> Patch /etc/gitlab/gitlab.rb"
ssh "${HOST}" "sudo tee /tmp/patch-github-omniauth.py >/dev/null && sudo chmod 700 /tmp/patch-github-omniauth.py" <<'REMOTE'
from pathlib import Path

env = {}
for line in Path("/etc/gitlab/github-oauth.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k] = v

app_id = env["GITHUB_APP_ID"]
app_secret = env["GITHUB_APP_SECRET"]

begin = "# BEGIN github omniauth (managed)"
end = "# END github omniauth (managed)"
block = f"""{begin}
gitlab_rails['omniauth_enabled'] = true
gitlab_rails['omniauth_allow_single_sign_on'] = ['github']
gitlab_rails['omniauth_block_auto_created_users'] = false
gitlab_rails['omniauth_auto_link_user'] = ['github']
gitlab_rails['omniauth_providers'] = [
  {{
    'name' => 'github',
    'app_id' => '{app_id}',
    'app_secret' => '{app_secret}',
    'args' => {{ 'scope' => 'user:email' }}
  }}
]
{end}
"""

rb = Path("/etc/gitlab/gitlab.rb")
text = rb.read_text(encoding="utf-8")
if begin in text and end in text:
    pre, rest = text.split(begin, 1)
    _, post = rest.split(end, 1)
    text = pre.rstrip() + "\n\n" + block + "\n" + post.lstrip("\n")
else:
    text = text.rstrip() + "\n\n" + block + "\n"
rb.write_text(text, encoding="utf-8")
rb.chmod(0o600)
print("patched", rb, "bytes=", rb.stat().st_size)
REMOTE

ssh "${HOST}" 'sudo python3 /tmp/patch-github-omniauth.py 2>/dev/null || sudo python /tmp/patch-github-omniauth.py'

echo "==> gitlab-ctl reconfigure"
ssh "${HOST}" 'sudo gitlab-ctl reconfigure'

echo "==> Wait for sign-in page"
for _ in $(seq 1 40); do
  code="$(curl -s -o /tmp/gl-signin-smoke.html -w '%{http_code}' 'https://gitlab.qa.guru/users/sign_in' || true)"
  if [[ "${code}" == "200" ]] && grep -q '/users/auth/github' /tmp/gl-signin-smoke.html; then
    echo "OK: GitHub login button present"
    echo "Open: https://gitlab.qa.guru/users/sign_in"
    exit 0
  fi
  sleep 3
done

echo "ERROR: sign-in page did not expose /users/auth/github (last HTTP ${code:-none})" >&2
exit 1
