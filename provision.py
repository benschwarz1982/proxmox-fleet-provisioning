#!/usr/bin/env python3
"""Provisions ONE ProLiant DL160 Gen10 with an unattended Proxmox VE 9 install,
driven entirely by CI/CD variables - no server-specific data is hardcoded
here or in git. The NIC MAC is discovered live via the iLO Redfish API on
every run; the target disk is matched by model (PVE_DISK_MODEL), which is
identical across the fleet, so no storage/drive discovery wait is needed.

Pure-stdlib Python (urllib/http.client/hashlib/ssl) - no extra pip
dependencies needed in the CI image. Every step mirrors the bash version
this replaced, validated empirically against real servers across many runs;
this is a faithful port, not a redesign.
"""

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def log(msg):
    print(msg, flush=True)


def require_env(name, example=""):
    val = os.environ.get(name)
    if not val:
        hint = f", e.g. {example}" if example else ""
        log(f"ERROR: missing required env var {name}{hint}")
        sys.exit(1)
    return val


def env_default(name, default):
    return os.environ.get(name) or default


# Module-level config, populated by load_config() from main() - not at
# import time, so this module can be imported (e.g. by a future test)
# without immediately validating the environment and possibly exiting.
PVE_HOSTNAME = PVE_STATIC_IP = ILO_URL = None
ILO_USER = PVE_GATEWAY = PVE_DNS = AZURE_STORAGE_ACCOUNT = None
ILO_PASSWORD = PVE_ROOT_PASSWORD = AZURE_SAS_TOKEN = None
PROXMOX_ISO_BASE_URL = PROXMOX_ISO_NAME = PROXMOX_ISO_SHA256 = None
PROXMOX_MAJOR = PROXMOX_KEYRING = AZURE_CONTAINER = None
PVE_DOMAIN = PVE_MAILTO = PVE_TIMEZONE = PVE_KEYBOARD = PVE_COUNTRY = None
PVE_NIC_INDEX = PVE_DISK_MODEL = None
_AUTH_HEADER = None

# Disabling verification (iLO's self-signed cert) via the public API,
# rather than the underscore-prefixed ssl._create_unverified_context().
_SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def is_https_url(url):
    """True for an https:// URL with a host - "https:/host" (one slash) has
    the right scheme but no host, so the scheme check alone isn't enough."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname)


def load_config():
    global PVE_HOSTNAME, PVE_STATIC_IP, ILO_URL
    global ILO_USER, PVE_GATEWAY, PVE_DNS, AZURE_STORAGE_ACCOUNT
    global ILO_PASSWORD, PVE_ROOT_PASSWORD, AZURE_SAS_TOKEN
    global PROXMOX_ISO_BASE_URL, PROXMOX_ISO_NAME, PROXMOX_ISO_SHA256
    global PROXMOX_MAJOR, PROXMOX_KEYRING, AZURE_CONTAINER
    global PVE_DOMAIN, PVE_MAILTO, PVE_TIMEZONE, PVE_KEYBOARD, PVE_COUNTRY
    global PVE_NIC_INDEX, PVE_DISK_MODEL, _AUTH_HEADER

    PVE_HOSTNAME = require_env("PVE_HOSTNAME", "pve-node15")
    PVE_STATIC_IP = require_env("PVE_STATIC_IP", "192.0.2.35/24")
    ILO_URL = require_env("ILO_URL", "https://ilo.example.com:4415")
    # The iLO password is sent (Basic auth) on the very first request, so
    # never over plain HTTP.
    if not is_https_url(ILO_URL):
        log(f"ERROR: ILO_URL must start with https:// (got {ILO_URL}).")
        sys.exit(1)

    # ILO_BLOCKED_PORTS (comma-separated) lists iLO ports that must never be
    # provisioned, e.g. a non-Proxmox box (shared storage, ...) behind the
    # same iLO host - reinstalling over it would be catastrophic. Blocked
    # regardless of how it ends up in ILO_URL (typo, copy-paste, whatever),
    # independent of any later hardware-model check. Compared as actual port
    # numbers (not a substring match) so e.g. ":44110" can't match "4411".
    blocked = os.environ.get("ILO_BLOCKED_PORTS", "")
    entries = [e.strip() for e in blocked.split(",") if e.strip()]
    if not all(e.isdigit() and 1 <= int(e) <= 65535 for e in entries):
        log(f"ERROR: ILO_BLOCKED_PORTS must be a comma-separated list of port numbers (got {blocked!r}).")
        sys.exit(1)
    blocked_ports = {int(e) for e in entries}
    # No explicit port in the URL means HTTPS's default 443.
    if (urllib.parse.urlsplit(ILO_URL).port or 443) in blocked_ports:
        log(f"ERROR: ILO_URL ({ILO_URL}) targets a port listed in ILO_BLOCKED_PORTS - refusing to run.")
        sys.exit(1)

    ILO_USER = require_env("ILO_USER", "ilo-admin")
    PVE_GATEWAY = require_env("PVE_GATEWAY", "192.0.2.1")
    PVE_DNS = require_env("PVE_DNS", "198.51.100.10")
    PVE_DOMAIN = require_env("PVE_DOMAIN", "example.internal")
    PVE_MAILTO = require_env("PVE_MAILTO", "admin@example.internal")
    AZURE_STORAGE_ACCOUNT = require_env("AZURE_STORAGE_ACCOUNT")

    ILO_PASSWORD = require_env("ILO_PASSWORD")
    PVE_ROOT_PASSWORD = require_env("PVE_ROOT_PASSWORD")
    # Checked here because root-password-hashed bypasses the installer's own
    # plaintext checks (min. 8 chars), and `openssl passwd -stdin` silently
    # truncates at 256 bytes and reads only one line.
    pw_len = len(PVE_ROOT_PASSWORD.encode("utf-8"))
    if "\n" in PVE_ROOT_PASSWORD or "\r" in PVE_ROOT_PASSWORD or not 8 <= pw_len <= 256:
        log("ERROR: PVE_ROOT_PASSWORD must be a single line of 8-256 bytes.")
        sys.exit(1)
    AZURE_SAS_TOKEN = require_env("AZURE_SAS_TOKEN")

    # Defaults - no CI/CD variable needed; setting one just overrides the default.
    PROXMOX_ISO_BASE_URL = env_default("PROXMOX_ISO_BASE_URL", "https://enterprise.proxmox.com/iso/").rstrip("/") + "/"
    if not is_https_url(PROXMOX_ISO_BASE_URL):
        log(f"ERROR: PROXMOX_ISO_BASE_URL must start with https:// (got {PROXMOX_ISO_BASE_URL}).")
        sys.exit(1)
    PROXMOX_MAJOR = int(env_default("PROXMOX_MAJOR", "9"))
    # Optional pin: set BOTH to skip the "latest release" lookup entirely.
    # Left unset (the normal case), resolve_stock_iso() picks the newest
    # release itself and these stay None until then.
    PROXMOX_ISO_NAME = os.environ.get("PROXMOX_ISO_NAME") or None
    PROXMOX_ISO_SHA256 = os.environ.get("PROXMOX_ISO_SHA256") or None
    if bool(PROXMOX_ISO_NAME) != bool(PROXMOX_ISO_SHA256):
        log("ERROR: PROXMOX_ISO_NAME and PROXMOX_ISO_SHA256 must be set together (or neither).")
        sys.exit(1)
    if PROXMOX_ISO_NAME:
        PROXMOX_ISO_SHA256 = PROXMOX_ISO_SHA256.lower()
        m = _ISO_NAME_RE.match(PROXMOX_ISO_NAME)
        if not m or int(m.group(1)) != PROXMOX_MAJOR or not re.fullmatch(r"[0-9a-f]{64}", PROXMOX_ISO_SHA256):
            log(
                f"ERROR: PROXMOX_ISO_NAME/PROXMOX_ISO_SHA256 must be an amd64 proxmox-ve_{PROXMOX_MAJOR}.x-y.iso and a 64-hex sha256."
            )
            sys.exit(1)
    if os.environ.get("PROXMOX_BASE_ISO_URL"):
        log(
            "ERROR: PROXMOX_BASE_ISO_URL is no longer supported - use PROXMOX_ISO_NAME + PROXMOX_ISO_SHA256 to pin a release (see README)."
        )
        sys.exit(1)
    PROXMOX_KEYRING = env_default("PROXMOX_KEYRING", "/usr/share/keyrings/proxmox-release-trixie.gpg")
    AZURE_CONTAINER = env_default("AZURE_CONTAINER", "iso")
    PVE_TIMEZONE = env_default("PVE_TIMEZONE", "Europe/Zurich")
    PVE_KEYBOARD = env_default("PVE_KEYBOARD", "de-ch")
    PVE_COUNTRY = env_default("PVE_COUNTRY", "ch")
    PVE_NIC_INDEX = env_default("PVE_NIC_INDEX", "1")
    # All fleet servers carry the identical disk (model+capacity checked
    # live on several of them) - filtering by model instead of the
    # per-unit-unique serial number means we never need to wait for iLO's
    # storage/drive inventory at all, which used to take 10s-180s per run.
    PVE_DISK_MODEL = env_default("PVE_DISK_MODEL", "Samsung SSD 870 QVO 1TB")

    _AUTH_HEADER = "Basic " + base64.b64encode(f"{ILO_USER}:{ILO_PASSWORD}".encode()).decode()


# Last known release - only used when the SHA256SUMS lookup below fails
# (e.g. enterprise.proxmox.com blocking this runner's cloud IP range), so a
# run still has a verified, pinned target instead of failing outright.
FALLBACK_ISO_NAME = "proxmox-ve_9.3-1.iso"
FALLBACK_ISO_SHA256 = "cc4fc97b9ac3a5712ad5b665195753571923a18e11f5c2ebb113fdc29a50bd36"

_SHA256SUMS_MAX_BYTES = 1024 * 1024
_ISO_NAME_RE = re.compile(r"^proxmox-ve_(\d+)\.(\d+)-(\d+)\.iso$")


def latest_release(sha256sums_text, major):
    """Returns (iso_name, sha256) of the newest amd64 proxmox-ve_<major>.x-y.iso
    listed in a SHA256SUMS file, or None if there is none. arm64 and other
    variants don't match the strict name pattern and are ignored."""
    best = None
    for line in sha256sums_text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*")
        m = _ISO_NAME_RE.match(name)
        if not m or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        version = tuple(int(x) for x in m.groups())
        if version[0] != major:
            continue
        if best is None or version > best[0]:
            best = (version, name, digest)
    return (best[1], best[2]) if best else None


def _fetch_small(url):
    """GET url with TLS verification, capped at _SHA256SUMS_MAX_BYTES."""
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read(_SHA256SUMS_MAX_BYTES + 1)
    if len(data) > _SHA256SUMS_MAX_BYTES:
        raise ValueError(f"{url} is larger than {_SHA256SUMS_MAX_BYTES} bytes")
    return data


def signature_ok(data, signature):
    """True if `signature` (detached, possibly multi-signature) contains at
    least one valid signature over `data` by a key in PROXMOX_KEYRING and no
    bad one. Proxmox signs SHA256SUMS with several release keys; the ones not
    in our keyring show up as ERRSIG (unknown key) and make gpgv exit non-zero,
    so the status lines are evaluated instead of the exit code."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        data_path, sig_path = os.path.join(d, "SHA256SUMS"), os.path.join(d, "SHA256SUMS.asc")
        with open(data_path, "wb") as f:
            f.write(data)
        with open(sig_path, "wb") as f:
            f.write(signature)
        res = subprocess.run(
            ["gpgv", "--status-fd", "1", "--keyring", PROXMOX_KEYRING, sig_path, data_path],
            capture_output=True,
            text=True,
        )
    status = {
        line.split()[1] for line in res.stdout.splitlines() if line.startswith("[GNUPG:] ") and len(line.split()) > 1
    }
    # ERRSIG (co-signer key not in our keyring) is tolerated; an expired or
    # revoked signature/key from our own keyring is not.
    rejected = {"BADSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG"}
    return {"GOODSIG", "VALIDSIG"} <= status and not (status & rejected)


def resolve_stock_iso():
    """Decides which stock ISO (name + expected sha256) this run uses:
    an explicit pin if configured, else the newest release from Proxmox's
    published SHA256SUMS, else the hardcoded last-known release."""
    if PROXMOX_ISO_NAME:
        log(f"Using pinned stock ISO {PROXMOX_ISO_NAME} (PROXMOX_ISO_NAME/PROXMOX_ISO_SHA256).")
        return PROXMOX_ISO_NAME, PROXMOX_ISO_SHA256
    sums_url = PROXMOX_ISO_BASE_URL + "SHA256SUMS"
    found = None
    try:
        # This checksum list is the trust anchor for the whole ISO, so it is
        # fetched with TLS verification (unlike the self-signed iLO calls)
        # AND its detached signature is checked against the Proxmox release
        # key that the CI image already trusts for its apt repo.
        sums = _fetch_small(sums_url)
        sig = _fetch_small(sums_url + ".asc")
        if signature_ok(sums, sig):
            found = latest_release(sums.decode("utf-8", "replace"), PROXMOX_MAJOR)
        else:
            log(f"WARNING: {sums_url} has no valid signature from {PROXMOX_KEYRING} - ignoring it.")
    except Exception as e:
        log(f"WARNING: could not fetch {sums_url} ({e}).")
    if found:
        # A stale (but validly signed) mirror must never downgrade below the
        # last release known to this code.
        fb = _ISO_NAME_RE.match(FALLBACK_ISO_NAME).groups()
        if int(fb[0]) == PROXMOX_MAJOR and tuple(map(int, fb)) > tuple(map(int, _ISO_NAME_RE.match(found[0]).groups())):
            log(
                f"WARNING: {sums_url} only lists {found[0]}, older than the known {FALLBACK_ISO_NAME} - using the latter."
            )
            return FALLBACK_ISO_NAME, FALLBACK_ISO_SHA256
        log(f"Latest Proxmox VE {PROXMOX_MAJOR} release: {found[0]}")
        return found
    if int(_ISO_NAME_RE.match(FALLBACK_ISO_NAME).group(1)) != PROXMOX_MAJOR:
        log(
            f"ERROR: no Proxmox VE {PROXMOX_MAJOR} release found online and the fallback {FALLBACK_ISO_NAME} is a different major - aborting."
        )
        sys.exit(1)
    log(f"WARNING: no Proxmox VE {PROXMOX_MAJOR} release found online - falling back to {FALLBACK_ISO_NAME}.")
    return FALLBACK_ISO_NAME, FALLBACK_ISO_SHA256


def ilo_request(method, path, body=None, timeout=20):
    """Low-level Redfish call. Returns (status_code, parsed_json_or_None).
    Raises only on network-level failures (timeout, connection refused) -
    HTTP error status codes (4xx) are returned normally, not raised, so
    callers can inspect iLO's own error payload (e.g. UnableToModifyDuringSystemPOST)."""
    url = ILO_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": _AUTH_HEADER,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            raw = r.read()
            parsed = json.loads(raw) if raw else None
            return r.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, parsed


def ilo_get(path, timeout=20):
    status, body = ilo_request("GET", path, timeout=timeout)
    return body


def ilo_patch(path, body, timeout=20):
    return ilo_request("PATCH", path, body=body, timeout=timeout)


_CRYPT_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def root_password_hash(password, hostname):
    """SHA-512-crypt ($6$) hash for answer.toml's root-password-hashed, so the
    custom ISO (publicly readable on Azure Blob, mounted via iLO) never
    carries the plaintext fleet password.

    The salt is derived from the hostname instead of being random: a random
    salt would change the rendered answer.toml - and thus the content-
    addressed ISO name - on every run, defeating the ISO cache. It is still
    unique per server and deliberately independent of the password, so it
    leaks nothing about it. Uses openssl (in the CI image) because Python
    3.13+ no longer has the crypt module."""
    digest = hashlib.sha256(f"pve-root-salt:{hostname}".encode()).digest()
    salt = "".join(_CRYPT_ALPHABET[b % 64] for b in digest[:16])
    return sha512_crypt(password, salt)


def sha512_crypt(password, salt):
    """SHA-512-crypt via `openssl passwd -6` with the given salt."""
    res = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    hashed = res.stdout.strip()
    if not re.fullmatch(r"\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}", hashed):
        raise RuntimeError("openssl passwd returned an unexpected hash format")
    return hashed


def custom_iso_name(hostname, stock_iso_name, stock_iso_sha256, answer_toml):
    """Content-addressed blob name of the per-host ISO: the stock release is
    visible in the name, and the hash covers the stock ISO's sha256 plus the
    rendered answer.toml, so any config or release change yields a new name."""
    stock_version = stock_iso_name[len("proxmox-ve_") : -len(".iso")]
    answer_hash = hashlib.sha256((stock_iso_sha256 + "\n" + answer_toml).encode()).hexdigest()[:16]
    return f"{hostname}-pve{stock_version}-{answer_hash}.iso"


def redfish_message_id(body):
    """First MessageId from a Redfish error payload (e.g.
    'iLO.2.x.UnableToModifyDuringSystemPOST'), or '' if there is none."""
    try:
        return body["error"]["@Message.ExtendedInfo"][0]["MessageId"]
    except KeyError, IndexError, TypeError:
        return ""


def clear_boot_override():
    """One attempt to disable the persistent boot override, verified by
    reading it back (a 2xx alone could be a merely queued change).
    Returns (ok, status, message_id); raises only on network errors."""
    status, body = ilo_patch("/redfish/v1/Systems/1/", {"Boot": {"BootSourceOverrideEnabled": "Disabled"}})
    if not 200 <= status < 300:
        return False, status, redfish_message_id(body)
    enabled = (ilo_get("/redfish/v1/Systems/1/") or {}).get("Boot", {}).get("BootSourceOverrideEnabled")
    return enabled == "Disabled", status, f"read back BootSourceOverrideEnabled={enabled}"


def abort_and_clear_override(msg):
    """Fatal error after the Continuous/Cd override was set: try to undo it
    so a later reboot doesn't boot whatever is (or isn't) mounted."""
    log(f"ERROR: {msg}")
    result = "no attempt made"
    for _ in range(6):  # ~1 min: the host may be mid-POST right now
        try:
            ok, status, msg_id = clear_boot_override()
            if ok:
                log("Boot override cleared.")
                sys.exit(1)
            result = f"HTTP {status} {msg_id}"
        except Exception as e:
            result = str(e)
        time.sleep(10)
    log(f"WARNING: could not clear the boot override ({result}) - clear it manually in iLO NOW ({ILO_URL}).")
    sys.exit(1)


def ilo_post(path, body, timeout=20):
    return ilo_request("POST", path, body=body, timeout=timeout)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _print_progress(done, total):
    if total:
        pct = int(done * 100 / total)
        print(f"\r{pct:3d}%", end="", flush=True)


def head_ok(url, timeout=15):
    """True if a plain HEAD against url returns 2xx."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def download(url, dest_path, timeout=300):
    """Streams url to dest_path with integer-percent progress. Returns True/False."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            if not (200 <= r.status < 300):
                return False
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            last_pct = -1
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if pct != last_pct:
                        _print_progress(done, total)
                        last_pct = pct
            print()
            return True
    except Exception as e:
        print()
        log(f"Download failed: {e}")
        return False


def upload_put(url, file_path, extra_headers=None):
    """Streams file_path to url via HTTP PUT (Azure 'Put Blob', BlockBlob)
    with integer-percent progress, using http.client directly so the file
    is sent in chunks rather than loaded into memory."""
    parsed = urllib.parse.urlsplit(url)
    size = os.path.getsize(file_path)
    headers = {"x-ms-blob-type": "BlockBlob", "Content-Length": str(size)}
    if extra_headers:
        headers.update(extra_headers)

    conn = http.client.HTTPSConnection(parsed.netloc, context=_SSL_CTX, timeout=300)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    conn.putrequest("PUT", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()

    done = 0
    last_pct = -1
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            conn.send(chunk)
            done += len(chunk)
            pct = int(done * 100 / size) if size else 0
            if pct != last_pct:
                _print_progress(done, size)
                last_pct = pct
    print()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    if not (200 <= resp.status < 300):
        raise RuntimeError(f"upload failed: HTTP {resp.status}")


def main():
    load_config()

    # Resolved before touching the target's boot settings, so a lookup
    # failure that aborts the run leaves the server untouched.
    log(f"=== [{PVE_HOSTNAME}] Resolving the stock Proxmox ISO release ===")
    stock_iso_name, stock_iso_sha256 = resolve_stock_iso()
    stock_version = stock_iso_name[len("proxmox-ve_") : -len(".iso")]

    log(f"=== [{PVE_HOSTNAME}] Verifying hardware model at {ILO_URL} ===")
    sysdata = ilo_get("/redfish/v1/Systems/1/")
    model = sysdata.get("Model", "")
    if model != "ProLiant DL160 Gen10":
        log(f"ERROR: unexpected model '{model}' at {ILO_URL} - refusing to provision.")
        sys.exit(1)
    log(f"Model OK: {model}")

    log(f"=== [{PVE_HOSTNAME}] Setting a PERSISTENT (not one-time) boot override to virtual CD ===")
    # A previous deployment may have already installed Proxmox on this box
    # (its own UEFI boot entry, e.g. "proxmox", could still be first in the
    # persistent BootOrder). We cannot cleanly delete individual firmware
    # boot entries via Redfish, and don't need to: BootSourceOverrideEnabled
    # "Continuous" takes precedence over the entire BootOrder list on every
    # single boot from here on, not just the next one - set here, right at
    # the start, so it's already in place no matter how/when the host next
    # gets power-cycled. Cleared back to "Disabled" partway through the
    # later wait loop (not simply "at the end" - see the comment there for
    # why), so the installer's own internal reboot into the freshly-installed
    # OS isn't affected by it, and so normal boots work afterwards.
    ilo_patch(
        "/redfish/v1/Systems/1/",
        {"Boot": {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Continuous"}},
    )
    boot = ilo_get("/redfish/v1/Systems/1/").get("Boot", {})
    boot_check = (boot.get("BootSourceOverrideEnabled"), boot.get("BootSourceOverrideTarget"))
    log(f"BootSourceOverrideEnabled/Target: {boot_check[0]} {boot_check[1]}")
    if boot_check != ("Continuous", "Cd"):
        log(
            f"ERROR: persistent boot override did not apply as expected (got {boot_check}, expected ('Continuous', 'Cd'))."
        )
        sys.exit(1)

    log(f"=== [{PVE_HOSTNAME}] De-prioritizing local-disk boot entries in the persistent BootOrder ===")
    # Confirmed in practice (not just theoretically): the Continuous/Cd
    # override above is NOT a 100% guarantee the firmware actually boots the
    # virtual CD - if the network-backed virtual media isn't ready to
    # respond within the firmware's own internal patience, it can silently
    # fall through to the next entry in the real persistent BootOrder
    # instead. On a box with a leftover install from a previous deployment,
    # that fallback entry boots the OLD OS, not a visible failure.
    # Mitigation: push every disk-boot entry (generic "Embedded SATA ... HDD"
    # as well as any specific OS's own UEFI entry, e.g. "proxmox") to the
    # very end of BootOrder, so a failed override attempt falls through to
    # USB/PXE first - which fail cleanly with nothing attached - instead of
    # silently booting stale data. Computed generically from UefiDevicePath
    # (anything starting with "HD(" is disk-boot), not hardcoded Boot####
    # IDs or entry names, since those vary per server and per how many
    # previous deployments left something behind.
    #
    # `boot` is still the response from the read-back verification just
    # above - no need for another GET, it already carries BootOrder.
    boot_order = boot.get("BootOrder", [])
    options = ilo_get("/redfish/v1/Systems/1/BootOptions/").get("Members", [])
    disk_refs = set()
    for m in options:
        detail = ilo_get(m["@odata.id"])
        if detail.get("UefiDevicePath", "").startswith("HD("):
            disk_refs.add(detail.get("BootOptionReference"))
    non_disk = [b for b in boot_order if b not in disk_refs]
    disk = [b for b in boot_order if b in disk_refs]
    new_boot_order = non_disk + disk
    log(f"Reordered BootOrder: {new_boot_order}")
    ilo_patch("/redfish/v1/Systems/1/", {"Boot": {"BootOrder": new_boot_order}})

    log(f"=== [{PVE_HOSTNAME}] Discovering NIC MAC (EthernetInterfaces/{PVE_NIC_INDEX}) ===")
    mac = ilo_get(f"/redfish/v1/Systems/1/EthernetInterfaces/{PVE_NIC_INDEX}/")["MACAddress"]
    mac_filter = "*" + mac.replace(":", "")
    log(f"NIC MAC: {mac} -> filter {mac_filter}")

    log(f"=== [{PVE_HOSTNAME}] Rendering answer.toml ===")
    fqdn = f"{PVE_HOSTNAME}.{PVE_DOMAIN}"
    with open("answer.toml.template") as f:
        template = string.Template(f.read())
    answer_toml = template.substitute(
        PVE_KEYBOARD=PVE_KEYBOARD,
        PVE_COUNTRY=PVE_COUNTRY,
        FQDN=fqdn,
        PVE_MAILTO=PVE_MAILTO,
        PVE_TIMEZONE=PVE_TIMEZONE,
        PVE_ROOT_PASSWORD_HASHED=root_password_hash(PVE_ROOT_PASSWORD, PVE_HOSTNAME),
        PVE_STATIC_IP=PVE_STATIC_IP,
        PVE_DNS=PVE_DNS,
        PVE_GATEWAY=PVE_GATEWAY,
        MAC_FILTER=mac_filter,
        # udev's ID_MODEL (what the installer's filter matches against)
        # replaces spaces with underscores - "Samsung SSD 870 QVO 1TB" is
        # "Samsung_SSD_870_QVO_1TB" there (confirmed on real hardware).
        PVE_DISK_MODEL=PVE_DISK_MODEL.replace(" ", "_"),
    )
    with open("answer.toml", "w") as f:
        f.write(answer_toml)
    subprocess.run(["proxmox-auto-install-assistant", "validate-answer", "answer.toml"], check=True)

    log(f"=== [{PVE_HOSTNAME}] Checking Azure Blob for an already-built matching custom ISO ===")
    # The custom ISO's blob name is content-addressed: it includes the stock
    # release and a hash of the rendered answer.toml (hostname, IP, root
    # password, MAC/disk filters, ...) plus the stock ISO's sha256, so a new
    # Proxmox release never reuses an ISO built from an older one. If
    # nothing about this server's config or the release changed since the
    # last run, a matching ISO already sitting on Azure Blob can be reused
    # directly - skipping BOTH the ~1.6GB stock-ISO download AND the
    # prepare-iso rebuild, which are the only genuinely slow parts of this
    # job. Any config change naturally produces a different hash, so there's
    # no stale-cache risk and no manual invalidation needed.
    iso_name = custom_iso_name(PVE_HOSTNAME, stock_iso_name, stock_iso_sha256, answer_toml)
    iso_url = f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{AZURE_CONTAINER}/{iso_name}"

    if head_ok(iso_url):
        log("Found an existing custom ISO for this exact config - reusing it, skipping the build.")
    else:
        log("No matching custom ISO cached - building a fresh one.")

        log(f"=== [{PVE_HOSTNAME}] Fetching stock Proxmox ISO {stock_iso_name} (SHA256-verified) ===")
        # enterprise.proxmox.com actively blocks/throttles requests from
        # cloud datacenter IP ranges (confirmed empirically: GitLab's shared
        # runner got a 215-byte response where a normal residential IP gets
        # the real 1.6GB file) - the same class of issue hit earlier with
        # Azure's own server-side "copy from URL" against the same host. So
        # the primary source here is our own Azure Blob mirror (populated
        # once from a non-blocked network), with enterprise.proxmox.com only
        # as a fallback for when that mirror is missing or fails verification.
        # The mirror blob keeps Proxmox's own file name, so the release is
        # visible in the container and each release gets its own blob.
        stock_iso_blob_url = f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{AZURE_CONTAINER}/{stock_iso_name}"
        have_valid_source = False
        if download(stock_iso_blob_url, "source.iso"):
            if sha256_file("source.iso") == stock_iso_sha256:
                log("Using verified stock ISO from the Azure Blob mirror.")
                have_valid_source = True
            else:
                log("Azure Blob mirror failed sha256 verification - falling back to the authoritative source.")
        else:
            log("No usable Azure Blob mirror - falling back to the authoritative source.")

        if not have_valid_source:
            if not download(PROXMOX_ISO_BASE_URL + stock_iso_name, "source.iso"):
                log("ERROR: failed to download the stock ISO from the authoritative source.")
                sys.exit(1)
            actual = sha256_file("source.iso")
            if actual != stock_iso_sha256:
                log(f"ERROR: downloaded ISO's sha256 ({actual}) does not match the expected {stock_iso_sha256}.")
                log("Either the download is corrupted, or blocked/throttled - this runner may need")
                log(f"{stock_iso_name} seeded on the Azure Blob mirror from a non-cloud IP (see README).")
                sys.exit(1)
            log("Downloaded ISO verified (sha256 matches) - re-populating the Azure Blob mirror.")
            upload_put(f"{stock_iso_blob_url}?{AZURE_SAS_TOKEN}", "source.iso")

        log(f"=== [{PVE_HOSTNAME}] Embedding answer file into a custom ISO ===")
        subprocess.run(
            [
                "proxmox-auto-install-assistant",
                "prepare-iso",
                "source.iso",
                # Generic local name: prepare-iso echoes it, and the real blob
                # name is an anonymous download link (see below).
                "--fetch-from",
                "iso",
                "--answer-file",
                "answer.toml",
                "--output",
                "custom.iso",
            ],
            check=True,
        )

        log(f"=== [{PVE_HOSTNAME}] Uploading custom ISO to Azure Blob Storage ===")
        upload_put(f"{iso_url}?{AZURE_SAS_TOKEN}", "custom.iso")
    # The blob URL itself is deliberately not logged: it's an anonymous
    # download link to an ISO carrying the root-password hash, and job logs
    # may be readable by more people than the fleet secrets (e.g. a public
    # GitHub mirror).
    log(f"Custom ISO ready on Azure Blob ({PVE_HOSTNAME}, Proxmox VE {stock_version}).")

    log(f"=== [{PVE_HOSTNAME}] Ejecting any previously mounted virtual media ===")
    # Best-effort: nothing may be mounted yet, and the slot being empty/already
    # ejected is not an error - the following InsertMedia is what matters.
    try:
        ilo_post("/redfish/v1/Managers/1/VirtualMedia/2/Actions/VirtualMedia.EjectMedia/", {})
    except Exception as e:
        log(f"(no previous media to eject, or eject failed - continuing: {e})")

    log(f"=== [{PVE_HOSTNAME}] Mounting the custom ISO as virtual CD/DVD ===")
    status, body = ilo_post(
        "/redfish/v1/Managers/1/VirtualMedia/2/Actions/VirtualMedia.InsertMedia/", {"Image": iso_url}
    )
    if not 200 <= status < 300:
        abort_and_clear_override(f"InsertMedia failed (HTTP {status} {redfish_message_id(body)}).")
    # iLO can take a moment to reflect the new media - poll briefly.
    media = {}
    for _ in range(6):
        try:
            media = ilo_get("/redfish/v1/Managers/1/VirtualMedia/2/") or {}
        except Exception as e:
            log(f"(transient error reading back virtual media, will retry: {e})")
            media = {}
        if media.get("Inserted") and media.get("Image") == iso_url:
            break
        time.sleep(5)
    else:
        # The returned Image URL is deliberately not logged (could carry a token).
        abort_and_clear_override(
            f"virtual CD does not show the custom ISO after InsertMedia (Inserted={media.get('Inserted')}, Image matches: {media.get('Image') == iso_url})."
        )

    log(f"=== [{PVE_HOSTNAME}] Re-confirming the persistent boot override is still Continuous/Cd ===")
    boot = ilo_get("/redfish/v1/Systems/1/").get("Boot", {})
    boot_check = (boot.get("BootSourceOverrideEnabled"), boot.get("BootSourceOverrideTarget"))
    log(f"BootSourceOverrideEnabled/Target: {boot_check[0]} {boot_check[1]}")
    if boot_check != ("Continuous", "Cd"):
        log(
            f"ERROR: boot override is no longer 'Continuous Cd' (got {boot_check}) - refusing to reboot into an uncertain boot target."
        )
        sys.exit(1)

    log(f"=== [{PVE_HOSTNAME}] Power-cycling host ===")
    power = ilo_get("/redfish/v1/Systems/1/")["PowerState"]
    reset_type = "ForceRestart" if power == "On" else "On"
    status, body = ilo_post("/redfish/v1/Systems/1/Actions/ComputerSystem.Reset/", {"ResetType": reset_type})
    if not 200 <= status < 300:
        abort_and_clear_override(
            f"{reset_type} failed (HTTP {status} {redfish_message_id(body)}) - the installer was never started."
        )

    log(f"=== [{PVE_HOSTNAME}] Waiting ~5 min, then clearing the persistent boot override ===")
    # The job no longer waits for the Proxmox web UI: GitLab's shared runners
    # are blocked from reaching port 8006 on the fleet network (confirmed on
    # a real server - reachable from the operator's network, never from the runner),
    # so that poll could only ever time out. The one thing that MUST still
    # happen from here is clearing the override in time.
    #
    # Confirmed in practice: clearing BootSourceOverrideEnabled only at the
    # end of the install is too late. The installer does its OWN internal
    # reboot (from the installer environment into the freshly-installed OS),
    # and Continuous means "every boot", not just the first, so that second
    # reboot tried the virtual CD again too, nearly re-running the whole
    # install over the just-installed disk. So it's cleared after ~5 min -
    # past even a slow virtual-media boot into the installer, well before
    # the install can finish. iLO refuses this PATCH with
    # "UnableToModifyDuringSystemPOST" whenever the host happens to be
    # mid-boot at that exact moment (also confirmed in practice) - transient,
    # so it's retried every 10s.
    #
    # The virtual media itself stays mounted: the running installer still
    # reads from it, and it's harmless afterwards (the override is cleared,
    # so the host boots from disk). The next run ejects it first anyway.
    reset_at = time.monotonic()
    time.sleep(300)
    # Wall-clock deadline (not an iteration count): each PATCH can itself
    # take up to its 20s timeout while the host reboots.
    deadline = reset_at + 15 * 60
    override_cleared = False
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            ok, status, msg_id = clear_boot_override()
        except Exception as e:
            # The host is actively rebooting around this point, so a transient
            # connection error/timeout is expected, not fatal.
            ok, last_error = False, f"network error: {e}"
            log(f"(transient error clearing boot override, will retry: {e})")
        else:
            if ok:
                log(
                    f"Cleared the persistent boot override after {int(time.monotonic() - reset_at)}s - past the risky first boot, well before the installer's own later internal reboot."
                )
                override_cleared = True
                break
            last_error = f"HTTP {status} {msg_id}".strip()
            if status in (401, 403):
                break  # permanent - retrying won't help
        time.sleep(min(10, max(0, deadline - time.monotonic())))

    ip_only = PVE_STATIC_IP.split("/")[0]
    if not override_cleared:
        log(f"ERROR: could not clear the persistent boot override (last result: {last_error}).")
        log(f"Clear it manually NOW in iLO ({ILO_URL}: Boot Order -> One-Time/Continuous boot override -> None),")
        log("otherwise the installer's own reboot boots the virtual CD again and reinstalls over the fresh disk.")
        sys.exit(1)

    log(f"=== [{PVE_HOSTNAME}] Install handed off - not waiting for completion ===")
    log("The unattended install keeps running on the host on its own (typically ~15-25 min, incl. one reboot).")
    log(
        f"Check the result at https://{ip_only}:8006/ (root / PVE_ROOT_PASSWORD), or via the iLO Remote Console at {ILO_URL}."
    )
    log("The custom ISO stays mounted as virtual media (the installer still needs it). Once the web UI is up,")
    log("eject it in iLO (Virtual Media -> Eject): a later boot that falls")
    log("through to the virtual CD would start the unattended install again. The next run ejects it anyway.")


if __name__ == "__main__":
    main()
