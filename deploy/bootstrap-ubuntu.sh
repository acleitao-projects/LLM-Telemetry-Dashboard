#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/bootstrap-ubuntu.sh <runner-user>" >&2
  exit 1
fi

RUNNER_USER=${1:-${SUDO_USER:-}}
if [[ -z ${RUNNER_USER} || ${RUNNER_USER} == root ]] || ! id "${RUNNER_USER}" >/dev/null 2>&1; then
  echo "Pass the Linux account used by the GitHub Actions runner." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv rsync curl
SYSTEMCTL_PATH=$(command -v systemctl)

if ! id llm-telemetry >/dev/null 2>&1; then
  useradd --system --home-dir /opt/llm-telemetry --shell /usr/sbin/nologin llm-telemetry
fi

install -d -o "${RUNNER_USER}" -g llm-telemetry -m 0755 /opt/llm-telemetry
install -d -o "${RUNNER_USER}" -g llm-telemetry -m 0755 /opt/llm-telemetry/releases
install -d -o llm-telemetry -g llm-telemetry -m 0750 /var/lib/llm-telemetry
install -o root -g root -m 0644 deploy/llm-telemetry.service /etc/systemd/system/llm-telemetry.service

cat >"/etc/sudoers.d/llm-telemetry-runner" <<EOF
${RUNNER_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_PATH} restart llm-telemetry.service
EOF
chmod 0440 /etc/sudoers.d/llm-telemetry-runner
visudo -cf /etc/sudoers.d/llm-telemetry-runner

systemctl daemon-reload
systemctl enable llm-telemetry.service

echo "Bootstrap complete. Register this runner with labels: self-hosted,linux,x64,llm-telemetry"
echo "The first successful main-branch workflow will install and start LLM-Telemetry."
