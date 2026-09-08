#!/usr/bin/env python3
"""P5 — GitLab CE OmniAuth openid_connect against https://auth.qa.guru + GitHub brokering.

Secrets stay in ~/.config (mode 600) and /etc/gitlab (root 600). Nothing here is printed.

  python3 deploy/oidc.py inventory              # gate: users + group members + tokens, off-box
  python3 deploy/oidc.py link-identities        # account linking by handle (dry run; --apply writes)
  python3 deploy/oidc.py seed-idp               # Keycloak side: gitlab client secret, github IdP
  python3 deploy/oidc.py configure              # gitlab.rb: openid_connect provider
  python3 deploy/oidc.py login-check            # end-to-end OIDC sign-in as pilot people
  python3 deploy/oidc.py disable-github-direct  # GitHub only through Keycloak
  python3 deploy/oidc.py verify                 # acceptance
  python3 deploy/oidc.py break-glass            # local root + machine traffic with IdP stopped
"""
from __future__ import annotations

import argparse
import html
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:
    certifi = None  # type: ignore[assignment]

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.qa.guru").rstrip("/")
AUTH_URL = os.environ.get("AUTH_URL", "https://auth.qa.guru").rstrip("/")
REALM = os.environ.get("REALM", "qaguru")
CLIENT_ID = "gitlab"
BOX3 = os.environ.get("GITLAB_SSH", "box3-zoo")
AUTH_ENV_FILE = Path(os.environ.get("AUTH_ENV", Path.home() / ".config/auth-qa-guru/keycloak.env"))
PILOT_ENV_FILE = Path(os.environ.get("PILOT_ENV", Path.home() / ".config/auth-qa-guru/pilot.env"))
ADMIN_ENV_FILE = Path(os.environ.get("GITLAB_ADMIN_ENV", Path.home() / ".config/gitlab/admin.env"))
RUNNER_ENV_FILE = Path(os.environ.get("GITLAB_RUNNER_ENV", Path.home() / ".config/gitlab/runner.env"))
INV_DIR = Path.home() / ".config/gitlab/p5-inventory"
ISSUER = f"{AUTH_URL}/realms/{REALM}"
WELL_KNOWN = f"{ISSUER}/.well-known/openid-configuration"
REDIRECT_URI = f"{GITLAB_URL}/users/auth/openid_connect/callback"
BROKER_CALLBACK = f"{AUTH_URL}/realms/{REALM}/broker/github/endpoint"
GITHUB_PLACEHOLDER = "github-oauth-app-not-configured"

# Break-glass: local root must survive every realm swap (ADR 017).
BREAK_GLASS = {"root"}

RUBY_INVENTORY = r"""
require 'json'

def iso(t)
  t.respond_to?(:iso8601) ? t.iso8601 : t&.to_s
end

users = User.order(:id).map do |u|
  {
    id: u.id,
    username: u.username,
    name: u.name,
    email: u.email,
    state: u.state,
    admin: u.admin?,
    external: (u.external? rescue nil),
    user_type: u.user_type.to_s,
    created_at: iso(u.created_at),
    last_sign_in_at: iso(u.last_sign_in_at),
    sign_in_count: u.sign_in_count,
    has_password: u.encrypted_password.present?,
    identities: u.identities.map { |i| { provider: i.provider, extern_uid: i.extern_uid } },
    secondary_emails: (u.emails.map(&:email) rescue []),
    active_pats: u.personal_access_tokens.where(revoked: false).count,
    ssh_keys: (u.keys.count rescue nil)
  }
end

def access_name(level)
  Gitlab::Access.human_access(level) rescue level.to_s
end

groups = Group.order(:id).map do |g|
  {
    id: g.id,
    full_path: g.full_path,
    visibility: g.visibility,
    member_count: g.members.size,
    members: g.members.map { |m|
      { user_id: m.user_id, username: m.user&.username, access_level: m.access_level,
        access: access_name(m.access_level) }
    }
  }
end

projects = Project.order(:id).map do |p|
  {
    id: p.id,
    full_path: p.full_path,
    visibility: p.visibility,
    member_count: p.project_members.size,
    members: p.project_members.map { |m|
      { user_id: m.user_id, username: m.user&.username, access_level: m.access_level,
        access: access_name(m.access_level) }
    }
  }
end

pats = PersonalAccessToken.order(:id).map do |t|
  { id: t.id, name: t.name, owner: t.user&.username, scopes: t.scopes,
    revoked: t.revoked, expires_at: t.expires_at&.to_s, last_used_at: iso(t.last_used_at) }
end

runners = Ci::Runner.order(:id).map do |r|
  { id: r.id, description: r.description, runner_type: r.runner_type,
    tags: (r.tag_list.to_a rescue []), token_prefix: r.token.to_s[0, 5],
    token_expires_at: r.token_expires_at&.to_s, contacted_at: iso(r.contacted_at),
    online: (r.online? rescue nil) }
end

deploy_tokens = (DeployToken.order(:id).map { |t|
  { id: t.id, name: t.name, revoked: t.revoked, expires_at: t.expires_at&.to_s }
} rescue [])

s = ApplicationSetting.current
settings = {
  signup_enabled: s.signup_enabled,
  require_admin_approval_after_user_signup: s.require_admin_approval_after_user_signup,
  password_authentication_enabled_for_web: s.password_authentication_enabled_for_web,
  password_authentication_enabled_for_git: s.password_authentication_enabled_for_git
}

omni = {
  providers: (Gitlab::Auth::OAuth::Provider.providers.map(&:to_s) rescue []),
  allow_single_sign_on: (Gitlab.config.omniauth.allow_single_sign_on rescue nil),
  auto_link_user: (Gitlab.config.omniauth.auto_link_user rescue nil),
  block_auto_created_users: (Gitlab.config.omniauth.block_auto_created_users rescue nil),
  enabled: (Gitlab.config.omniauth.enabled rescue nil)
}

identities = Identity.group(:provider).count

puts JSON.pretty_generate({
  captured_at: Time.now.utc.iso8601,
  gitlab_version: Gitlab::VERSION,
  url: Gitlab.config.gitlab.url,
  counts: {
    users: users.size,
    humans: users.count { |u| u[:user_type] == 'human' || u[:user_type].empty? },
    active: users.count { |u| u[:state] == 'active' },
    admins: users.count { |u| u[:admin] },
    groups: groups.size,
    projects: projects.size,
    active_pats: pats.count { |t| !t[:revoked] },
    runners: runners.size
  },
  identities_by_provider: identities,
  settings: settings,
  omniauth: omni,
  users: users,
  groups: groups,
  projects: projects,
  personal_access_tokens: pats,
  runners: runners,
  deploy_tokens: deploy_tokens
})
"""


def ssl_ctx() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def load_kv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def ssh(host: str, script: str, *, check: bool = True, timeout: int = 900) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, "bash", "-s"],
        input=script.encode(),
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace")
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:1200]
        raise SystemExit(f"ssh {host} failed ({proc.returncode}): {err or out[:600]}")
    return out


def rails(script: str, payload: Any = None) -> str:
    """Run a Ruby script through gitlab-rails runner on Box3 and return its stdout.

    `payload` is handed over as /tmp/p5-input.json so usernames never reach a shell.
    """
    remote = "set -euo pipefail\n"
    if payload is not None:
        remote += (
            "sudo tee /tmp/p5-input.json >/dev/null <<'JSON_EOF'\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\nJSON_EOF\n"
        )
    remote += (
        "sudo tee /tmp/p5-gitlab.rb >/dev/null <<'RUBY_EOF'\n"
        + script
        + "\nRUBY_EOF\n"
        "sudo gitlab-rails runner /tmp/p5-gitlab.rb\n"
        "sudo rm -f /tmp/p5-gitlab.rb /tmp/p5-input.json\n"
    )
    return ssh(BOX3, remote)


def rails_json(script: str, payload: Any = None) -> Any:
    out = rails(script, payload)
    start = out.find("{")
    end = out.rfind("}")
    if start < 0 or end < 0:
        raise SystemExit(f"gitlab-rails runner returned no JSON:\n{out[:800]}")
    return json.loads(out[start : end + 1])


def auth_env() -> dict[str, str]:
    env = load_kv(AUTH_ENV_FILE)
    missing = [k for k in ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD", "KC_CLIENT_SECRET_GITLAB") if not env.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} in {AUTH_ENV_FILE}")
    return env


def keycloak_token(env: dict[str, str]) -> str:
    form = urllib.parse.urlencode(
        {
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": env["KC_BOOTSTRAP_ADMIN_USERNAME"],
            "password": env["KC_BOOTSTRAP_ADMIN_PASSWORD"],
        }
    ).encode()
    req = urllib.request.Request(f"{AUTH_URL}/realms/master/protocol/openid-connect/token", data=form, method="POST")
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx()) as resp:
        return json.loads(resp.read())["access_token"]


def kc(method: str, path: str, token: str, body: Any = None) -> Any:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{AUTH_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx()) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code} {exc.read()[:300]!r}") from exc


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def classify(user: dict[str, Any]) -> tuple[str, str]:
    name = user["username"]
    providers = {i["provider"] for i in user["identities"]}
    if name in BREAK_GLASS:
        return "break-glass", "local admin; never federated; keeps password login"
    if user["user_type"] not in {"human", ""}:
        return "service", f"internal user_type={user['user_type']}; not a person"
    if "github" in providers:
        return "github-direct", "signs in through GitLab's own GitHub OmniAuth — must move to brokering"
    return "local", "local password; links to Keycloak by email on first OIDC sign-in"


def cmd_inventory() -> int:
    INV_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = rails_json(RUBY_INVENTORY)
    before = INV_DIR / "before.json"
    before.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    before.chmod(0o600)

    counts = data["counts"]
    print(f"GitLab {data['gitlab_version']} — {data['url']}")
    print(
        f"users={counts['users']} humans={counts['humans']} active={counts['active']} "
        f"admins={counts['admins']} groups={counts['groups']} projects={counts['projects']} "
        f"active_pats={counts['active_pats']} runners={counts['runners']}"
    )
    print(f"identities: {data['identities_by_provider'] or '{}'}")
    print(f"omniauth providers: {data['omniauth']['providers']} auto_link_user={data['omniauth']['auto_link_user']}")
    print()

    print("username → handle (existing username always wins — ADR 017)")
    rows = []
    for u in data["users"]:
        kind, note = classify(u)
        rows.append({"username": u["username"], "kind": kind, "note": note,
                     "identities": [i["provider"] for i in u["identities"]],
                     "state": u["state"], "admin": u["admin"], "email": u["email"]})
        print(f"  {u['username']:<24} {kind:<14} identities={[i['provider'] for i in u['identities']] or '[]'}  {note}")
    print()

    print("group membership (GitLab CE has no OIDC group sync — this stays manual)")
    for g in data["groups"]:
        members = ", ".join(f"{m['username']}:{m['access']}" for m in g["members"])
        print(f"  {g['full_path']:<24} members={g['member_count']}  [{members}]")
    for p in data["projects"]:
        if p["members"]:
            members = ", ".join(f"{m['username']}:{m['access']}" for m in p["members"])
            print(f"  project {p['full_path']:<16} members={p['member_count']}  [{members}]")
    print()

    print("tokens (realm swap does not touch them, but record owners anyway)")
    for t in data["personal_access_tokens"]:
        state = "revoked" if t["revoked"] else "active"
        print(f"  PAT  {t['name']:<26} owner={t['owner']:<12} {state:<8} expires={t['expires_at']} scopes={t['scopes']}")
    for r in data["runners"]:
        print(f"  runner {r['description']:<24} type={r['runner_type']} online={r['online']} token={r['token_prefix']}…")
    for t in data["deploy_tokens"]:
        print(f"  deploy-token {t['name']} revoked={t['revoked']}")
    print()

    duplicates = [r for r in rows if r["kind"] == "github-direct"]
    (INV_DIR / "username-map.json").write_text(
        json.dumps({"captured_at": data["captured_at"], "rows": rows, "github_direct": duplicates},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (INV_DIR / "username-map.json").chmod(0o600)
    print(f"snapshot  {before}")
    print(f"map       {INV_DIR / 'username-map.json'}")
    print(f"floor: users must not drop below {counts['users']} (humans {counts['humans']})")
    return 0


# ---------------------------------------------------------------------------
# account linking
# ---------------------------------------------------------------------------

RUBY_LINK = r"""
require 'json'

input = JSON.parse(File.read('/tmp/p5-input.json'))
apply = input['apply']
results = []

input['pairs'].each do |pair|
  username = pair['username']
  extern_uid = pair['extern_uid']
  user = User.find_by_username(username)
  if user.nil?
    results << { username: username, action: 'absent-in-gitlab' }
    next
  end

  existing = user.identities.find_by(provider: 'openid_connect')
  if existing
    action = existing.extern_uid == extern_uid ? 'already-linked' : 'CONFLICT-different-uid'
    results << { username: username, action: action, extern_uid: existing.extern_uid, wanted: extern_uid }
    next
  end

  taken = Identity.find_by(provider: 'openid_connect', extern_uid: extern_uid)
  if taken && taken.user_id != user.id
    results << { username: username, action: 'CONFLICT-uid-owned-by', owner: taken.user&.username }
    next
  end

  unless apply
    results << { username: username, action: 'would-link', extern_uid: extern_uid,
                 keeps: user.identities.map(&:provider) }
    next
  end

  identity = user.identities.build(provider: 'openid_connect', extern_uid: extern_uid)
  if identity.save
    results << { username: username, action: 'linked', extern_uid: extern_uid,
                 keeps: user.reload.identities.map(&:provider) }
  else
    results << { username: username, action: 'error', errors: identity.errors.full_messages }
  end
end

puts JSON.pretty_generate({
  apply: apply,
  results: results,
  identities_by_provider: Identity.group(:provider).count,
  user_count: User.count
})
"""


def cmd_link_identities(apply: bool = False) -> int:
    """Pre-seed openid_connect identities so GitLab matches by handle, never by email.

    find_by_email in GitLab 19 is `User.find_by(email:)` — the primary address only.
    GitLab's svasenkov carries the GitHub address, Keycloak carries the school one, so
    email matching would miss and JIT would mint a second account with a suffixed
    username. Linking on the handle keeps the existing account, its group membership
    and its history.
    """
    env = auth_env()
    token = keycloak_token(env)
    kc_users = kc("GET", f"/admin/realms/{REALM}/users?briefRepresentation=true&max=1000", token) or []
    handles = {u["username"] for u in kc_users}

    before_path = INV_DIR / "before.json"
    if not before_path.is_file():
        raise SystemExit(f"no inventory at {before_path} — run `inventory` first")
    before = json.loads(before_path.read_text(encoding="utf-8"))

    pairs = []
    for u in before["users"]:
        name = u["username"]
        if name in BREAK_GLASS:
            print(f"skip  {name:<14} break-glass — stays local, never federated")
            continue
        if u["user_type"] not in {"human", ""}:
            print(f"skip  {name:<14} service account (user_type={u['user_type']})")
            continue
        if name not in handles:
            print(f"skip  {name:<14} no Keycloak handle with this username")
            continue
        pairs.append({"username": name, "extern_uid": name})

    if not pairs:
        print("nothing to link")
        return 0

    data = rails_json(RUBY_LINK, {"apply": apply, "pairs": pairs})
    for r in data["results"]:
        marker = "FAIL" if str(r["action"]).startswith(("CONFLICT", "error")) else "OK  "
        print(f"{marker} {r['username']:<14} {r['action']} {r.get('extern_uid') or ''} {r.get('keeps') or ''}")
    print(f"identities now: {data['identities_by_provider']}  users={data['user_count']}")
    if not apply:
        print("\ndry run — re-run with --apply to write the links")
    bad = [r for r in data["results"] if str(r["action"]).startswith(("CONFLICT", "error"))]
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# Keycloak side
# ---------------------------------------------------------------------------


def sync_auth_env_to_host() -> None:
    """Mirror ~/.config/auth-qa-guru/keycloak.env to /etc/keycloak/keycloak.env (root 600)."""
    host = os.environ.get("AUTH_SSH", "auth-qa-guru")
    payload = AUTH_ENV_FILE.read_bytes()
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "sudo install -m 600 -o root -g root /dev/stdin /etc/keycloak/keycloak.env"],
        input=payload, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"failed to sync /etc/keycloak/keycloak.env: {proc.stderr.decode()[:300]}")
    print("host env: /etc/keycloak/keycloak.env refreshed (root 600)")


def cmd_seed_idp() -> int:
    env = auth_env()
    token = keycloak_token(env)

    clients = kc("GET", f"/admin/realms/{REALM}/clients?clientId={CLIENT_ID}", token) or []
    if not clients:
        raise SystemExit(f"client {CLIENT_ID} missing from realm {REALM}")
    client = clients[0]
    cid = client["id"]

    desired_redirects = sorted({REDIRECT_URI})
    changed = []
    if sorted(client.get("redirectUris") or []) != desired_redirects:
        client["redirectUris"] = desired_redirects
        changed.append("redirectUris")
    if not client.get("enabled"):
        client["enabled"] = True
        changed.append("enabled")
    if changed:
        kc("PUT", f"/admin/realms/{REALM}/clients/{cid}", token, client)
    print(f"client {CLIENT_ID}: {'updated ' + ','.join(changed) if changed else 'already correct'}")

    secret = kc("GET", f"/admin/realms/{REALM}/clients/{cid}/client-secret", token) or {}
    if secret.get("value") != env["KC_CLIENT_SECRET_GITLAB"]:
        raise SystemExit(
            "client secret on the server differs from KC_CLIENT_SECRET_GITLAB in "
            f"{AUTH_ENV_FILE} — realm was imported with a different env file; refusing to guess"
        )
    print("client secret: matches keycloak.env")

    mappers = kc("GET", f"/admin/realms/{REALM}/clients/{cid}/protocol-mappers/models", token) or []
    names = {m["name"] for m in mappers}
    print(f"protocol mappers: {sorted(names)}")

    gh_id = env.get("KC_GITHUB_CLIENT_ID", "")
    gh_secret = env.get("KC_GITHUB_CLIENT_SECRET", "")
    configured = bool(gh_id) and gh_id != GITHUB_PLACEHOLDER
    idps = kc("GET", f"/admin/realms/{REALM}/identity-provider/instances", token) or []
    github = next((i for i in idps if i["alias"] == "github"), None)
    if github is None:
        raise SystemExit("github IdP missing from realm — realm file drifted")

    if configured:
        github["enabled"] = True
        github["config"]["clientId"] = gh_id
        github["config"]["clientSecret"] = gh_secret
        kc("PUT", f"/admin/realms/{REALM}/identity-provider/instances/github", token, github)
        print(f"github IdP: enabled with real OAuth App; callback {BROKER_CALLBACK}")
        # The live config lives in the Keycloak DB, but a disaster re-import reads the host
        # env file — leave the placeholder there and brokering silently dies on restore.
        sync_auth_env_to_host()
    else:
        if github.get("enabled"):
            github["enabled"] = False
            kc("PUT", f"/admin/realms/{REALM}/identity-provider/instances/github", token, github)
            print("github IdP: DISABLED — credentials are still the placeholder, a live button would 404 for everyone")
        else:
            print("github IdP: already disabled (no OAuth App yet)")
        print(f"      to finish brokering: OAuth App callback {BROKER_CALLBACK}")
        print(f"      then set KC_GITHUB_CLIENT_ID / KC_GITHUB_CLIENT_SECRET in {AUTH_ENV_FILE},")
        print("      push it to the host and re-run seed-idp.")
    return 0


# ---------------------------------------------------------------------------
# GitLab side
# ---------------------------------------------------------------------------

BEGIN = "# BEGIN openid_connect omniauth (managed by deploy/oidc.py)"
END = "# END openid_connect omniauth (managed by deploy/oidc.py)"

REMOTE_PATCH = r'''
import json
import re
from pathlib import Path

env = {}
for line in Path("/etc/gitlab/oidc.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k] = v

secret = env["GITLAB_OIDC_CLIENT_SECRET"]
issuer = env["GITLAB_OIDC_ISSUER"]
client_id = env["GITLAB_OIDC_CLIENT_ID"]
redirect = env["GITLAB_OIDC_REDIRECT_URI"]
keep_github = env.get("GITLAB_KEEP_GITHUB", "true") == "true"

gh_env = {}
gh_path = Path("/etc/gitlab/github-oauth.env")
if gh_path.is_file():
    for line in gh_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            gh_env[k] = v

providers = []
if keep_github and gh_env.get("GITHUB_APP_ID"):
    providers.append(
        "  {\n"
        "    'name' => 'github',\n"
        "    'app_id' => '%s',\n"
        "    'app_secret' => '%s',\n"
        "    'args' => { 'scope' => 'user:email' }\n"
        "  }" % (gh_env["GITHUB_APP_ID"], gh_env["GITHUB_APP_SECRET"])
    )
providers.append(
    "  {\n"
    "    'name' => 'openid_connect',\n"
    "    'label' => 'QA Guru',\n"
    "    'args' => {\n"
    "      'name' => 'openid_connect',\n"
    "      'scope' => ['openid', 'profile', 'email'],\n"
    "      'response_type' => 'code',\n"
    "      'issuer' => '%s',\n"
    "      'discovery' => true,\n"
    "      'client_auth_method' => 'basic',\n"
    "      'uid_field' => 'preferred_username',\n"
    "      'gitlab_username_claim' => 'preferred_username',\n"
    "      'send_scope_to_token_endpoint' => 'false',\n"
    "      'pkce' => true,\n"
    "      'client_options' => {\n"
    "        'identifier' => '%s',\n"
    "        'secret' => '%s',\n"
    "        'redirect_uri' => '%s'\n"
    "      }\n"
    "    }\n"
    "  }" % (issuer, client_id, secret, redirect)
)

sso = ["'openid_connect'"]
link = ["'openid_connect'"]
if keep_github and gh_env.get("GITHUB_APP_ID"):
    sso.insert(0, "'github'")
    link.insert(0, "'github'")

begin = "# BEGIN openid_connect omniauth (managed by deploy/oidc.py)"
end = "# END openid_connect omniauth (managed by deploy/oidc.py)"
block = "\n".join([
    begin,
    "gitlab_rails['omniauth_enabled'] = true",
    "gitlab_rails['omniauth_allow_single_sign_on'] = [%s]" % ", ".join(sso),
    "gitlab_rails['omniauth_block_auto_created_users'] = false",
    "gitlab_rails['omniauth_auto_link_user'] = [%s]" % ", ".join(link),
    "gitlab_rails['omniauth_providers'] = [",
    ",\n".join(providers),
    "]",
    end,
    "",
])

rb = Path("/etc/gitlab/gitlab.rb")
text = rb.read_text(encoding="utf-8")

# Retire the old GitHub-only block written by enable-github-omniauth.sh: its
# omniauth_providers assignment would otherwise overwrite ours.
old_begin = "# BEGIN github omniauth (managed)"
old_end = "# END github omniauth (managed)"
if old_begin in text and old_end in text:
    pre, rest = text.split(old_begin, 1)
    _, post = rest.split(old_end, 1)
    text = pre.rstrip() + "\n" + post.lstrip("\n")

if begin in text and end in text:
    pre, rest = text.split(begin, 1)
    _, post = rest.split(end, 1)
    text = pre.rstrip() + "\n\n" + block + post.lstrip("\n")
else:
    text = text.rstrip() + "\n\n" + block

rb.write_text(text, encoding="utf-8")
rb.chmod(0o600)
print(json.dumps({"bytes": rb.stat().st_size, "github_kept": bool(keep_github and gh_env.get("GITHUB_APP_ID"))}))
'''


def push_oidc_env(keep_github: bool) -> None:
    env = auth_env()
    payload = "\n".join(
        [
            "# P5 — GitLab OmniAuth openid_connect. root 600, not git, not Vault (ADR 017 §12).",
            f"GITLAB_OIDC_ISSUER={ISSUER}",
            f"GITLAB_OIDC_CLIENT_ID={CLIENT_ID}",
            f"GITLAB_OIDC_CLIENT_SECRET={env['KC_CLIENT_SECRET_GITLAB']}",
            f"GITLAB_OIDC_REDIRECT_URI={REDIRECT_URI}",
            f"GITLAB_KEEP_GITHUB={'true' if keep_github else 'false'}",
            "",
        ]
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", BOX3, "sudo tee /etc/gitlab/oidc.env >/dev/null && sudo chmod 600 /etc/gitlab/oidc.env"],
        input=payload.encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"failed to upload /etc/gitlab/oidc.env: {proc.stderr.decode()[:400]}")


def reconfigure() -> None:
    print("==> gitlab-ctl reconfigure (a few minutes)")
    out = ssh(BOX3, "set -euo pipefail\nsudo gitlab-ctl reconfigure 2>&1 | tail -5\n", timeout=1800)
    print("   " + out.strip().replace("\n", "\n   "))


def wait_signin(expect: set[str], absent: set[str] = frozenset(), timeout: int = 240) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{GITLAB_URL}/users/sign_in")
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as resp:
                page = resp.read().decode("utf-8", "replace")
                code = resp.status
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            code, page = getattr(exc, "code", 0), ""
        found = {p for p in ("github", "openid_connect") if f"/users/auth/{p}" in page}
        last = {"code": code, "providers": sorted(found)}
        if code == 200 and expect <= found and not (absent & found):
            return last
        time.sleep(5)
    return last


def cmd_configure(keep_github: bool = False) -> int:
    print(f"==> backup /etc/gitlab/gitlab.rb on {BOX3}")
    stamp = ssh(BOX3, "set -euo pipefail\nts=$(date -u +%Y%m%dT%H%M%SZ)\nsudo cp -a /etc/gitlab/gitlab.rb /etc/gitlab/.p5-gitlab.rb.$ts\necho $ts\n").strip()
    print(f"   /etc/gitlab/.p5-gitlab.rb.{stamp}")
    local_backup = INV_DIR / f"gitlab.rb.before.{stamp}"
    INV_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    rb = ssh(BOX3, "sudo cat /etc/gitlab/gitlab.rb\n")
    local_backup.write_text(rb, encoding="utf-8")
    local_backup.chmod(0o600)
    print(f"   off-box copy {local_backup}")

    print("==> upload /etc/gitlab/oidc.env (root 600)")
    push_oidc_env(keep_github)

    print("==> patch /etc/gitlab/gitlab.rb")
    remote = (
        "set -euo pipefail\n"
        "sudo tee /tmp/p5-patch.py >/dev/null <<'PY_EOF'\n" + REMOTE_PATCH + "\nPY_EOF\n"
        "sudo python3 /tmp/p5-patch.py\n"
        "sudo rm -f /tmp/p5-patch.py\n"
    )
    print("   " + ssh(BOX3, remote).strip())

    reconfigure()

    expect = {"openid_connect"} | ({"github"} if keep_github else set())
    absent = set() if keep_github else {"github"}
    state = wait_signin(expect, absent)
    if state.get("code") != 200 or not expect <= set(state.get("providers", [])):
        raise SystemExit(f"sign-in page did not settle: {state}")
    print(f"OK  sign-in page {state['code']}, providers {state['providers']}")
    return 0


def cmd_disable_github_direct() -> int:
    """GitHub stops being a GitLab provider: the only way in is through Keycloak."""
    env = auth_env()
    if env.get("KC_GITHUB_CLIENT_ID", GITHUB_PLACEHOLDER) == GITHUB_PLACEHOLDER:
        raise SystemExit(
            "refusing: Keycloak has no GitHub OAuth App yet, so dropping GitLab's direct GitHub\n"
            "provider would leave GitHub users with no route at all. Configure brokering first."
        )
    return cmd_configure(keep_github=False)


# ---------------------------------------------------------------------------
# login checks
# ---------------------------------------------------------------------------


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._current = {"action": ad.get("action", ""), "method": ad.get("method", "get").lower(),
                             "id": ad.get("id", ""), "inputs": {}}
            self.forms.append(self._current)
        elif tag in {"input", "button"} and self._current is not None:
            name = ad.get("name")
            if name:
                self._current["inputs"][name] = ad.get("value", "")


def parse_forms(page: str) -> list[dict[str, Any]]:
    parser = FormParser()
    parser.feed(page)
    return parser.forms


def _join(base: str, action: str) -> str:
    return urllib.parse.urljoin(base, html.unescape(action))


class Session:
    """Cookie-jar browser good enough for OmniAuth redirects."""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx()),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.opener.addheaders = [("User-Agent", "qa-guru-p5-oidc/1.0")]

    def fetch(self, url: str, data: bytes | None = None) -> tuple[str, str, int]:
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        try:
            with self.opener.open(req, timeout=45) as resp:
                return resp.geturl(), resp.read().decode("utf-8", "replace"), resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            loc = exc.headers.get("Location")
            if exc.code in {301, 302, 303, 307, 308} and loc:
                return self.fetch(_join(url, loc), None)
            return url, body, exc.code

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def api(self, path: str) -> tuple[int, Any]:
        req = urllib.request.Request(f"{GITLAB_URL}{path}", headers={"Cookie": self.cookie_header()})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, None


def start_provider(sess: Session, provider: str) -> tuple[str, str, int]:
    """GitLab only accepts POST for the OmniAuth request phase."""
    url, page, _ = sess.fetch(f"{GITLAB_URL}/users/sign_in")
    forms = parse_forms(page)
    form = next((f for f in forms if f"/users/auth/{provider}" in f.get("action", "")), None)
    if form is None:
        raise SystemExit(f"no {provider} button on the sign-in page")
    payload = urllib.parse.urlencode(form["inputs"]).encode()
    return sess.fetch(_join(url, form["action"]), payload)


def keycloak_login(sess: Session, url: str, page: str, username: str, password: str) -> tuple[str, str, int]:
    status = 0
    for _ in range(6):
        forms = parse_forms(page)
        login = next((f for f in forms if f.get("id") == "kc-form-login" or "username" in f["inputs"]), None)
        if login is None:
            break
        payload = dict(login["inputs"])
        payload["username"] = username
        payload["password"] = password
        payload.setdefault("credentialId", "")
        url, page, status = sess.fetch(_join(url, login["action"]), urllib.parse.urlencode(payload).encode())
    return url, page, status


def oidc_login(username: str, password: str) -> dict[str, Any]:
    sess = Session()
    url, page, status = start_provider(sess, "openid_connect")
    url, page, status = keycloak_login(sess, url, page, username, password)
    code, me = sess.api("/api/v4/user")
    return {
        "username": username,
        "landed": url,
        "status": status,
        "api_code": code,
        "gitlab_username": (me or {}).get("username"),
        "gitlab_id": (me or {}).get("id"),
        "is_admin": (me or {}).get("is_admin"),
        "identities": (me or {}).get("identities"),
    }


def password_login(username: str, password: str) -> dict[str, Any]:
    sess = Session()
    url, page, _ = sess.fetch(f"{GITLAB_URL}/users/sign_in")
    forms = parse_forms(page)
    form = next((f for f in forms if "user[login]" in f["inputs"] or "user[password]" in f["inputs"]), None)
    if form is None:
        return {"username": username, "error": "no password form on sign-in page"}
    payload = dict(form["inputs"])
    payload["user[login]"] = username
    payload["user[password]"] = password
    payload["user[remember_me]"] = "0"
    url, page, status = sess.fetch(_join(url, form["action"] or "/users/sign_in"), urllib.parse.urlencode(payload).encode())
    code, me = sess.api("/api/v4/user")
    return {
        "username": username,
        "landed": url,
        "status": status,
        "api_code": code,
        "gitlab_username": (me or {}).get("username"),
        "is_admin": (me or {}).get("is_admin"),
    }


def pilot_people() -> list[tuple[str, str, str]]:
    env = load_kv(PILOT_ENV_FILE)
    out = []
    for kind, u, p in (
        ("staff", "PILOT_STAFF_USERNAME", "PILOT_STAFF_PASSWORD"),
        ("students", "PILOT_STUDENT_USERNAME", "PILOT_STUDENT_PASSWORD"),
    ):
        if env.get(u) and env.get(p):
            out.append((kind, env[u], env[p]))
    if not out:
        raise SystemExit(f"no pilot credentials in {PILOT_ENV_FILE}")
    return out


def cmd_login_check() -> int:
    rc = 0
    for kind, user, password in pilot_people():
        res = oidc_login(user, password)
        ok = res["api_code"] == 200 and res["gitlab_username"]
        print(f"{'OK ' if ok else 'FAIL'} {kind:<9} {user} → gitlab user={res['gitlab_username']} "
              f"admin={res['is_admin']} api={res['api_code']}")
        if res.get("identities"):
            print(f"     identities={res['identities']}")
        if not ok:
            print(f"     landed={res['landed']} status={res['status']}")
            rc = 1
    return rc


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


def cmd_verify() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    before_path = INV_DIR / "before.json"
    if not before_path.is_file():
        raise SystemExit(f"no inventory at {before_path} — run `inventory` first")
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = rails_json(RUBY_INVENTORY)
    (INV_DIR / "after.json").write_text(json.dumps(after, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (INV_DIR / "after.json").chmod(0o600)

    b, a = before["counts"], after["counts"]
    check("users did not decrease", a["users"] >= b["users"], f"{b['users']} → {a['users']}")
    check("humans did not decrease", a["humans"] >= b["humans"], f"{b['humans']} → {a['humans']}")
    check("admins did not decrease", a["admins"] >= b["admins"], f"{b['admins']} → {a['admins']}")

    before_names = {u["username"] for u in before["users"]}
    after_names = {u["username"] for u in after["users"]}
    lost = sorted(before_names - after_names)
    check("no username disappeared", not lost, ", ".join(lost) if lost else "all present")

    before_members = {
        g["full_path"]: {(m["username"], m["access_level"]) for m in g["members"]} for g in before["groups"]
    }
    after_members = {
        g["full_path"]: {(m["username"], m["access_level"]) for m in g["members"]} for g in after["groups"]
    }
    dropped = {g: sorted(m[0] for m in before_members[g] - after_members.get(g, set())) for g in before_members}
    dropped = {g: v for g, v in dropped.items() if v}
    check("group membership intact", not dropped, str(dropped) if dropped else "no member lost")

    providers = set(after["omniauth"]["providers"])
    check("openid_connect enabled", "openid_connect" in providers, str(sorted(providers)))

    root = next((u for u in after["users"] if u["username"] in BREAK_GLASS), None)
    check("break-glass root still local", bool(root and root["has_password"]),
          "password login preserved" if root and root["has_password"] else "root lost its password")
    check("password auth for web on", bool(after["settings"]["password_authentication_enabled_for_web"]))
    check("password auth for git on", bool(after["settings"]["password_authentication_enabled_for_git"]))

    b_pats = {t["id"] for t in before["personal_access_tokens"] if not t["revoked"]}
    a_pats = {t["id"] for t in after["personal_access_tokens"] if not t["revoked"]}
    check("active PATs survived", b_pats <= a_pats, f"{len(b_pats)} → {len(a_pats)}")
    b_runners = {r["id"] for r in before["runners"]}
    a_runners = {r["id"] for r in after["runners"]}
    check("runners survived", b_runners <= a_runners, f"{len(b_runners)} → {len(a_runners)}")
    online = [r["description"] for r in after["runners"] if r["online"]]
    check("runners online", len(online) == len(a_runners), f"{online}")

    state = wait_signin({"openid_connect"}, timeout=30)
    check("sign-in offers Keycloak", state.get("code") == 200 and "openid_connect" in state.get("providers", []), str(state))

    env = auth_env()
    brokered = env.get("KC_GITHUB_CLIENT_ID", GITHUB_PLACEHOLDER) != GITHUB_PLACEHOLDER
    if brokered:
        check("GitHub not a direct GitLab provider", "github" not in state.get("providers", []),
              "GitHub goes through Keycloak")
        token = keycloak_token(env)
        idps = kc("GET", f"/admin/realms/{REALM}/identity-provider/instances", token) or []
        gh = next((i for i in idps if i["alias"] == "github"), None)
        check("github IdP enabled in Keycloak", bool(gh and gh.get("enabled")))
    else:
        token = keycloak_token(env)
        idps = kc("GET", f"/admin/realms/{REALM}/identity-provider/instances", token) or []
        gh = next((i for i in idps if i["alias"] == "github"), None)
        check("no dead GitHub button on the IdP", not (gh and gh.get("enabled")),
              "placeholder credentials must not be exposed as a live button")
        print("WARN  GitHub brokering not finished — OAuth App still missing (P5 tail)")

    for kind, user, password in pilot_people():
        res = oidc_login(user, password)
        check(f"OIDC sign-in {kind} ({user})", res["api_code"] == 200 and bool(res["gitlab_username"]),
              f"gitlab user={res['gitlab_username']}")

    print()
    print("FAILED: " + ", ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# break-glass
# ---------------------------------------------------------------------------


def _docker_keycloak(action: str) -> subprocess.CompletedProcess[str]:
    container = os.environ.get("KEYCLOAK_CONTAINER", "auth-qa-guru-keycloak-1")
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", os.environ.get("AUTH_SSH", "auth-qa-guru"),
         f"sudo docker {action} {container}"],
        capture_output=True, text=True, check=False,
    )


def cmd_break_glass() -> int:
    admin = load_kv(ADMIN_ENV_FILE)
    if not admin.get("GITLAB_ROOT_USER") or not admin.get("GITLAB_ROOT_PASSWORD"):
        raise SystemExit(f"missing GITLAB_ROOT_USER / GITLAB_ROOT_PASSWORD in {ADMIN_ENV_FILE}")
    runner_tokens = [v for k, v in load_kv(RUNNER_ENV_FILE).items() if k.startswith("GITLAB_RUNNER_AUTH_TOKEN")]

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    print("==> stop Keycloak container (docker stop — the unit is oneshot, systemctl is a no-op)")
    stopped = _docker_keycloak("stop")
    if stopped.returncode != 0:
        raise SystemExit(f"could not stop Keycloak: {stopped.stderr[:300]}")
    try:
        time.sleep(3)
        res = password_login(admin["GITLAB_ROOT_USER"], admin["GITLAB_ROOT_PASSWORD"])
        check("local root signs in with IdP down",
              res.get("api_code") == 200 and res.get("gitlab_username") == admin["GITLAB_ROOT_USER"],
              f"api={res.get('api_code')} user={res.get('gitlab_username')}")

        for i, token in enumerate(runner_tokens, start=1):
            body = urllib.parse.urlencode({"token": token}).encode()
            req = urllib.request.Request(f"{GITLAB_URL}/api/v4/runners/verify", data=body, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            check(f"runner token {i} still valid with IdP down", code in {200, 201}, f"HTTP {code}")

        sess = Session()
        url, page, _ = sess.fetch(f"{GITLAB_URL}/users/sign_in")
        check("sign-in page still served", "user[login]" in page, "password form present")
    finally:
        print("==> start Keycloak back")
        started = _docker_keycloak("start")
        if started.returncode != 0:
            failures.append("keycloak did not restart")
            print("FAIL  Keycloak did not start back — ssh auth-qa-guru 'sudo docker start auth-qa-guru-keycloak-1'")

    for _ in range(30):
        try:
            req = urllib.request.Request(WELL_KNOWN)
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx()) as resp:
                if resp.status == 200:
                    print("OK   Keycloak discovery back up")
                    break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(4)
    else:
        failures.append("keycloak discovery did not come back")

    print()
    print("FAILED: " + ", ".join(failures) if failures else "break-glass verified")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        choices=["inventory", "link-identities", "seed-idp", "configure", "login-check",
                 "disable-github-direct", "verify", "break-glass"],
    )
    parser.add_argument("--apply", action="store_true", help="link-identities: actually write the links")
    args = parser.parse_args()
    if args.command == "link-identities":
        return cmd_link_identities(apply=args.apply)
    return {
        "inventory": cmd_inventory,
        "seed-idp": cmd_seed_idp,
        "configure": cmd_configure,
        "login-check": cmd_login_check,
        "disable-github-direct": cmd_disable_github_direct,
        "verify": cmd_verify,
        "break-glass": cmd_break_glass,
    }[args.command]()


if __name__ == "__main__":
    sys.exit(main())
