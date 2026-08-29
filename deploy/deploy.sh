#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/opt/llm-telemetry
RELEASES_DIR=${APP_ROOT}/releases
RELEASE_SHA=${RELEASE_SHA:-${GITHUB_SHA:-}}
SOURCE_DIR=${GITHUB_WORKSPACE:-$(pwd)}
HEALTH_URL=http://127.0.0.1:8090/api/overview

if [[ -z ${RELEASE_SHA} || ! ${RELEASE_SHA} =~ ^[0-9a-fA-F]{7,40}$ ]]; then
  echo "RELEASE_SHA must be a Git commit SHA." >&2
  exit 1
fi
if [[ ! -f ${SOURCE_DIR}/app.py || ! -f ${SOURCE_DIR}/requirements.txt ]]; then
  echo "The workflow checkout is not a valid LLM-Telemetry source tree." >&2
  exit 1
fi

RELEASE_DIR=${RELEASES_DIR}/${RELEASE_SHA}
PREVIOUS_RELEASE=$(readlink -f "${APP_ROOT}/current" 2>/dev/null || true)

mkdir -p "${RELEASE_DIR}"
rsync -a --delete \
  --exclude .git/ --exclude .github/ --exclude .venv/ --exclude data/ \
  "${SOURCE_DIR}/" "${RELEASE_DIR}/"

python3 -m venv "${RELEASE_DIR}/.venv"
"${RELEASE_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${RELEASE_DIR}/.venv/bin/python" -m pip install -r "${RELEASE_DIR}/requirements.txt"
"${RELEASE_DIR}/.venv/bin/python" -m unittest discover -s "${RELEASE_DIR}/tests" -v

NEXT_LINK=${APP_ROOT}/.current-${RELEASE_SHA}
rm -f -- "${NEXT_LINK}"
ln -s "${RELEASE_DIR}" "${NEXT_LINK}"
mv -Tf "${NEXT_LINK}" "${APP_ROOT}/current"
sudo systemctl restart llm-telemetry.service

healthy=false
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 2 "${HEALTH_URL}" >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done

if [[ ${healthy} != true ]]; then
  echo "Health check failed; restoring the previous release." >&2
  if [[ -n ${PREVIOUS_RELEASE} && -d ${PREVIOUS_RELEASE} ]]; then
    ROLLBACK_LINK=${APP_ROOT}/.rollback-${RELEASE_SHA}
    rm -f -- "${ROLLBACK_LINK}"
    ln -s "${PREVIOUS_RELEASE}" "${ROLLBACK_LINK}"
    mv -Tf "${ROLLBACK_LINK}" "${APP_ROOT}/current"
    sudo systemctl restart llm-telemetry.service
  fi
  exit 1
fi

mapfile -t OLD_RELEASES < <(find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2-)
for old_release in "${OLD_RELEASES[@]}"; do
  if [[ $(readlink -f "${APP_ROOT}/current") != $(readlink -f "${old_release}") ]]; then
    rm -rf -- "${old_release}"
  fi
done

echo "Deployed ${RELEASE_SHA} successfully."
