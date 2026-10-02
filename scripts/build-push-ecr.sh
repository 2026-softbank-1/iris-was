#!/usr/bin/env bash
# Copied from iris-infra examples/github-actions/build-push-ecr.sh. Bash 3.2 compatible.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "Required command: $1"; }

mode="${1:-}"
[[ "$#" == 1 && ( "$mode" == build || "$mode" == push ) ]] \
  || fail 'Usage: build-push-ecr.sh build|push'
require_command docker
require_command python3

build_context="${DOCKER_BUILD_CONTEXT:-.}"
dockerfile="${DOCKERFILE:-Dockerfile}"
platform="${DOCKER_PLATFORM:-linux/amd64}"
[[ -d "$build_context" ]] || fail "Build context does not exist: $build_context"
[[ -f "$dockerfile" ]] || fail "Dockerfile does not exist: $dockerfile"
[[ "$platform" == linux/amd64 || "$platform" == linux/arm64 ]] \
  || fail 'DOCKER_PLATFORM must be linux/amd64 or linux/arm64.'

source_sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
run_id="${GITHUB_RUN_ID:-$(date -u +%s)}"
attempt="${GITHUB_RUN_ATTEMPT:-1}"
[[ "$source_sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || fail 'Invalid source commit SHA.'
[[ "$run_id" =~ ^[1-9][0-9]{0,19}$ && "$attempt" =~ ^[1-9][0-9]{0,9}$ ]] \
  || fail 'Run ID and attempt must be positive integers.'
image_tag="sha-${source_sha}-${run_id}-${attempt}"
image_uri="iris-build-check:$image_tag"

if [[ "$mode" == push ]]; then
  require_command aws
  account="${AWS_ACCOUNT_ID:-}"
  region="${AWS_REGION:-}"
  repository="${ECR_REPOSITORY:-}"
  [[ "$account" =~ ^[0-9]{12}$ && "$account" != 000000000000 ]] || fail 'Invalid AWS_ACCOUNT_ID.'
  [[ "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]] || fail 'Invalid AWS_REGION.'
  [[ "$repository" =~ ^[a-z][a-z0-9._-]*/[a-z0-9]+([._-][a-z0-9]+)*$ ]] \
    || fail 'ECR_REPOSITORY must be project/service, for example iris/was.'
  registry="${account}.dkr.ecr.${region}.amazonaws.com"
  [[ "${ECR_REGISTRY:-$registry}" == "$registry" ]] || fail 'ECR registry/account/region mismatch.'
  actual_account="$(aws sts get-caller-identity --query Account --output text)"
  [[ "$actual_account" == "$account" ]] || fail 'Authenticated AWS account does not match AWS_ACCOUNT_ID.'
  image_uri="$registry/$repository:$image_tag"
fi

metadata="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/iris-build-metadata.XXXXXX")"
trap 'rm -f "$metadata"' EXIT
args=(buildx build --platform "$platform" --file "$dockerfile"
  --tag "$image_uri" --label "org.opencontainers.image.revision=$source_sha"
  --metadata-file "$metadata")
if [[ "$mode" == push ]]; then
  args+=(--push)
else
  args+=(--load)
fi
docker "${args[@]}" "$build_context"

image_digest=''
image_ref=''
if [[ "$mode" == push ]]; then
  image_digest="$(python3 - "$metadata" <<'PY'
import json
import re
import sys

with open(sys.argv[1]) as stream:
    digest = json.load(stream).get("containerimage.digest", "")
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    sys.exit("Buildx did not return a valid pushed image digest.")
print(digest)
PY
  )"
  image_ref="$registry/$repository@$image_digest"
fi

printf 'image_uri=%s\n' "$image_uri"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    printf 'image_tag=%s\nimage_uri=%s\n' "$image_tag" "$image_uri"
    if [[ "$mode" == push ]]; then
      printf 'image_digest=%s\nimage_ref=%s\n' "$image_digest" "$image_ref"
    fi
  } >> "$GITHUB_OUTPUT"
fi
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    printf '### Image %s\n\n' "$mode"
    # Backticks are literal Markdown code delimiters in these printf formats.
    # shellcheck disable=SC2016
    printf -- '- Commit: `%s`\n- Image: `%s`\n' "$source_sha" "$image_uri"
    # shellcheck disable=SC2016
    if [[ "$mode" == push ]]; then printf -- '- Deploy reference: `%s`\n' "$image_ref"; fi
  } >> "$GITHUB_STEP_SUMMARY"
fi
