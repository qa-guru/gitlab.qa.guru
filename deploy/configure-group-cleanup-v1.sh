#!/usr/bin/env bash
# Apply qa-guru group cleanup policy v1 on GitLab CE (Box 3):
#   - Instance default job artifacts expiration: 14 days
#   - All projects under group qa-guru: automatic pipeline cleanup after 14 days
#
# GitLab CE has no group-level artifact/pipeline retention API — policy is enforced via
# instance default + per-project CI/CD settings + etalon .gitlab-ci.yml expire_in.
#
# Usage (laptop): ./configure-group-cleanup-v1.sh
# Verify only:    ./configure-group-cleanup-v1.sh --verify
set -euo pipefail

HOST="${GITLAB_DEPLOY_HOST:-box3}"
GROUP_PATH="${GITLAB_GROUP_PATH:-qa-guru}"
ARTIFACTS_EXPIRE="${GITLAB_ARTIFACTS_EXPIRE:-14 days}"
PIPELINE_RETENTION="${GITLAB_PIPELINE_RETENTION:-14 days}"
VERIFY_ONLY=false
if [[ "${1:-}" == "--verify" ]]; then
  VERIFY_ONLY=true
fi

echo "==> qa-guru cleanup v1 on ${HOST} (group=${GROUP_PATH}, artifacts=${ARTIFACTS_EXPIRE}, pipelines=${PIPELINE_RETENTION})"

ssh "${HOST}" "sudo tee /tmp/configure-group-cleanup-v1.rb >/dev/null" <<RUBY
group_path = '${GROUP_PATH}'
artifacts_expire = '${ARTIFACTS_EXPIRE}'
pipeline_retention = '${PIPELINE_RETENTION}'
verify_only = ${VERIFY_ONLY}

g = Group.find_by_full_path(group_path)
abort("group not found: " + group_path) unless g

projects = g.all_projects.includes(:ci_cd_settings)
as = ApplicationSetting.current

if verify_only
  puts "instance default_artifacts_expire_in=" + as.default_artifacts_expire_in.inspect
  projects.each do |p|
    secs = p.ci_cd_settings.delete_pipelines_in_seconds
    human = secs ? ChronicDuration.output(secs, format: :short) : 'never'
    puts p.full_path + ": delete_pipelines_in_seconds=" + secs.inspect + " (" + human + ")"
  end
  exit 0
end

as.update!(default_artifacts_expire_in: artifacts_expire)
puts "instance default_artifacts_expire_in=" + as.default_artifacts_expire_in

pipeline_seconds = ChronicDuration.parse(pipeline_retention)
abort("invalid pipeline retention: " + pipeline_retention.inspect) unless pipeline_seconds&.positive?

projects.each do |p|
  p.ci_cd_settings.update!(delete_pipelines_in_seconds: pipeline_seconds.to_i)
  puts p.full_path + ": delete_pipelines_in_seconds=" + p.ci_cd_settings.delete_pipelines_in_seconds.to_s
end

puts "OK: qa-guru cleanup v1 applied (" + projects.size.to_s + " project(s))"
RUBY

ssh "${HOST}" 'sudo gitlab-rails runner /tmp/configure-group-cleanup-v1.rb'
