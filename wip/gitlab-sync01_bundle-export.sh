#!/usr/bin/env bash
set -euo pipefail

# Run this on your Local environment. It clones only main + tags for each selected repo, creates and verifies each .bundle file, and packs them with a manifest.txt into one .tar.gz archive.

# Requirements: git, tar (plus curl and jq only when using --all).Token permissions: read_repository (plus read_api when using --all).

# Usage:
#   # Export all non-archived repos in a GitLab group (including subgroups)
#   GITLAB_TOKEN="glpat-xxx" ./gitlab-bundle-export.sh \
#     --url https://gitlab-local.example.com \
#     --group my-org/my-group \
#     --all
#
#   # Export specific repos only (relative to --group)
#   GITLAB_TOKEN="glpat-xxx" ./gitlab-bundle-export.sh \
#     --url https://gitlab-local.example.com \
#     --group my-org/my-group \
#     repo-one repo-two subgroup/repo-three

GITLAB_URL=""
GITLAB_GROUP=""
SRC_BRANCH="main"
OUTPUT_FILE=""
USE_ALL=false
USE_SSH=false
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)      GITLAB_URL="${2%/}"; shift 2 ;;
    --group)    GITLAB_GROUP="${2#/}"; GITLAB_GROUP="${GITLAB_GROUP%/}"; shift 2 ;;
    --branch)   SRC_BRANCH="$2"; shift 2 ;;
    --output)   OUTPUT_FILE="$2"; shift 2 ;;
    --all)      USE_ALL=true; shift ;;
    --ssh)      USE_SSH=true; shift ;;
    -h|--help)
      echo "Usage: $0 --url <gitlab_url> --group <group_path> [--branch main] [--output file.tar.gz] [--ssh] (--all | repo1 [repo2 ...])"
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2; exit 1 ;;
    *)
      REPOS+=("$1"); shift ;;
  esac
done

if [[ -z "$GITLAB_URL" || -z "$GITLAB_GROUP" ]]; then
  echo "Error: --url and --group are required." >&2
  exit 1
fi

if [[ "$USE_ALL" == false && ${#REPOS[@]} -eq 0 ]]; then
  echo "Error: Specify --all or provide at least one repo name." >&2
  exit 1
fi

if [[ "$USE_ALL" == true || "$USE_SSH" == false ]] && [[ -z "${GITLAB_TOKEN:-}" ]]; then
  echo "Error: GITLAB_TOKEN environment variable is required (for API discovery and/or HTTPS cloning)." >&2
  exit 1
fi

OUTPUT_FILE="${OUTPUT_FILE:-gitlab-bundles-$(date +%Y%m%d-%H%M%S).tar.gz}"

WORK_DIR=$(mktemp -d)
ASKPASS_SCRIPT=""
cleanup() {
  rm -rf "$WORK_DIR"
  [[ -n "$ASKPASS_SCRIPT" ]] && rm -f "$ASKPASS_SCRIPT"
}
trap cleanup EXIT

# Configure non-interactive HTTPS auth without embedding token in URLs
if [[ "$USE_SSH" == false ]]; then
  ASKPASS_SCRIPT=$(mktemp)
  chmod 700 "$ASKPASS_SCRIPT"
  cat <<'EOF' > "$ASKPASS_SCRIPT"
#!/usr/bin/env bash
case "$1" in
  *Username*) echo "oauth2" ;;
  *Password*) echo "${GITLAB_TOKEN}" ;;
esac
EOF
  export GIT_ASKPASS="$ASKPASS_SCRIPT"
  export GIT_TERMINAL_PROMPT=0
fi

# Discover all repos in the GitLab group if --all was passed
if [[ "$USE_ALL" == true ]]; then
  command -v curl >/dev/null && command -v jq >/dev/null || {
    echo "Error: curl and jq are required for --all." >&2
    exit 1
  }

  echo "Discovering projects in group '${GITLAB_GROUP}' on ${GITLAB_URL}..."
  ENCODED_GROUP=$(jq -rn --arg g "$GITLAB_GROUP" '$g | @uri')
  PAGE=1

  while true; do
    RESP=$(curl -fsS \
      --header "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
      "${GITLAB_URL}/api/v4/groups/${ENCODED_GROUP}/projects?per_page=100&page=${PAGE}&include_subgroups=true&archived=false")

    COUNT=$(echo "$RESP" | jq 'length')
    [[ "$COUNT" -eq 0 ]] && break

    while IFS= read -r rel_path; do
      REPOS+=("$rel_path")
    done < <(echo "$RESP" | jq -r --arg prefix "${GITLAB_GROUP}/" '.[] | .path_with_namespace | ltrimstr($prefix)')

    PAGE=$((PAGE + 1))
  done

  if [[ ${#REPOS[@]} -eq 0 ]]; then
    echo "Error: No projects found in group '${GITLAB_GROUP}'." >&2
    exit 1
  fi
  echo "Found ${#REPOS[@]} project(s)."
fi

STAGE_DIR="${WORK_DIR}/stage"
mkdir -p "${STAGE_DIR}/bundles"
MANIFEST="${STAGE_DIR}/manifest.txt"
: > "$MANIFEST"

GITLAB_HOST="${GITLAB_URL#http://}"
GITLAB_HOST="${GITLAB_HOST#https://}"
GITLAB_HOST="${GITLAB_HOST%%/*}"

SUCCEEDED=()
FAILED=()

for repo in "${REPOS[@]}"; do
  echo "------------------------------------------------------------"
  echo "Bundling: ${repo} (branch: ${SRC_BRANCH} + tags)"

  if [[ "$USE_SSH" == true ]]; then
    CLONE_URL="git@${GITLAB_HOST}:${GITLAB_GROUP}/${repo}.git"
  else
    CLONE_URL="${GITLAB_URL}/${GITLAB_GROUP}/${repo}.git"
  fi

  TMP_REPO="${WORK_DIR}/repo_${RANDOM}_${RANDOM}"
  BUNDLE_OUT="${STAGE_DIR}/bundles/${repo}.bundle"
  mkdir -p "$(dirname "$BUNDLE_OUT")"

  git init --bare --quiet "$TMP_REPO"

  if ! git -C "$TMP_REPO" fetch --quiet --no-tags "$CLONE_URL" "refs/heads/${SRC_BRANCH}:refs/heads/${SRC_BRANCH}"; then
    echo "  [FAILED] Could not fetch branch '${SRC_BRANCH}' from ${repo}." >&2
    FAILED+=("$repo")
    rm -rf "$TMP_REPO"
    continue
  fi

  if ! git -C "$TMP_REPO" fetch --quiet "$CLONE_URL" "refs/tags/*:refs/tags/*"; then
    echo "  [FAILED] Could not fetch tags from ${repo}." >&2
    FAILED+=("$repo")
    rm -rf "$TMP_REPO"
    continue
  fi

  if git -C "$TMP_REPO" bundle create --quiet "$BUNDLE_OUT" "refs/heads/${SRC_BRANCH}" --tags \
     && git -C "$TMP_REPO" bundle verify --quiet "$BUNDLE_OUT"; then
    echo "$repo" >> "$MANIFEST"
    SUCCEEDED+=("$repo")
    echo "  [OK] $(du -h "$BUNDLE_OUT" | cut -f1)"
  else
    echo "  [FAILED] Bundle creation or verification failed for ${repo}." >&2
    FAILED+=("$repo")
  fi

  rm -rf "$TMP_REPO"
done

echo "============================================================"
echo "Summary: ${#SUCCEEDED[@]} succeeded, ${#FAILED[@]} failed."

if [[ ${#SUCCEEDED[@]} -eq 0 ]]; then
  echo "Error: No bundles were created." >&2
  exit 1
fi

tar -czf "$OUTPUT_FILE" -C "$STAGE_DIR" manifest.txt bundles
echo "Archive ready: ${OUTPUT_FILE} ($(du -h "$OUTPUT_FILE" | cut -f1))"

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "Failed repos: ${FAILED[*]}" >&2
  exit 1
fi