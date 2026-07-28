#!/usr/bin/env bash
# Emergency: drain Sidekiq + process stuck CI pipelines.
# Prefer fixing Sidekiq (health_checks port / apply-gitlab-rb.sh) — not routine ops.
set -euo pipefail

HOST="${GITLAB_DEPLOY_HOST:-box3}"
PIPELINE_IDS="${1:-}"

ssh "${HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo gitlab-ctl restart sidekiq
sleep 15
# Process pending pipelines (comma-separated IDs or all created/pending)
ids="${PIPELINE_IDS:-}"
if [ -z "$ids" ]; then
  ids=$(sudo gitlab-rails runner "puts Ci::Pipeline.where(status: [:created, :pending]).pluck(:id).join(',')" 2>/dev/null | tail -1)
fi
for id in $(echo "$ids" | tr ',' ' '); do
  [ -n "$id" ] || continue
  echo "==> Ci::InitialPipelineProcessWorker $id"
  sudo gitlab-rails runner "Ci::InitialPipelineProcessWorker.new.perform(${id})" 2>/dev/null || true
done
qsize=$(sudo gitlab-rails runner 'require "sidekiq/api"; puts Sidekiq::Queue.new("default").size' 2>/dev/null | tail -1)
echo "sidekiq default queue: ${qsize}"
REMOTE
