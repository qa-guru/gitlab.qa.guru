# gitlab.qa.guru

Production deploy for **GitLab CE** on Box 3 + GitLab Runners on Box 2 — **https://gitlab.qa.guru**

| На сервере | Значение |
|------------|----------|
| URL | https://gitlab.qa.guru |
| GitLab CE | **19.2.0** Omnibus `/opt/gitlab` |
| Box 3 | `212.92.101.15` — Omnibus, workhorse `:8181`, Puma `:8088` |
| Box 2 | `89.248.193.83` — edge nginx + 2× Docker executor runners |
| Runners | `box2-docker-1/2`, tags `docker,java21,selenoid,box2` |
| Group | `qa-guru` · pilot [reference-app-tests](https://gitlab.qa.guru/qa-guru/reference-app-tests) |

## Быстрый старт

Из клона (laptop):

```bash
./deploy/smoke.sh
```

Полный bringup: **[deploy/README.md](deploy/README.md)**.

Monorepo wrapper (inventory only): `projects/services-home/gitlab-qa-guru-home/`.

## Структура

| Путь | Назначение |
|------|------------|
| [`deploy/`](deploy/) | Install, nginx, DNS, runners, smoke — **SSOT** |
| [`etalon/.gitlab-ci.yml`](etalon/.gitlab-ci.yml) | Reference-app Java + Selenoid + Allure pilot CI |

Secrets (local, not in git): `~/.config/gitlab/admin.env`, `runner.env`, `github-oauth.env`.
