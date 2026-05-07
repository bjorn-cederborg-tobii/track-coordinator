#!/usr/bin/env bash
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
SKILLS_TARGET_DIR="${CODEX_HOME}/skills"
MANIFEST_PATH="${CODEX_HOME}/.track-coordinator-installed-skills"
PACKAGE_NAME="track-coordinator"

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "error: Required command not found: ${command_name}" >&2
    exit 1
  fi
}

require_command uv

if [[ -f "${MANIFEST_PATH}" ]]; then
  while IFS= read -r skill_name; do
    [[ -n "${skill_name}" ]] || continue
    rm -rf "${SKILLS_TARGET_DIR}/${skill_name}"
    echo "Removed skill: ${skill_name}"
  done < "${MANIFEST_PATH}"
  rm -f "${MANIFEST_PATH}"
else
  echo "No managed skill manifest found."
fi

if uv tool list | grep -Eq "^${PACKAGE_NAME} "; then
  uv tool uninstall "${PACKAGE_NAME}"
  echo "Uninstalled CLI package: ${PACKAGE_NAME}"
else
  echo "CLI package not installed: ${PACKAGE_NAME}"
fi
