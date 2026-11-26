# CI image for building Proxmox VE auto-install ISOs and talking to iLO/Azure.
# PVE 9 is Debian 13 (trixie) based - proxmox-auto-install-assistant must come
# from the trixie pve-no-subscription repo, not bookworm. provision.py does
# all iLO/Azure HTTP itself (stdlib urllib/http.client) - curl here is only
# needed at image-build time, to fetch the Proxmox repo's GPG key. gpgv is
# used at runtime to verify Proxmox's signed ISO SHA256SUMS; trixie's base
# image only ships sqv and gnupg merely recommends gpgv, so it's explicit.
FROM debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl gnupg gpgv xorriso python3 ca-certificates \
    && curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
         -o /etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg \
    && echo "deb http://download.proxmox.com/debian/pve trixie pve-no-subscription" \
         > /etc/apt/sources.list.d/pve-install-repo.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends proxmox-auto-install-assistant \
    && gpgv --version >/dev/null \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
