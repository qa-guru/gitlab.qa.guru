#!/usr/bin/env bash
# Edge nginx on Box 2 — proxy gitlab.qa.guru → Box 3 workhorse :8181 (dark edge pattern).
set -euo pipefail

BOX2="${BOX2_CI:-89.248.193.83}"
BOX3="${BOX3_ZOO:-212.92.101.15}"
SSH="ssh -i ${GITLAB_DEPLOY_KEY:-$HOME/.ssh/qa_guru_ed25519} -o IdentitiesOnly=yes qaguru@${BOX2}"

${SSH} "BOX3='${BOX3}' bash -s" <<'REMOTE'
set -euo pipefail

# Phase 1: HTTP-only for ACME (no SSL block until cert exists)
if [ ! -f /etc/letsencrypt/live/gitlab.qa.guru/fullchain.pem ]; then
  sudo tee /etc/nginx/sites-available/gitlab >/dev/null <<'HTTPONLY'
server {
  listen 80; listen [::]:80;
  server_name gitlab.qa.guru;
  location /.well-known/acme-challenge/ { root /var/www/html; }
  location / {
    proxy_pass http://BOX3_PLACEHOLDER:8181;
    proxy_set_header Host $host;
  }
}
HTTPONLY
  sudo sed -i "s/BOX3_PLACEHOLDER/${BOX3}/" /etc/nginx/sites-available/gitlab
  sudo ln -sf /etc/nginx/sites-available/gitlab /etc/nginx/sites-enabled/gitlab
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot certonly --webroot -w /var/www/html -d gitlab.qa.guru \
    --non-interactive --agree-tos -m admin@qa.guru
fi

# Phase 2: full TLS + proxy
sudo tee /etc/nginx/sites-available/gitlab >/dev/null <<NGINX
server {
  listen 80; listen [::]:80;
  server_name gitlab.qa.guru;
  location /.well-known/acme-challenge/ { root /var/www/html; }
  location / { return 301 https://gitlab.qa.guru\$request_uri; }
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
    proxy_pass http://${BOX3}:8181;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-Host \$host;
    proxy_read_timeout 300;
    proxy_buffering off;
  }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/gitlab /etc/nginx/sites-enabled/gitlab
sudo nginx -t && sudo systemctl reload nginx
REMOTE

echo "Box2 edge gitlab.qa.guru → http://${BOX3}:8181 configured."
