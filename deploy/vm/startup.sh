#!/bin/bash
# brain-vm startup script (GCE metadata `startup-script`). Runs as root on every boot,
# so every step checks before it acts. Installs Docker Engine + the Compose plugin from
# Docker's apt repo, caps container logs, adds swap, points Docker at Artifact Registry
# through the gcloud credential helper, and creates /opt/brain. The app stack itself is
# started by Docker (restart: unless-stopped), not by this script.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REGISTRY_HOST=asia-south1-docker.pkg.dev
DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

if ! command -v docker >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  if ! gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | grep -q "^fpr:::::::::${DOCKER_KEY_FINGERPRINT}:"; then
    echo "Docker apt key fingerprint mismatch" >&2
    exit 1
  fi
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if [ ! -f /etc/docker/daemon.json ]; then
  install -d -m 0755 /etc/docker
  cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
  systemctl restart docker
fi
systemctl enable --now docker

if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >>/etc/fstab
  echo 'vm.swappiness=10' >/etc/sysctl.d/90-swappiness.conf
  sysctl -q -p /etc/sysctl.d/90-swappiness.conf
fi
swapon --show | grep -q /swapfile || swapon /swapfile

# Root pulls images in deploy.sh; the helper trades the VM service account for registry tokens.
if ! grep -qs "\"${REGISTRY_HOST}\"" /root/.docker/config.json; then
  HOME=/root gcloud auth configure-docker "${REGISTRY_HOST}" --quiet
fi

install -d -m 0755 /opt/brain /opt/brain/releases
