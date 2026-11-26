"""Unit tests for the pure logic in provision.py - no iLO, Azure or network access."""

import hashlib
import os
import shutil
import subprocess

import pytest

import provision as P

# Locally, tests needing system tools are skipped if a tool is missing; in CI
# (CI=true) they always run, so a missing tool fails instead of hiding.
IN_CI = os.environ.get("CI", "").strip().lower() == "true"


def requires(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    return pytest.mark.skipif(bool(missing) and not IN_CI, reason=f"not installed: {', '.join(missing)}")


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

# --- latest_release ----------------------------------------------------------


def test_latest_release_compares_versions_numerically():
    sums = f"{SHA_A}  proxmox-ve_9.9-2.iso\n{SHA_B}  proxmox-ve_9.10-1.iso\n{SHA_C}  proxmox-ve_9.2-1.iso\n"
    assert P.latest_release(sums, 9) == ("proxmox-ve_9.10-1.iso", SHA_B)


def test_latest_release_ignores_arm64_other_majors_and_garbage():
    sums = "\n".join(
        [
            f"{SHA_A}  proxmox-ve_9.3-1.iso",
            f"{SHA_B}  proxmox-ve_9.4-1-arm64.iso",
            f"{SHA_C}  proxmox-ve_10.0-1.iso",
            "not-a-hash  proxmox-ve_9.9-1.iso",
            "garbage line with too many fields here",
            "",
        ]
    )
    assert P.latest_release(sums, 9) == ("proxmox-ve_9.3-1.iso", SHA_A)


def test_latest_release_normalizes_digest_and_binary_marker():
    sums = f"{SHA_A.upper()} *proxmox-ve_9.3-1.iso\n"
    assert P.latest_release(sums, 9) == ("proxmox-ve_9.3-1.iso", SHA_A)


def test_latest_release_none_when_no_match():
    assert P.latest_release(f"{SHA_A}  proxmox-ve_8.4-1.iso\n", 9) is None


# --- resolve_stock_iso -------------------------------------------------------


@pytest.fixture
def resolver(monkeypatch):
    """resolve_stock_iso() with config set and the network/signature check stubbed."""
    monkeypatch.setattr(P, "PROXMOX_ISO_BASE_URL", "https://iso.example.com/")
    monkeypatch.setattr(P, "PROXMOX_MAJOR", 9)
    monkeypatch.setattr(P, "PROXMOX_ISO_NAME", None)
    monkeypatch.setattr(P, "PROXMOX_ISO_SHA256", None)
    monkeypatch.setattr(P, "PROXMOX_KEYRING", "/nonexistent")
    monkeypatch.setattr(P, "FALLBACK_ISO_NAME", "proxmox-ve_9.3-1.iso")
    monkeypatch.setattr(P, "FALLBACK_ISO_SHA256", SHA_A)
    monkeypatch.setattr(P, "log", lambda msg: None)

    calls = {"fetch": [], "verify": []}

    def setup(sums=None, signed=True, error=None):
        def fetch(url):
            calls["fetch"].append(url)
            if error:
                raise error
            return sums.encode() if url.endswith("SHA256SUMS") else b"detached-signature"

        def verify(data, sig):
            calls["verify"].append((data, sig))
            return signed

        monkeypatch.setattr(P, "_fetch_small", fetch)
        monkeypatch.setattr(P, "signature_ok", verify)
        return P.resolve_stock_iso

    setup.calls = calls
    return setup


def test_resolve_uses_pin_without_network(resolver, monkeypatch):
    resolve = resolver(sums="")
    monkeypatch.setattr(P, "PROXMOX_ISO_NAME", "proxmox-ve_9.2-1.iso")
    monkeypatch.setattr(P, "PROXMOX_ISO_SHA256", SHA_B)
    assert resolve() == ("proxmox-ve_9.2-1.iso", SHA_B)
    assert resolver.calls["fetch"] == []


def test_resolve_picks_newest_signed_release(resolver):
    sums = f"{SHA_A}  proxmox-ve_9.3-1.iso\n{SHA_B}  proxmox-ve_9.4-1.iso\n"
    resolve = resolver(sums=sums)
    assert resolve() == ("proxmox-ve_9.4-1.iso", SHA_B)
    assert resolver.calls["fetch"] == ["https://iso.example.com/SHA256SUMS", "https://iso.example.com/SHA256SUMS.asc"]
    # The checksum list and its detached signature, in that order.
    assert resolver.calls["verify"] == [(sums.encode(), b"detached-signature")]


def test_resolve_ignores_unsigned_list(resolver):
    resolve = resolver(sums=f"{SHA_B}  proxmox-ve_9.4-1.iso\n", signed=False)
    assert resolve() == ("proxmox-ve_9.3-1.iso", SHA_A)


def test_resolve_falls_back_when_unreachable(resolver):
    resolve = resolver(error=OSError("blocked"))
    assert resolve() == ("proxmox-ve_9.3-1.iso", SHA_A)


def test_resolve_never_downgrades_below_fallback(resolver):
    resolve = resolver(sums=f"{SHA_B}  proxmox-ve_9.2-1.iso\n")
    assert resolve() == ("proxmox-ve_9.3-1.iso", SHA_A)


def test_resolve_aborts_when_fallback_is_other_major(resolver, monkeypatch):
    resolve = resolver(error=OSError("blocked"))
    monkeypatch.setattr(P, "PROXMOX_MAJOR", 10)
    with pytest.raises(SystemExit):
        resolve()


# --- signature_ok (real gpg/gpgv, temporary keys) ----------------------------

needs_gpg = requires("gpg", "gpgv")


def _gpg(home, *args, data=None):
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "", *args],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def gpg_keys(tmp_path_factory):
    """Two throwaway signing keys; returns (gpg home, [(fingerprint, keyring path), ...])."""
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    keys = []
    for name in ("release", "other"):
        _gpg(home, "--quick-gen-key", f"{name} <{name}@example.invalid>", "ed25519", "sign", "never")
        listing = _gpg(home, "--with-colons", "--list-keys", f"{name}@example.invalid").decode()
        fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        keyring = home / f"{name}.gpg"
        keyring.write_bytes(_gpg(home, "--export", fpr))
        keys.append((fpr, keyring))
    return home, keys


def _sign(home, data, *fprs):
    signers = [arg for fpr in fprs for arg in ("--local-user", fpr)]
    return _gpg(home, *signers, "--armor", "--detach-sign", data=data)


@needs_gpg
def test_signature_ok_accepts_valid_signature(gpg_keys, monkeypatch):
    home, [(release, keyring), _] = gpg_keys
    monkeypatch.setattr(P, "PROXMOX_KEYRING", str(keyring))
    data = b"checksums\n"
    assert P.signature_ok(data, _sign(home, data, release))


@needs_gpg
def test_signature_ok_rejects_tampered_data(gpg_keys, monkeypatch):
    home, [(release, keyring), _] = gpg_keys
    monkeypatch.setattr(P, "PROXMOX_KEYRING", str(keyring))
    sig = _sign(home, b"checksums\n", release)
    assert not P.signature_ok(b"checksums tampered\n", sig)


@needs_gpg
def test_signature_ok_rejects_unknown_signer(gpg_keys, monkeypatch):
    home, [(_, keyring), (other, _)] = gpg_keys
    monkeypatch.setattr(P, "PROXMOX_KEYRING", str(keyring))
    data = b"checksums\n"
    assert not P.signature_ok(data, _sign(home, data, other))


@needs_gpg
def test_signature_ok_tolerates_unknown_cosigner(gpg_keys, monkeypatch):
    # Proxmox signs SHA256SUMS with several release keys; only ours is trusted.
    home, [(release, keyring), (other, _)] = gpg_keys
    monkeypatch.setattr(P, "PROXMOX_KEYRING", str(keyring))
    data = b"checksums\n"
    assert P.signature_ok(data, _sign(home, data, release, other))


# --- load_config -------------------------------------------------------------

BASE_ENV = {
    "PVE_HOSTNAME": "pve-node15",
    "PVE_STATIC_IP": "192.0.2.35/24",
    "ILO_URL": "https://ilo.example.com:4415",
    "ILO_USER": "ilo-admin",
    "PVE_GATEWAY": "192.0.2.1",
    "PVE_DNS": "198.51.100.10",
    "PVE_DOMAIN": "example.internal",
    "PVE_MAILTO": "admin@example.internal",
    "AZURE_STORAGE_ACCOUNT": "storageaccount",
    "ILO_PASSWORD": "ilo-secret",
    "PVE_ROOT_PASSWORD": "correct horse battery",
    "AZURE_SAS_TOKEN": "sv=test",
}
OPTIONAL_ENV = [
    "ILO_BLOCKED_PORTS",
    "PROXMOX_ISO_BASE_URL",
    "PROXMOX_BASE_ISO_URL",
    "PROXMOX_MAJOR",
    "PROXMOX_ISO_NAME",
    "PROXMOX_ISO_SHA256",
    "PROXMOX_KEYRING",
    "AZURE_CONTAINER",
    "PVE_TIMEZONE",
    "PVE_KEYBOARD",
    "PVE_COUNTRY",
    "PVE_NIC_INDEX",
    "PVE_DISK_MODEL",
]


@pytest.fixture
def config(monkeypatch):
    """Runs load_config() with BASE_ENV plus overrides (None removes a variable)."""
    monkeypatch.setattr(P, "log", lambda msg: None)
    for name in OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)

    def load(**overrides):
        for name, value in {**BASE_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        P.load_config()

    return load


def test_load_config_defaults(config):
    config()
    assert P.PVE_DOMAIN == "example.internal"
    assert P.PROXMOX_MAJOR == 9
    assert P.PROXMOX_ISO_NAME is None
    assert P.PROXMOX_ISO_BASE_URL == "https://enterprise.proxmox.com/iso/"
    assert P.PVE_DISK_MODEL == "Samsung SSD 870 QVO 1TB"


@pytest.mark.parametrize("name", ["PVE_DOMAIN", "PVE_MAILTO", "ILO_PASSWORD", "AZURE_SAS_TOKEN"])
def test_load_config_requires(config, name):
    with pytest.raises(SystemExit):
        config(**{name: None})


@pytest.mark.parametrize(
    ("ilo_url", "blocked"),
    [
        ("http://ilo.example.com:4415", ""),  # plain HTTP would leak the iLO password
        ("https:/ilo.example.com:4415", ""),  # typo: https scheme but no host
        ("https://ilo.example.com:4411", "4411"),
        ("https://ilo.example.com:4416", " 4411 , 4416 "),
        ("https://ilo.example.com", "443"),  # no port = 443
        ("https://ilo.example.com:4415", "4411 4416"),  # missing comma
        ("https://ilo.example.com:4415", "70000"),
        ("https://ilo.example.com:4415", "abc"),
    ],
)
def test_load_config_rejects_ilo_target(config, ilo_url, blocked):
    with pytest.raises(SystemExit):
        config(ILO_URL=ilo_url, ILO_BLOCKED_PORTS=blocked)


@pytest.mark.parametrize("ilo_url", ["https://ilo.example.com:44110", "https://ilo.example.com:4415"])
def test_load_config_allows_unblocked_port(config, ilo_url):
    config(ILO_URL=ilo_url, ILO_BLOCKED_PORTS="4411,")
    assert ilo_url == P.ILO_URL


@pytest.mark.parametrize("password", ["short", "x" * 257, "two\nlines"])
def test_load_config_rejects_bad_root_password(config, password):
    with pytest.raises(SystemExit):
        config(PVE_ROOT_PASSWORD=password)


@pytest.mark.parametrize(
    "overrides",
    [
        {"PROXMOX_ISO_NAME": "proxmox-ve_9.3-1.iso"},  # pin without checksum
        {"PROXMOX_ISO_NAME": "proxmox-ve_9.3-1-arm64.iso", "PROXMOX_ISO_SHA256": SHA_A},
        {"PROXMOX_ISO_NAME": "proxmox-ve_8.4-1.iso", "PROXMOX_ISO_SHA256": SHA_A},  # wrong major
        {"PROXMOX_ISO_NAME": "proxmox-ve_9.3-1.iso", "PROXMOX_ISO_SHA256": "abc"},
        {"PROXMOX_BASE_ISO_URL": "https://example.com/old.iso"},  # removed variable
        {"PROXMOX_ISO_BASE_URL": "http://iso.example.com/"},
        {"PROXMOX_ISO_BASE_URL": "https:/iso.example.com/"},
    ],
)
def test_load_config_rejects_bad_release_settings(config, overrides):
    with pytest.raises(SystemExit):
        config(**overrides)


def test_load_config_accepts_pin_and_normalizes_checksum(config):
    config(PROXMOX_ISO_NAME="proxmox-ve_9.3-1.iso", PROXMOX_ISO_SHA256=SHA_A.upper())
    assert (P.PROXMOX_ISO_NAME, P.PROXMOX_ISO_SHA256) == ("proxmox-ve_9.3-1.iso", SHA_A)


# --- password hashing --------------------------------------------------------

needs_openssl = requires("openssl")


@needs_openssl
def test_sha512_crypt_matches_reference_vector():
    # Test vector from the SHA-crypt specification (Drepper).
    assert P.sha512_crypt("Hello world!", "saltstring") == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1"
    )


@needs_openssl
def test_root_password_hash_is_stable_per_host_and_salted_per_host():
    a1 = P.root_password_hash("correct horse battery", "pve-node14")
    a2 = P.root_password_hash("correct horse battery", "pve-node14")
    b = P.root_password_hash("correct horse battery", "pve-node15")
    assert a1 == a2
    assert a1.split("$")[2] != b.split("$")[2]
    assert "correct horse" not in a1


@needs_openssl
def test_root_password_hash_salt_does_not_depend_on_password():
    a = P.root_password_hash("first password", "pve-node14")
    b = P.root_password_hash("second password", "pve-node14")
    assert a.split("$")[2] == b.split("$")[2]
    assert a != b


# --- custom ISO name ---------------------------------------------------------


def test_custom_iso_name_format_and_cache_key():
    name = P.custom_iso_name("pve-node14", "proxmox-ve_9.3-1.iso", SHA_A, "answer")
    expected_hash = hashlib.sha256(f"{SHA_A}\nanswer".encode()).hexdigest()[:16]
    assert name == f"pve-node14-pve9.3-1-{expected_hash}.iso"
    assert name == P.custom_iso_name("pve-node14", "proxmox-ve_9.3-1.iso", SHA_A, "answer")
    assert name != P.custom_iso_name("pve-node14", "proxmox-ve_9.3-1.iso", SHA_B, "answer")
    assert name != P.custom_iso_name("pve-node14", "proxmox-ve_9.3-1.iso", SHA_A, "changed answer")


# --- Redfish helpers ---------------------------------------------------------


def test_redfish_message_id():
    body = {"error": {"@Message.ExtendedInfo": [{"MessageId": "iLO.2.15.UnableToModifyDuringSystemPOST"}]}}
    assert P.redfish_message_id(body) == "iLO.2.15.UnableToModifyDuringSystemPOST"
    assert P.redfish_message_id(None) == ""
    assert P.redfish_message_id({"error": {}}) == ""
