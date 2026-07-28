#!/usr/bin/env bash
# nginx TLS vhost for gitlab.qa.guru on Box 3 (proxy → workhorse :8181).
# Public edge is Box2; this vhost is for direct Box3 access / cert parity with Jira.
set -euo pipefail

HOST="${GITLAB_DEPLOY_HOST:-box3}"

ssh "${HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo tee /etc/nginx/sites-available/gitlab >/dev/null <<'NGINX'
server {
  listen 80; listen [::]:80;
  server_name gitlab.qa.guru;
  location /.well-known/acme-challenge/ { root /var/www/html; }
  location / { return 301 https://$host$request_uri; }
}
server {
  listen 443 ssl http2; listen [::]:443 ssl http2;
  server_name gitlab.qa.guru;
  ssl_certificate /etc/letsencrypt/live/gitlab.qa.guru/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/gitlab.qa.guru/privkey.pem;
  include /etc/letsencrypt/options-ssl-nginx.conf;
  ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
  client_max_body_size 250m;
  location / {
    proxy_pass http://127.0.0.1:8181;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-Host $host;
    proxy_read_timeout 300;
    proxy_buffering off;
  }
}
NGINX

# Cert: copy from Box2 if edge already issued, else standalone (requires DNS → Box3 briefly)
if [ ! -f /etc/letsencrypt/live/gitlab.qa.guru/fullchain.pem ]; then
  echo "No cert on Box3 yet — run after Box2 edge cert or DNS cutover to Box3 for certbot"
  sudo ln -sf /etc/nginx/sites-available/gitlab /etc/nginx/sites-enabled/gitlab || true
else
  sudo ln -sf /etc/nginx/sites-available/gitlab /etc/nginx/sites-enabled/gitlab
  sudo nginx -t && sudo systemctl reload nginx
fi
REMOTE

echo "Box3 nginx gitlab vhost staged (cert optional until DNS → Box3)."
