# Deploy gitlab.qa.guru

Bringup GitLab CE on Box 3 + GitLab Runners on Box 2.

Preflight (2026-07-28): Box 3 — 62 Gi RAM, ~43 Gi available; 373 Gi disk free on `/`. **PASS** for 8–12 Gi GitLab cap.

## Prerequisites

- SSH: `box3` alias → `212.92.101.15`, `qaguru` + `~/.ssh/qa_guru_ed25519`
- Box 2: `qaguru@89.248.193.83` (same key)
- DNS token: `~/.config/cloudflare-dns.token`
- Secrets dir: `~/.config/gitlab/` (created by install, not in git)
- DNS upsert uses monorepo `scripts/infra-dns/dns_audit.py` (wrapper checkout or `ZERO_DESIGN_SYSTEM`)

## Install order

Run from repo root:

```bash
# 1. GitLab Omnibus on Box 3
./deploy/install-gitlab-box3.sh

# 2. DNS A gitlab.qa.guru → Box2 edge (then certbot on Box2 + Box3)
./deploy/configure-dns.sh

# 3. nginx TLS (Box3 direct + Box2 edge proxy)
./deploy/configure-nginx-box3.sh
./deploy/configure-nginx-box2-edge.sh

# 3b. Lock workhorse :8181 → BOX2_CI + localhost (iptables + netfilter-persistent)
./deploy/restrict-workhorse-8181.sh

# 4. Runners on Box 2 (after admin bootstrap; auth tokens in ~/.config/gitlab/runner.env)
./deploy/install-runner-box2.sh

# 5. Smoke
./deploy/smoke.sh
```

## Admin bootstrap

After first `gitlab-ctl reconfigure`, root password is in `/etc/gitlab/initial_root_password` (one-time, 24h). Copy to `~/.config/gitlab/admin.env`:

```bash
ssh box3 'sudo cat /etc/gitlab/initial_root_password'  # once
# locally: GITLAB_ROOT_PASSWORD=…  GITLAB_URL=https://gitlab.qa.guru
```

Create group `qa-guru`, project `reference-app-tests`, import or push [`etalon/.gitlab-ci.yml`](../etalon/.gitlab-ci.yml).

## Runners (Box 2) — authentication tokens (GitLab 19+)

Registration tokens (`GITLAB_RUNNER_REGISTRATION_TOKEN`, `gitlab-runner register --registration-token`) are **deprecated** (removal GitLab 20). Use **runner authentication tokens** (`glrt-` prefix):

| File | Variables |
|------|-----------|
| `~/.config/gitlab/admin.env` | `GITLAB_URL`, `GITLAB_ROOT_USER`, `GITLAB_ROOT_PASSWORD` |
| `~/.config/gitlab/runner.env` | `GITLAB_RUNNER_AUTH_TOKEN_1`, `GITLAB_RUNNER_AUTH_TOKEN_2`, … |

Flow (automated in [`install-runner-box2.sh`](install-runner-box2.sh)):

1. **Create runner** on GitLab (Box3 `CreateRunnerService` via `gitlab-rails runner`) — description, tags, `run_untagged=false` set here.
2. **Save token** to `runner.env` (shown once at creation).
3. **Register manager** on Box2: `gitlab-runner register --token …` (executor/docker settings only).

Commands:

```bash
# Fresh install / idempotent re-run (skips existing managers in config.toml)
./deploy/install-runner-box2.sh

# One-time: legacy glrtr- → glrt- migration (brief runner restart)
./deploy/install-runner-box2.sh --migrate

# Disaster recovery: copy tokens from live Box2 config.toml → runner.env
./deploy/install-runner-box2.sh --bootstrap-env
```

Runners `box2-docker-1/2` poll **`https://gitlab.qa.guru`** (not internal workhorse `:8181`). Tags: `docker,java21,selenoid,box2`.

## Sign-in — Keycloak OIDC (P5)

Phase `11.qa-guru-identity`, window P5. Everything runs from the laptop through [`oidc.py`](oidc.py); the client secret comes from `~/.config/auth-qa-guru/keycloak.env` and lands in `/etc/gitlab/oidc.env` (root 600). Nothing is printed and nothing is committed.

```bash
python3 deploy/oidc.py inventory              # gate: users + group members + tokens → ~/.config/gitlab/p5-inventory/
python3 deploy/oidc.py link-identities        # dry run; --apply pre-seeds openid_connect identities
python3 deploy/oidc.py seed-idp               # Keycloak: client secret check, github IdP
python3 deploy/oidc.py configure              # gitlab.rb block + gitlab-ctl reconfigure
python3 deploy/oidc.py login-check            # sign in as the pilot people, end to end
python3 deploy/oidc.py verify                 # acceptance against the inventory floor
python3 deploy/oidc.py break-glass            # root + runner tokens with the IdP stopped
```

**Run `link-identities --apply` before `configure`.** GitLab's `auto_link_user` resolves an existing account by **primary email only** (`User.find_by(email:)` in `lib/gitlab/auth/o_auth/user.rb`). A person whose GitLab address differs from their Keycloak address would not be matched, and JIT would create a second account with a suffixed username. Pre-seeding the identity makes `find_by_uid_and_provider` hit first, so the existing account, its group membership and its history are kept.

`uid_field` and `gitlab_username_claim` are both `preferred_username`, so JIT users get the bare handle — GitLab does not append a suffix the way Sonar does.

**`/users/sign_in` goes straight to Keycloak** (`omniauth_auto_sign_in_with_provider = 'openid_connect'`): the local password form only trapped students typing their IdP password. Break-glass `root` signs in at `https://gitlab.qa.guru/users/sign_in?auto_sign_in=false`. The auto-submit page renders the provider form with an empty `authenticity_token` and JS fills it from `<meta name="csrf-token">`; `oidc.py` does the same for its non-JS sign-in.

## GitHub sign-in

**Brokering** (GitHub → Keycloak → GitLab). GitLab's sign-in page offers only `openid_connect`. The GitHub button lives on Keycloak (`https://auth.qa.guru/realms/qaguru/broker/github/endpoint`). Direct OmniAuth is gone (`disable-github-direct`).

`svasenkov` is pre-linked in Keycloak to GitHub user `9194170` so the first broker login does not mint a second realm user (GitHub email ≠ school email).

`enable-github-omniauth.sh` is **superseded**. Re-apply brokering after a realm re-import: credentials in `~/.config/auth-qa-guru/keycloak.env`, then `python3 deploy/oidc.py seed-idp`.

## Smoke

```bash
./deploy/smoke.sh
# Pilot pipeline: https://gitlab.qa.guru/qa-guru/reference-app-tests/-/pipelines/10
```

If pipelines stick in `created`/`pending`: first `./deploy/apply-gitlab-rb.sh` (Sidekiq health port / env). Emergency only: `./deploy/process-stuck-pipelines.sh`.

## Backup / restore (Box 3)

Verified 2026-07-28 on Box3 (GitLab CE **19.2.0**).

### Policy (live)

| Item | Value |
|------|--------|
| Cron | `/etc/cron.d/gitlab-backup` → `0 2 * * * root gitlab-backup create CRON=1 SKIP=registry,artifacts,builds,pages,lfs,terraform_state,packages,ci_secure_files` |
| Path | `/var/opt/gitlab/backups` (`backup_keep_time` = 7 days) |
| Manual create | `ssh box3 'sudo gitlab-backup create SKIP=registry,artifacts,builds,pages,lfs,terraform_state,packages,ci_secure_files'` |
| First archive | empty dir **4.0K** → **`1785275421_2026_07_28_19.2.0_gitlab_backup.tar` (~1.6M)** |

Cron is installed by [`install-gitlab-box3.sh`](install-gitlab-box3.sh). Omnibus **does not** put `gitlab.rb` / `gitlab-secrets.json` into the tar — keep those off-host (local `~/.config/gitlab/` copies / ops vault). **Never commit secrets.**

### Restore (same host / same major.minor.patch)

Restore only onto the **same GitLab CE version** that created the archive (here: `19.2.0`). External nginx + workhorse ACL stay in place; this restores Omnibus data only.

```bash
# 1. List archives (BACKUP id = prefix before _gitlab_backup.tar)
ssh box3 'sudo ls -lah /var/opt/gitlab/backups/'

# 2. Stop processes that write to DB / repos (keep PostgreSQL up)
ssh box3 'sudo gitlab-ctl stop puma && sudo gitlab-ctl stop sidekiq'

# 3. Restore (example id from day-1 verify — replace with the archive you need)
ssh box3 'sudo gitlab-backup restore BACKUP=1785275421_2026_07_28_19.2.0'

# 4. If gitlab.rb / gitlab-secrets.json were lost, restore them from off-host
#    copies first, then reconfigure. Do not paste secrets into chat/git.
ssh box3 'sudo gitlab-ctl reconfigure && sudo gitlab-ctl restart'

# 5. Smoke
./deploy/smoke.sh
```

Notes:

- Answer `yes` to Omnibus prompts that overwrite DB / repos.
- Skipped components in cron (`SKIP=…`) are **not** in the archive — do not expect registry/artifacts/LFS/etc. after restore on this contour.
- After restore, re-check runners (`box2-docker-*`) and GitHub OmniAuth (`./deploy/enable-github-omniauth.sh` if the OmniAuth block was wiped with `gitlab.rb`).
- Disaster rebuild on a **new** Box3: install Omnibus → restore `gitlab-secrets.json` + tuned `gitlab.rb` (or `./deploy/apply-gitlab-rb.sh` + OmniAuth) → copy `*_gitlab_backup.tar` into `/var/opt/gitlab/backups` (owner `git:git`) → steps 2–5 above → nginx/ACL scripts as in Install order.

## Scripts

| Script | Role |
|--------|------|
| [`install-gitlab-box3.sh`](install-gitlab-box3.sh) | Omnibus install + `gitlab.rb` + cgroup |
| [`gitlab.rb.template`](gitlab.rb.template) | Memory-tuned Omnibus config (Sidekiq health `:8094`) |
| [`apply-gitlab-rb.sh`](apply-gitlab-rb.sh) | Re-apply template → Box3 (keeps OmniAuth) |
| [`oidc.py`](oidc.py) | P5 — Keycloak OIDC sign-in, account linking, acceptance, break-glass |
| [`enable-github-omniauth.sh`](enable-github-omniauth.sh) | GitHub OAuth → Omnibus OmniAuth (**superseded by `oidc.py`**) |
| [`configure-dns.sh`](configure-dns.sh) | Cloudflare A → Box2 edge |
| [`configure-nginx-box3.sh`](configure-nginx-box3.sh) | TLS vhost on Box3 |
| [`configure-nginx-box2-edge.sh`](configure-nginx-box2-edge.sh) | Edge proxy Box2 → Box3 |
| [`restrict-workhorse-8181.sh`](restrict-workhorse-8181.sh) | iptables ACL `:8181` → `127.0.0.1` + `BOX2_CI` |
| [`install-runner-box2.sh`](install-runner-box2.sh) | 2× Docker executor runners (auth tokens, `--migrate`) |
| [`process-stuck-pipelines.sh`](process-stuck-pipelines.sh) | Emergency flush stuck CI pipelines |
| [`configure-group-cleanup-v1.sh`](configure-group-cleanup-v1.sh) | qa-guru artifact/pipeline retention v1 (14d) |
| [`smoke.sh`](smoke.sh) | curl + workhorse ACL + runner verify |

Credentials: `~/.config/gitlab/admin.env` (admin), `runner.env` (glrt- auth tokens), `github-oauth.env` — **never commit**. Do **not** store runner tokens in `admin.env`.
