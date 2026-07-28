#!/usr/bin/env bash
# Install GitLab Runner(s) on Box 2 — Docker executor, Selenoid access.
# GitLab 19+: runner authentication tokens (glrt-), not registration tokens.
#
# Secrets (local, not in git):
#   ~/.config/gitlab/admin.env   — GITLAB_URL, GITLAB_ROOT_USER, GITLAB_ROOT_PASSWORD
#   ~/.config/gitlab/runner.env  — GITLAB_RUNNER_AUTH_TOKEN_1, _2, … (written by this script)
#
# Usage:
#   ./install-runner-box2.sh              # idempotent install / register missing managers
#   ./install-runner-box2.sh --migrate    # replace legacy glrtr- runners with glrt- auth tokens
#   ./install-runner-box2.sh --bootstrap-env  # copy tokens from Box2 config.toml → runner.env
set -euo pipefail

BOX2="${BOX2_CI:-89.248.193.83}"
BOX3_HOST="${GITLAB_DEPLOY_HOST:-box3}"
KEY="${GITLAB_DEPLOY_KEY:-$HOME/.ssh/qa_guru_ed25519}"
SSH_BOX2=(ssh -i "${KEY}" -o IdentitiesOnly=yes "qaguru@${BOX2}")
SSH_BOX3=(ssh -o ConnectTimeout=15 "${BOX3_HOST}")

ADMIN_ENV="${HOME}/.config/gitlab/admin.env"
RUNNER_ENV="${HOME}/.config/gitlab/runner.env"

MODE="${1:-install}"
RUNNER_COUNT="${GITLAB_RUNNER_COUNT:-2}"
RUNNER_TAGS=(docker java21 selenoid box2)
RUNNER_TAG_CSV="docker,java21,selenoid,box2"

if [[ -f "${ADMIN_ENV}" ]]; then
  # shellcheck source=/dev/null
  source "${ADMIN_ENV}"
fi
if [[ -f "${RUNNER_ENV}" ]]; then
  # shellcheck source=/dev/null
  source "${RUNNER_ENV}"
fi

: "${GITLAB_URL:?Set GITLAB_URL in ~/.config/gitlab/admin.env}"
RUNNER_GITLAB_URL="${GITLAB_RUNNER_URL:-${GITLAB_URL}}"

runner_name() {
  echo "box2-docker-${1}"
}

runner_token_var() {
  echo "GITLAB_RUNNER_AUTH_TOKEN_${1}"
}

get_runner_token() {
  local idx="$1"
  local var
  var="$(runner_token_var "${idx}")"
  printf '%s' "${!var:-}"
}

set_runner_token_in_env() {
  local idx="$1"
  local token="$2"
  local var
  var="$(runner_token_var "${idx}")"
  mkdir -p "$(dirname "${RUNNER_ENV}")"
  touch "${RUNNER_ENV}"
  chmod 600 "${RUNNER_ENV}"
  if grep -q "^${var}=" "${RUNNER_ENV}" 2>/dev/null; then
    if [[ "$(uname)" == "Darwin" ]]; then
      sed -i '' "s|^${var}=.*|${var}=${token}|" "${RUNNER_ENV}"
    else
      sed -i "s|^${var}=.*|${var}=${token}|" "${RUNNER_ENV}"
    fi
  else
    printf '%s=%s\n' "${var}" "${token}" >>"${RUNNER_ENV}"
  fi
  # shellcheck disable=SC2163
  export "${var}=${token}"
}

bootstrap_runner_env_from_box2() {
  echo "==> bootstrap runner.env from Box2 /etc/gitlab-runner/config.toml"
  local tmp
  tmp="$(mktemp)"
  "${SSH_BOX2[@]}" 'sudo cat /etc/gitlab-runner/config.toml' >"${tmp}"
  python - "${tmp}" "${RUNNER_ENV}" "${RUNNER_COUNT}" <<'PY'
import re, sys, os

config_path, env_path, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
text = open(config_path, encoding="utf-8").read()
blocks = re.split(r"\n(?=\[\[runners\]\])", text)
tokens = {}
for block in blocks:
    m_name = re.search(r'^\s*name\s*=\s*"([^"]+)"', block, re.M)
    m_token = re.search(r'^\s*token\s*=\s*"([^"]+)"', block, re.M)
    if not m_name or not m_token:
        continue
    name = m_name.group(1)
    if re.fullmatch(r"box2-docker-\d+", name):
        tokens[name] = m_token.group(1)

lines = []
if os.path.isfile(env_path):
    lines = open(env_path, encoding="utf-8").read().splitlines()
    lines = [ln for ln in lines if not ln.startswith("GITLAB_RUNNER_AUTH_TOKEN_")]

for i in range(1, count + 1):
    name = f"box2-docker-{i}"
    token = tokens.get(name)
    if not token:
        print(f"WARN: no token in config.toml for {name}", file=sys.stderr)
        continue
    lines.append(f"GITLAB_RUNNER_AUTH_TOKEN_{i}={token}")

header = [
    "# ~/.config/gitlab/runner.env — not in git",
    "# Runner authentication tokens for box2-docker-N (glrt- / glrtr-).",
    "# Written by install-runner-box2.sh",
]
out = header + ([""] if lines else []) + lines
os.makedirs(os.path.dirname(env_path), exist_ok=True)
open(env_path, "w", encoding="utf-8").write("\n".join(out).rstrip() + "\n")
os.chmod(env_path, 0o600)
print(f"OK: {env_path} ({sum(1 for ln in lines if ln.startswith('GITLAB_RUNNER_AUTH_TOKEN_'))} token(s))")
PY
  rm -f "${tmp}"
  # shellcheck source=/dev/null
  source "${RUNNER_ENV}"
}

create_runner_on_gitlab() {
  local idx="$1"
  local name
  name="$(runner_name "${idx}")"
  echo "==> create runner ${name} on GitLab (Box3 CreateRunnerService)"
  local out token
  out="$("${SSH_BOX3[@]}" "sudo gitlab-rails runner - ${name} ${RUNNER_TAG_CSV}" <<'RUBY'
name = ARGV[0]
tag_csv = ARGV[1]
tags = tag_csv.split(',')

legacy = Ci::Runner.find_by(description: name)
if legacy
  if legacy.registration_type == 'authenticated_user'
    puts "EXISTS_AUTH"
    exit 0
  end
  puts "EXISTS_LEGACY"
  exit 0
end

user = User.find_by(username: 'root') || User.admins.first
abort('no admin user') unless user

result = Ci::Runners::CreateRunnerService.new(
  user: user,
  params: {
    runner_type: 'instance_type',
    description: name,
    tag_list: tags,
    run_untagged: false,
    locked: false,
    access_level: 'not_protected'
  }
).execute

if result.error?
  abort(result.message.to_s)
end

runner = result.payload[:runner]
puts "TOKEN=#{runner.token}"
puts "ID=#{runner.id}"
RUBY
)"
  if grep -q '^EXISTS_AUTH$' <<<"${out}"; then
    echo "Runner ${name} already exists (authenticated_user); need token in runner.env"
    return 1
  fi
  if grep -q '^EXISTS_LEGACY$' <<<"${out}"; then
    echo "Runner ${name} is legacy (registration_token); run with --migrate"
    return 1
  fi
  token="$(grep '^TOKEN=' <<<"${out}" | cut -d= -f2-)"
  if [[ -z "${token}" ]]; then
    echo "FAIL: no token from CreateRunnerService for ${name}"
    echo "${out}"
    return 1
  fi
  set_runner_token_in_env "${idx}" "${token}"
  echo "OK: ${name} id=$(grep '^ID=' <<<"${out}" | cut -d= -f2-) token prefix=${token:0:5}…"
}

delete_legacy_runners_on_gitlab() {
  echo "==> delete legacy registration_token runners on GitLab"
  "${SSH_BOX3[@]}" "sudo gitlab-rails runner - ${RUNNER_COUNT}" <<'RUBY'
count = ARGV[0].to_i
(1..count).each do |i|
  name = "box2-docker-#{i}"
  r = Ci::Runner.find_by(description: name)
  next unless r
  next if r.registration_type == 'authenticated_user'
  puts "delete id=#{r.id} name=#{name} type=#{r.registration_type}"
  r.destroy!
end
RUBY
}

ensure_box2_runner_package() {
  "${SSH_BOX2[@]}" "bash -s" <<'REMOTE'
set -euo pipefail
grep -q 'gitlab.qa.guru' /etc/hosts || echo '89.248.193.83 gitlab.qa.guru' | sudo tee -a /etc/hosts
if ! command -v gitlab-runner >/dev/null 2>&1; then
  curl -fsSL https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh | sudo bash
  sudo apt-get install -y gitlab-runner
fi
sudo usermod -aG docker gitlab-runner 2>/dev/null || true
sudo gitlab-runner install --user=gitlab-runner --working-directory=/home/gitlab-runner 2>/dev/null || true
REMOTE
}

register_runner_on_box2() {
  local idx="$1"
  local name token
  name="$(runner_name "${idx}")"
  token="$(get_runner_token "${idx}")"
  : "${token:?Set GITLAB_RUNNER_AUTH_TOKEN_${idx} in ~/.config/gitlab/runner.env or re-run install}"

  if "${SSH_BOX2[@]}" "sudo grep -qF 'name = \"${name}\"' /etc/gitlab-runner/config.toml"; then
    echo "Runner ${name} already in config.toml, skip register"
    return 0
  fi

  echo "==> register ${name} on Box2 (--token)"
  "${SSH_BOX2[@]}" "bash -s" <<REMOTE
set -euo pipefail
sudo gitlab-runner register --non-interactive \\
  --url '${RUNNER_GITLAB_URL}' \\
  --token '${token}' \\
  --executor docker \\
  --docker-image eclipse-temurin:21-jdk \\
  --description '${name}' \\
  --docker-privileged=false \\
  --docker-volumes "/cache" \\
  --docker-extra-hosts "selenoid.qa.guru:89.248.192.30"
REMOTE
}

finalize_box2_config() {
  "${SSH_BOX2[@]}" "bash -s" <<'REMOTE'
set -euo pipefail
sudo sed -i 's/^concurrent = .*/concurrent = 3/' /etc/gitlab-runner/config.toml
sudo sed -i 's|url = "http://212.92.101.15:8181"|url = "https://gitlab.qa.guru"|g' /etc/gitlab-runner/config.toml
sudo systemctl restart gitlab-runner
sudo gitlab-runner verify
REMOTE
}

remove_runner_from_box2_config() {
  local name="$1"
  echo "==> remove ${name} from Box2 config.toml"
  "${SSH_BOX2[@]}" "bash -s" "${name}" <<'REMOTE'
set -euo pipefail
name="$1"
sudo python3 - "${name}" <<'PY'
import re, sys
name = sys.argv[1]
path = "/etc/gitlab-runner/config.toml"
text = open(path, encoding="utf-8").read()
parts = re.split(r"\n(?=\[\[runners\]\])", text)
header = parts[0]
kept = [header.rstrip()]
for part in parts[1:]:
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', part, re.M)
    if m and m.group(1) == name:
        continue
    kept.append(part.strip())
open(path, "w", encoding="utf-8").write("\n".join(kept).rstrip() + "\n")
PY
sudo chmod 600 /etc/gitlab-runner/config.toml
REMOTE
}

migrate_legacy_runners() {
  echo "==> migrate legacy registration_token → runner authentication tokens (glrt-)"
  ensure_box2_runner_package

  for i in $(seq 1 "${RUNNER_COUNT}"); do
    local name
    name="$(runner_name "${i}")"
    remove_runner_from_box2_config "${name}"
  done

  delete_legacy_runners_on_gitlab

  for i in $(seq 1 "${RUNNER_COUNT}"); do
    create_runner_on_gitlab "${i}"
  done

  for i in $(seq 1 "${RUNNER_COUNT}"); do
    register_runner_on_box2 "${i}"
  done

  finalize_box2_config
  echo "Migration complete. Tokens saved in ${RUNNER_ENV}"
}

install_runners() {
  ensure_box2_runner_package

  for i in $(seq 1 "${RUNNER_COUNT}"); do
    local name token
    name="$(runner_name "${i}")"
    token="$(get_runner_token "${i}")"

    if [[ -z "${token}" ]]; then
      if ! create_runner_on_gitlab "${i}"; then
        echo "Hint: ./install-runner-box2.sh --bootstrap-env  OR  --migrate"
        exit 1
      fi
    fi
    register_runner_on_box2 "${i}"
  done

  finalize_box2_config
  echo "Runners on Box2 ready."
}

case "${MODE}" in
  --bootstrap-env)
    bootstrap_runner_env_from_box2
    ;;
  --migrate|--migrate-legacy)
    migrate_legacy_runners
    ;;
  install|--install|"")
    install_runners
    ;;
  *)
    echo "Usage: $0 [--migrate | --bootstrap-env]"
    exit 1
    ;;
esac
