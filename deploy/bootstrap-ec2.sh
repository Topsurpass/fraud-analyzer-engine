#!/usr/bin/env bash
# One-time host setup for a fresh Ubuntu 22.04 / 24.04 EC2 instance.
#
#     ./bootstrap-ec2.sh
#
# Run as a user with sudo (ubuntu, by default on an Ubuntu AMI). It calls sudo
# itself where it needs to and asks for nothing else. Safe to re-run: every
# step checks before it acts.
#
# What it does, and nothing more:
#   1. installs Docker Engine and the compose plugin from Docker's own apt
#      repository
#   2. puts you in the docker group so the deploy scripts do not need sudo
#   3. enables the daemon at boot, so a reboot brings the stack back
#   4. adds swap on a small instance, because the dashboard's Next build is a
#      memory peak that gets OOM-killed on a t3.micro
#
# It does NOT touch the firewall, the security group, or DNS. Inbound 80 and
# 443 are a security-group setting in the AWS console, and this script has no
# business guessing who should reach this box.

set -euo pipefail

C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }
ok()   { printf '    %sok%s   %s\n' "$C_GREEN" "$C_RESET" "$1"; }
note() { printf '    %snote%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }

if [[ ! -r /etc/os-release ]]; then
	echo "Cannot identify this OS: no /etc/os-release." >&2
	exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
	echo "This script is written for Ubuntu; this host reports ID=${ID:-unknown}." >&2
	echo "Docker's install docs cover every other distribution: https://docs.docker.com/engine/install/" >&2
	exit 1
fi
printf '%sBootstrapping %s %s%s\n' "$C_BOLD" "$PRETTY_NAME" "$(uname -m)" "$C_RESET"

# --- 1. Docker --------------------------------------------------------------

step "Docker Engine and the compose plugin"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
	ok "already installed ($(docker --version | cut -d, -f1), compose $(docker compose version --short))"
else
	# Docker's own repository, not Ubuntu's docker.io package. The distro
	# package lags by releases and ships neither the compose plugin nor the
	# buildx plugin, both of which this deploy uses.
	sudo apt-get update -qq
	sudo apt-get install -y -qq ca-certificates curl gnupg

	sudo install -m 0755 -d /etc/apt/keyrings
	if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
		sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
			-o /etc/apt/keyrings/docker.asc
		sudo chmod a+r /etc/apt/keyrings/docker.asc
	fi

	# $VERSION_CODENAME is jammy on 22.04 and noble on 24.04. Read from
	# os-release rather than hardcoded, so this works on both without an if.
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
		| sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

	sudo apt-get update -qq
	sudo apt-get install -y -qq \
		docker-ce docker-ce-cli containerd.io \
		docker-buildx-plugin docker-compose-plugin
	ok "installed $(docker --version | cut -d, -f1)"
fi

# --- 2. group membership ----------------------------------------------------

step "Docker group membership for $(id -un)"

if id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
	ok "already in the docker group"
else
	sudo usermod -aG docker "$(id -un)"
	ok "added $(id -un) to the docker group"
	note "group membership is read at login, so it is NOT active in this shell."
	note "Log out and back in (or run: newgrp docker) before ./deploy.sh."
fi

# --- 3. start at boot -------------------------------------------------------

step "Start Docker at boot"

sudo systemctl enable --now docker >/dev/null 2>&1
ok "docker.service is enabled and running"
# Without this, a reboot leaves the instance up and the stack down, and
# `restart: unless-stopped` in the compose file cannot help - it only restores
# containers once the daemon itself is back.

# --- 4. swap on a small instance --------------------------------------------

step "Swap"

MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
SWAP_KB="$(awk '/SwapTotal/ {print $2}' /proc/meminfo)"

if [[ $MEM_MB -ge 3500 ]]; then
	ok "${MEM_MB} MB of memory, no swap needed"
elif [[ ${SWAP_KB:-0} -gt 0 ]]; then
	ok "swap already configured ($((SWAP_KB / 1024)) MB)"
else
	# The Next production build is the peak, not the running stack. Without
	# swap on a t3.micro it is OOM-killed, and the symptom is a compiler
	# process dying with no message that mentions memory.
	note "${MEM_MB} MB of memory is below what the dashboard build peaks at; adding 2 GB of swap"
	sudo fallocate -l 2G /swapfile
	sudo chmod 600 /swapfile
	sudo mkswap /swapfile >/dev/null
	sudo swapon /swapfile
	# Survive a reboot. Guarded so re-running does not append a duplicate line.
	grep -q '^/swapfile ' /etc/fstab || \
		echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
	ok "2 GB swapfile active and recorded in /etc/fstab"
fi

# --- done -------------------------------------------------------------------

cat <<EOF

$(printf '%s' "$C_BOLD")Host is ready.$(printf '%s' "$C_RESET")

Next, in this order:

  1. If this script just added you to the docker group, log out and back in.

  2. Check the two repositories are both here and on the branch you want:
         ls -d ~/fraud-analyzer-engine ~/fraud-analyzer-dashboard

  3. Configure:
         cd fraud-analyzer-engine/deploy
         cp .env.prod.example .env.prod
         chmod 600 .env.prod
         nano .env.prod

  4. Check the configuration, then deploy, then prove it works:
         ./preflight.sh
         ./deploy.sh
         ./verify.sh

  5. In the AWS console, allow inbound 80 and 443 on this instance's security
     group, and confirm the RDS security group allows 5432 from THIS
     instance's security group and from nowhere else.
EOF
