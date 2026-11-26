# syntax=docker/dockerfile:1
# CI image for building Proxmox VE auto-install ISOs and talking to iLO/Azure.
# provision.py does all iLO/Azure HTTP itself (Python stdlib), so the image
# only adds the few CLI tools it shells out to. PVE 9 is Debian 13 (trixie)
# based - proxmox-auto-install-assistant must come from the trixie repo.
FROM python:3.15.2-slim-trixie@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

# Proxmox release key, pinned by checksum (no curl/gnupg needed in the image).
# Trusted by apt only for the Proxmox repo (Signed-By), and used by
# provision.py to verify Proxmox's signed ISO SHA256SUMS.
ADD --checksum=sha256:1bcd2d5bab556076c9ea756a84fe2b7445b13f4ef6e97b2e412b68778377ba6d \
    https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
    /usr/share/keyrings/proxmox-release-trixie.gpg

# gpgv: signature check, openssl: root password hash,
# xorriso + proxmox-auto-install-assistant: ISO build.
RUN chmod 0644 /usr/share/keyrings/proxmox-release-trixie.gpg \
    && printf '%s\n' \
         'Types: deb' \
         'URIs: http://download.proxmox.com/debian/pve' \
         'Suites: trixie' \
         'Components: pve-no-subscription' \
         'Signed-By: /usr/share/keyrings/proxmox-release-trixie.gpg' \
         > /etc/apt/sources.list.d/proxmox.sources \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
         gpgv openssl xorriso proxmox-auto-install-assistant \
    && gpgv --version >/dev/null && openssl version >/dev/null \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
