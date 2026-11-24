#!/usr/bin/env bash
set -euo pipefail

# Run this on Medium and again on High using the same .tar.gz archive. It requires only git and tar—no API calls, curl, or jq needed.

# It force-pushes main → env-main (clobbering drift on env-main) and force-syncs tags with --prune (removing destination-only tags), while leaving all other branches, CI/CD variables, and project settings untouched.

# Token permissions: write_repository (Maintainer role if env-main is protected).

# Usage:
#   # Preview changes on Medium before pushing (--dry-run)
#   GITLAB_TOKEN="glpat-yyy" ./gitlab-bundle-import.sh \
#     --input gitlab-bundles-20261123-183000.tar.gz \
#     --url https://gitlab-medium.example.com \
#     --group my-org/my-group \
#     --all --dry-run
#
#   # Update all repos in the archive
#   GITLAB_TOKEN="glpat-yyy" ./gitlab-bundle-import.sh \
#     --input gitlab-bundles-20261123-183000.tar.gz \
#     --url https://gitlab-medium.example.com \
#     --group my-org/my-group \
#     --all
#
#   # Update only specific repos from the archive
#   GITLAB_TOKEN="glpat-yyy" ./gitlab-bundle-import.sh \
#     --input gitlab-bundles-20261123-183000.tar.gz \
#     --url https://gitlab-high.example.com \
#     --group my-org/my-group \
#     repo-one repo-two

INPUT_FILE=""
GITLAB_URL=""
GITLAB_GROUP=""
SRC_BRANCH="main"
DST_BRANCH="env-main"
USE_ALL=false
USE_SSH=false
DRY_RUN=false
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)      INPUT_FILE="$2"; shift 2 ;;
    --url)        GITLAB_URL="${2%/}"; shift 2 ;;
    --group)      GITLAB_GROUP="${2#/}"; GITLAB_GROUP="${GITLAB_GROUP%/}"; shift 2 ;;
    --src-branch) SRC_BRANCH="$2"; shift 2 ;;
    --dst-branch) DST_BRANCH="$2"; shift 2 ;;
    --all)        USE_ALL=true; shift ;;
    --ssh)        USE_SSH=true; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 --input <archive.tar.gz> --url <gitlab_url> --group <group_path> [--src-branch main] [--dst-branch env-main] [--ssh] [--dry-run] (--all | repo1 [repo2 ...])"
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2; exit 1 ;;
    *)
      REPOS+=("$1"); shift ;;
  esac
done

if [[ -z "$INPUT_FILE" || -z "$GITLAB_URL" || -z "$GITLAB_GROUP" ]]; then
  echo "Error: --input, --url, and --group are required." >&2
  exit 1
fi

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "Error: Archive not found: ${INPUT_FILE}" >&2
  exit 1
fi

if [[ "$USE_ALL" == false && ${#REPOS[@]} -eq 0 ]]; then
  echo "Error: Specify --all or provide at least one repo name." >&2
  exit 1
fi

if [[ "$USE_SSH" == false && -z "${GITLAB_TOKEN:-}" ]]; then
  echo "Error: GITLAB_TOKEN environment variable is required for HTTPS pushes (or use --ssh)." >&2
  exit 1
fi

WORK_DIR=$(mktemp -d)
ASKPASS_SCRIPT=""
cleanup() {
  rm -rf "$WORK_DIR"
  [[ -n "$ASKPASS_SCRIPT" ]] && rm -f "$ASKPASS_SCRIPT"
}
trap cleanup EXIT

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

EXTRACT_DIR="${WORK_DIR}/extracted"
mkdir -p "$EXTRACT_DIR"
tar -xzf "$INPUT_FILE" -C "$EXTRACT_DIR"

if [[ "$USE_ALL" == true ]]; then
  if [[ ! -f "${EXTRACT_DIR}/manifest.txt" ]]; then
    echo "Error: manifest.txt missing from archive." >&2
    exit 1
  fi
  while IFS= read -r rel_path; do
    [[ -n "$rel_path" ]] && REPOS+=("$rel_path")
  done < "${EXTRACT_DIR}/manifest.txt"
fi

GITLAB_HOST="${GITLAB_URL#http://}"
GITLAB_HOST="${GITLAB_HOST#https://}"
GITLAB_HOST="${GITLAB_HOST%%/*}"

PUSH_OPTS=(--force)
[[ "$DRY_RUN" == true ]] && PUSH_OPTS+=(--dry-run)

SUCCEEDED=()
FAILED=()

[[ "$DRY_RUN" == true ]] && echo "*** DRY-RUN MODE: No changes will be pushed ***"

for repo in "${REPOS[@]}"; do
  echo "------------------------------------------------------------"
  echo "Importing: ${repo} (${SRC_BRANCH} -> ${DST_BRANCH} + tags)"

  BUNDLE_FILE="${EXTRACT_DIR}/bundles/${repo}.bundle"
  if [[ ! -f "$BUNDLE_FILE" ]]; then
    echo "  [FAILED] Bundle not found in archive: ${repo}" >&2
    FAILED+=("$repo")
    continue
  fi

  if [[ "$USE_SSH" == true ]]; then
    DEST_URL="git@${GITLAB_HOST}:${GITLAB_GROUP}/${repo}.git"
  else
    DEST_URL="${GITLAB_URL}/${GITLAB_GROUP}/${repo}.git"
  fi

  TMP_REPO="${WORK_DIR}/repo_${RANDOM}_${RANDOM}"
  git init --bare --quiet "$TMP_REPO"

  # Unpack bundle: map source branch -> destination branch, plus all tags
  if ! git -C "$TMP_REPO" fetch --quiet --no-tags "$BUNDLE_FILE" "refs/heads/${SRC_BRANCH}:refs/heads/${DST_BRANCH}"; then
    echo "  [FAILED] Could not unpack branch '${SRC_BRANCH}' from bundle." >&2
    FAILED+=("$repo")
    rm -rf "$TMP_REPO"
    continue
  fi

  git -C "$TMP_REPO" fetch --quiet "$BUNDLE_FILE" "refs/tags/*:refs/tags/*"

  # 1. Force-push main -> env-main (clobbers drift, no MR created)
  # 2. Force-push + prune tags so tags match source 1:1
  if git -C "$TMP_REPO" push "${PUSH_OPTS[@]}" "$DEST_URL" "refs/heads/${DST_BRANCH}:refs/heads/${DST_BRANCH}" \
     && git -C "$TMP_REPO" push "${PUSH_OPTS[@]}" --prune "$DEST_URL" "refs/tags/*:refs/tags/*"; then
    SUCCEEDED+=("$repo")
    echo "  [OK]"
  else
    echo "  [FAILED] Push rejected for ${repo} (check repo existence and protected branch/tag force-push settings)." >&2
    FAILED+=("$repo")
  fi

  rm -rf "$TMP_REPO"
done

echo "============================================================"
echo "Summary: ${#SUCCEEDED[@]} succeeded, ${#FAILED[@]} failed."

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "Failed repos: ${FAILED[*]}" >&2
  exit 1
fi