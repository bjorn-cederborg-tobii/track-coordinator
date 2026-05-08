#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
SKILLS_SOURCE_DIR="${REPO_ROOT}/skills"
SKILLS_TARGET_DIR="${CODEX_HOME}/skills"
MANIFEST_PATH="${CODEX_HOME}/.track-coordinator-installed-skills"
PACKAGE_NAME="track-coordinator"
XDG_DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
BASH_COMPLETION_TARGET_DIR="${XDG_DATA_HOME}/bash-completion/completions"
BASH_COMPLETION_TARGET_PATH="${BASH_COMPLETION_TARGET_DIR}/track"

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "error: Required command not found: ${command_name}" >&2
    exit 1
  fi
}

contains_line() {
  local needle="$1"
  shift
  local value
  for value in "$@"; do
    if [[ "${value}" == "${needle}" ]]; then
      return 0
    fi
  done
  return 1
}

require_command uv
require_command rsync
require_command python3

mkdir -p "${CODEX_HOME}" "${SKILLS_TARGET_DIR}"

declare -a current_skills=()
if [[ -d "${SKILLS_SOURCE_DIR}" ]]; then
  while IFS= read -r skill_dir; do
    [[ -f "${skill_dir}/SKILL.md" ]] || continue
    current_skills+=("$(basename "${skill_dir}")")
  done < <(find "${SKILLS_SOURCE_DIR}" -mindepth 1 -maxdepth 1 -type d | sort)
fi

if [[ -f "${MANIFEST_PATH}" ]]; then
  while IFS= read -r installed_skill; do
    [[ -n "${installed_skill}" ]] || continue
    if ! contains_line "${installed_skill}" "${current_skills[@]}"; then
      rm -rf "${SKILLS_TARGET_DIR}/${installed_skill}"
      echo "Removed stale managed skill: ${installed_skill}"
    fi
  done < "${MANIFEST_PATH}"
fi

echo "Installing CLI package: ${PACKAGE_NAME}"
uv tool install --editable "${REPO_ROOT}" --force
echo "Installed CLI command: track"

mkdir -p "${BASH_COMPLETION_TARGET_DIR}"
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m track_coordinator completion bash > "${BASH_COMPLETION_TARGET_PATH}"
echo "Installed bash completion: ${BASH_COMPLETION_TARGET_PATH}"

manifest_tmp="$(mktemp "${MANIFEST_PATH}.XXXXXX")"
trap 'rm -f "${manifest_tmp}"' EXIT
: > "${manifest_tmp}"

for skill_name in "${current_skills[@]}"; do
  src_dir="${SKILLS_SOURCE_DIR}/${skill_name}/"
  dest_dir="${SKILLS_TARGET_DIR}/${skill_name}/"
  mkdir -p "${dest_dir}"
  rsync -a --delete "${src_dir}" "${dest_dir}"
  printf '%s\n' "${skill_name}" >> "${manifest_tmp}"
  echo "Installed skill: ${skill_name} -> ${dest_dir}"
done

mv "${manifest_tmp}" "${MANIFEST_PATH}"
trap - EXIT

if [[ "${#current_skills[@]}" -eq 0 ]]; then
  echo "No repo-managed skills found."
fi
