# proxmox-fleet-provisioning

Unattended Proxmox VE 9 installation for a fleet of identical HPE ProLiant
DL160 Gen10 servers, managed via their iLO 5 Redfish API (e.g. one iLO per
port behind a shared host such as `ilo.example.com:<port>`).

Nothing server-specific (hostname, IP, MAC) is stored in this repo. Every
run discovers the NIC MAC address live via the iLO Redfish API, builds a
matching Proxmox
[automated-install answer file](https://pve.proxmox.com/wiki/Automated_Installation),
embeds it into a fresh copy of the latest stock Proxmox VE 9 ISO, uploads the
resulting custom ISO to Azure Blob Storage, mounts it on the target server as
virtual media via Redfish, and power-cycles the host. The whole thing is a
single Python script (`provision.py`, stdlib only - `urllib`/`http.client`,
no `requests`, no Azure CLI, no `pip install` needed in the CI image) that
talks to iLO's Redfish API and Azure Blob Storage directly over HTTP,
including both ISO transfers (SAS token as a URL query parameter), with a
whole-percent progress indicator for each.

The target disk is matched by model (`PVE_DISK_MODEL`, default `Samsung SSD
870 QVO 1TB`; spaces are turned into underscores when rendering
`answer.toml`, since that's how udev's `ID_MODEL` spells it:
`Samsung_SSD_870_QVO_1TB`), not by its per-unit-unique serial number - confirmed
identical (model and capacity) across the fleet. That means no
storage/drive inventory wait is needed at all (previously 10s-180s per run,
since iLO only populates the `Drives` array after the host has been
powered on for a while) - a meaningful chunk of every run's total time.

**Which Proxmox release**: every run fetches Proxmox's published
[`SHA256SUMS`](https://enterprise.proxmox.com/iso/SHA256SUMS) - with TLS
verification, and its detached signature `SHA256SUMS.asc` checked via `gpgv`
against the Proxmox trixie release key the CI image already trusts for its
apt repo (`PROXMOX_KEYRING`; only a good, non-expired, non-revoked
signature counts), since it's the trust anchor for the ISO - and picks the newest
`proxmox-ve_<PROXMOX_MAJOR>.x-y.iso` listed there (amd64 only, versions
compared numerically, so 9.10 > 9.9; never older than the hardcoded
fallback below). This happens before any iLO setting is touched. New point releases are therefore
picked up automatically, with their checksum taken from the same file. The
major version stays pinned (`PROXMOX_MAJOR`, default `9`) because the CI
image's `proxmox-auto-install-assistant` comes from the Debian-trixie / PVE 9
repo. If `SHA256SUMS` can't be fetched or isn't validly signed (e.g. a cloud
runner IP being blocked, see below), the run falls back to the last known release
hardcoded in `provision.py` (`FALLBACK_ISO_NAME`/`FALLBACK_ISO_SHA256`,
currently `proxmox-ve_9.3-1.iso`) with a warning (or aborts, if that
fallback doesn't match `PROXMOX_MAJOR`). Setting both
`PROXMOX_ISO_NAME` and `PROXMOX_ISO_SHA256` pins a specific release and
skips the lookup.

Azure Blob Storage (the `iso` container) is where the final, per-server
custom ISO lives - iLO's Virtual Media needs *some* HTTPS URL to mount it
from. Its blob name is content-addressed:
`<PVE_HOSTNAME>-pve<release>-<hash of the stock ISO's sha256 + the rendered
answer.toml>.iso`, e.g. `pve-node14-pve9.3-1-1a2b3c4d5e6f7a8b.iso`.
Before doing any real work, each run checks whether a blob with that exact
name already exists:

- **Cache hit** (nothing about this server's config changed since the last
  run - same IP, root password, MAC/disk filters, ...): the existing custom
  ISO is reused directly. The ~1.6GB stock-ISO download *and* the
  `prepare-iso` rebuild - the only genuinely slow parts of this job - are
  both skipped entirely.
- **Cache miss** (first run for this server, a new Proxmox release, or any
  config value changed, which changes the name): the stock ISO is fetched and
  verified against its expected sha256 - a real cryptographic check, not a size comparison,
  and a hard failure on mismatch rather than a silent fallback - then
  rebuilt into a new custom ISO and uploaded under the new hash-named blob.
  The stock ISO itself comes from a mirror blob in the same container,
  named exactly like Proxmox's own file (e.g. `proxmox-ve_9.3-1.iso`, so
  the release is visible in the container), **not** directly from
  `enterprise.proxmox.com`:
  that host actively blocks/throttles requests from cloud datacenter IP
  ranges (confirmed empirically - a GitLab shared runner got a 215-byte
  response where a normal residential IP gets the real 1.6GB file, the
  same class of issue hit earlier with Azure's own "copy from URL" against
  that host). `enterprise.proxmox.com` is only a fallback for when the
  mirror blob for the selected release is missing or fails verification, at
  which point a successful fetch populates it - but on a GitLab-hosted
  runner that fallback will likely also fail, so each new release's mirror
  blob may need to be seeded once from a non-blocked network (see "Known
  limitations").

Because the cache key is derived from the config itself, there's no manual
invalidation and no stale-cache risk - a changed password or IP simply
or a new Proxmox release simply produces a different blob, never a
wrongly-reused old one. The one
consequence: every distinct config variant used over time leaves its own
blob behind (see "Known limitations").

## Configuration

Both pipelines (GitHub Actions and GitLab CI) run the same `Dockerfile` and
`provision.py` and use the same variable names. Nothing environment-specific
is committed - the examples below are placeholders; the real values live
only in the pipeline's secrets/variables.

**Per run (entered when starting a deployment):**

| Variable | Example | Notes |
|---|---|---|
| `PVE_HOSTNAME` | `pve-node15` | becomes the Proxmox FQDN `<PVE_HOSTNAME>.<PVE_DOMAIN>` |
| `PVE_STATIC_IP` | `192.0.2.35/24` | CIDR notation |
| `ILO_URL` | `https://ilo.example.com:4415` | full base URL incl. `https://` + port |

**Blocked iLO ports**: list every iLO port that must never be reinstalled
(e.g. a shared storage box behind the same iLO host) in
`ILO_BLOCKED_PORTS`. `provision.py` refuses to run against those ports
regardless of how they end up in `ILO_URL`.

**Fleet-wide config (not secret) - required:**

| Variable | Example |
|---|---|
| `ILO_USER` | `ilo-admin` |
| `PVE_GATEWAY` | `192.0.2.1` |
| `PVE_DNS` | `198.51.100.10` |
| `AZURE_STORAGE_ACCOUNT` | `<storage-account>` |
| `PVE_DOMAIN` | `example.internal` (DNS domain of the Proxmox hosts) |
| `PVE_MAILTO` | `admin@example.internal` (Proxmox admin e-mail) |
| `ILO_BLOCKED_PORTS` | `4411` or `4411,4416` - optional, but strongly recommended if any non-Proxmox box shares the iLO host |

**Fleet-wide secrets - required:**

| Variable | Purpose |
|---|---|
| `ILO_PASSWORD` | password for the `ILO_USER` account (same across the whole fleet) |
| `PVE_ROOT_PASSWORD` | Proxmox root password (same across the whole fleet, 8-256 bytes). Only its SHA-512-crypt hash (`root-password-hashed`, salt derived from the hostname so the ISO cache keeps working) ends up in `answer.toml` / the custom ISO - never the plaintext |
| `AZURE_SAS_TOKEN` | SAS token for the `AZURE_STORAGE_ACCOUNT`'s `iso` container, create+write only - **not** the account key, so a leak here can't touch anything outside that one container |

**Defaults baked into `provision.py` - no variable needed, only set one to
override:**
`PROXMOX_ISO_BASE_URL` (`https://enterprise.proxmox.com/iso/` - where
`SHA256SUMS` and the ISOs are fetched from), `PROXMOX_MAJOR` (`9`),
`PROXMOX_ISO_NAME` + `PROXMOX_ISO_SHA256` (unset = latest release; the
old `PROXMOX_BASE_ISO_URL` is rejected with an error; set both
together to pin one, e.g. `proxmox-ve_9.3-1.iso` +
`cc4fc97b9ac3a5712ad5b665195753571923a18e11f5c2ebb113fdc29a50bd36`),
`PROXMOX_KEYRING` (`/etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg`),
`AZURE_CONTAINER` (`iso`), `PVE_TIMEZONE` (`Europe/Zurich`), `PVE_KEYBOARD` (`de-ch`),
`PVE_COUNTRY` (`ch`), `PVE_NIC_INDEX` (`1`), `PVE_DISK_MODEL`
(`Samsung SSD 870 QVO 1TB`).

**Running several servers at once** needs no code change: start one run per
server, as many as you like. Each run only ever touches its own target iLO
and its own content-addressed blob name, so there's no shared state between
concurrent runs. Don't start two runs against the *same* server.

## Usage with GitHub Actions

- `.github/workflows/ci.yml` - on pushes to `main` and on pull requests:
  syntax check of `provision.py` and a test build of the CI image.
- `.github/workflows/deploy-pve.yml` - manual only: **Actions → Deploy
  Proxmox VE → Run workflow**, with required fields `pve_hostname`,
  `pve_static_ip`, `ilo_url` (no defaults, so nothing can be reinstalled by
  accident) and optional `pve_nic_index`. The image is built inside the job
  (cached via the GitHub Actions cache), so no container registry is
  involved. Only one run per `ilo_url` at a time.

One-time setup - **Settings → Environments → New environment
`pve-fleet`**:
- **Deployment branches and tags → Selected branches → `main`** (otherwise
  a run dispatched from any other branch - with whatever
  workflow/`provision.py` that branch contains - gets the fleet secrets).
- Optionally **Required reviewers**, for an approval before each reinstall.
- **Environment secrets**: `ILO_PASSWORD`, `PVE_ROOT_PASSWORD`,
  `AZURE_SAS_TOKEN`.
- **Environment variables**: `ILO_USER`, `PVE_GATEWAY`, `PVE_DNS`,
  `AZURE_STORAGE_ACCOUNT`, `PVE_DOMAIN`, `PVE_MAILTO`, `ILO_BLOCKED_PORTS`,
  plus any optional override from the defaults
  list above (e.g. `PROXMOX_ISO_NAME`/`PROXMOX_ISO_SHA256`,
  `PVE_DISK_MODEL`, `PVE_NIC_INDEX`).

Actions are pinned to full commit SHAs (version in a comment), the
workflows only get `contents: read`, and inputs/secrets reach the script
solely as environment variables. Public repositories have public Actions
logs, so `provision.py` never logs the custom ISO's (anonymous) download
URL. GitHub-hosted runners are cloud IPs, so the "Known limitations" below
apply (no web UI check, Proxmox download may be blocked).

## Usage with GitLab CI (alternative)

`.gitlab-ci.yml` provides the same flow for a GitLab-hosted copy of this
repository, with the stages `validate` → `build` (CI image, pushed to the
project's container registry whenever the `Dockerfile` changes) → `deploy`.

One-time setup - **Settings → CI/CD → Variables**: the fleet-wide config
from above as plain variables, the secrets as **masked + protected**
variables. Don't store the per-run values (`PVE_HOSTNAME`,
`PVE_STATIC_IP`, `ILO_URL`) there - otherwise the manual `deploy:pve` job
of any push pipeline is one click away from reinstalling that server.

**CI/CD → Pipelines → Run pipeline** shows the three per-run values as
labeled input fields (declared with `description` in `.gitlab-ci.yml`).
Each field pre-fills with an obvious placeholder (e.g.
`CHANGE-ME-pve-hostname`, `0.0.0.0/32`) rather than a real value -
deliberately, so forgetting to edit one fails cleanly instead of
re-provisioning whatever server the last real example happened to be. Then
run the `deploy:pve` job (manual). If your runner concurrency limit
(Settings → CI/CD → Runners) is lower than the number of servers started at
once, the extra runs simply queue.

## Known limitations

- `PVE_NIC_INDEX` defaults to `1` (the first `EthernetInterfaces` entry), which
  matched the cabled port on the servers tested. If a server's cabled port differs, pass
  `PVE_NIC_INDEX` as an extra pipeline variable for that run.
- The answer file's `disk-setup` filters on `ID_MODEL` (`PVE_DISK_MODEL`),
  which matches *every* disk of that model in the server - this only works
  because every fleet server currently has exactly one disk. If a server
  ever has more than one disk of the same model, `answer.toml.template`
  needs a different/more specific filter key (e.g. `ID_SERIAL_SHORT`) for
  that case, which would bring back live per-server disk discovery.
- `answer.toml`'s `dns` field only accepts one resolver; only the primary
  DNS server is set.
- New Proxmox point releases are picked up automatically (see "Which
  Proxmox release" above). The first run after a new release has to fetch
  it from `enterprise.proxmox.com`, which a GitLab-hosted runner likely
  can't reach - in that case seed the mirror blob once from a non-blocked
  network under Proxmox's own file name, after checking its sha256 against
  `SHA256SUMS` (e.g. `az storage blob upload --account-name <account>
  --container-name iso --name proxmox-ve_9.3-1.iso --file <freshly-downloaded
  iso> --account-key <key>`). If even `SHA256SUMS` is blocked, runs fall back
  to `FALLBACK_ISO_NAME`/`FALLBACK_ISO_SHA256` in `provision.py` - bump
  those two together when you want that fallback to move to a newer release.
  A new **major** version (PVE 10) is deliberately not picked up: it needs
  `PROXMOX_MAJOR` and the Dockerfile's Debian/PVE repo updated together.
- Old releases' mirror blobs (`proxmox-ve_*.iso`) aren't deleted
  automatically either - remove them manually once no longer needed.
- The job does **not** wait for the install to finish. GitLab's shared
  runners are blocked from reaching port 8006 on the fleet network
  (confirmed on a real server: reachable from the operator's network, never from the
  runner), so polling the web UI could only ever time out. Instead the job
  ends green ~5-6 min after the power-cycle, once the boot override is
  cleared (see below); the install itself keeps running on the host for
  roughly another 15-25 min. Check the result at `https://<ip>:8006/` or
  via the iLO Remote Console. The job only fails here if the override
  can't be cleared within ~15 min - then clear it manually in iLO right
  away. The job also fails (and clears the override again) if
  `InsertMedia` or the power-cycle is rejected by iLO. A green job means
  the install was handed off to iLO correctly - not proof that the host
  actually booted the installer (see the boot-handling notes below), so
  glance at the iLO Remote Console after the job. The custom ISO stays mounted as
  virtual media afterwards, since the running installer still reads from
  it. **Eject it in iLO once the web UI is up**: a later boot that falls through to the
  virtual CD (e.g. disk boot failing) would start the unattended install
  again. The next run ejects it first anyway.
- Boot handling: a server from a previous deployment may already have a
  Proxmox UEFI boot entry (e.g. "proxmox") sitting first in the persistent
  `BootOrder` from its old install. Rather than relying on a one-time
  override timed just before the final reboot, this script sets
  `BootSourceOverrideEnabled=Continuous` + `BootSourceOverrideTarget=Cd`
  right at the start, before anything else - so it's already in place no
  matter how or when the host actually ends up getting power-cycled
  (including a manual reboot during troubleshooting, which is exactly what
  happened during testing). Continuous takes precedence over the entire
  `BootOrder` list on *every* boot from that point on, not just the next
  one. It's read back and verified twice (right after being set, and again
  right before the final
  reboot) - a mismatch aborts the job instead of rebooting into an unknown
  state.

  **Confirmed in practice, not just in theory: the Continuous override is
  not a 100% guarantee.** On a real run against one server, the override stayed
  reported as `Continuous`/`Cd` throughout, yet the firmware still silently
  booted the OLD on-disk install instead - almost certainly because the
  network-backed virtual media (HTTPS from Azure Blob) wasn't ready to
  respond within the firmware's own internal patience for the override
  attempt, so it fell through to the real persistent `BootOrder`. Because
  that disk already has a leftover OS from a previous deployment, falling
  through there is a *silent wrong-OS boot*, not a visible failure.

  Mitigation (second layer of defense, right after setting the override):
  every boot entry whose `UefiDevicePath` starts with `HD(` - i.e. it
  targets a specific disk partition by GPT GUID, which is exactly how a
  specific OS's own UEFI entry (e.g. "proxmox") looks, as opposed to a
  generic PCI/SATA-controller-level entry - gets moved to the very end of
  `BootOrder`. This is computed generically from the live `BootOptions`
  collection, not hardcoded `Boot####` IDs or entry names, since those
  differ per server and per how many previous deployments left something
  behind. If the override ever fails again, the firmware now falls through
  to USB/PXE first - which fail cleanly with nothing attached - instead of
  silently booting stale data. A fresh install's own new UEFI entry isn't
  affected by this reordering (it doesn't exist yet when this runs) and
  gets inserted at the front of `BootOrder` by the firmware automatically,
  same as the empirical finding below that this whole mechanism relies on
  for normal operation afterwards. Individual firmware boot entries can't
  be cleanly deleted via Redfish, which is why this reorders rather than
  removes the stale one.

  **Also confirmed in practice: clearing the override only on success is
  too late, even with the reordering above in place.** The *installer's
  own* internal reboot - from its live environment into the
  freshly-installed OS, which happens partway through the unattended
  install, long before the web UI ever comes up - is still subject to
  "Continuous", since Continuous means every boot, not just the first. On
  a real run this reboot tried the virtual CD again too, nearly re-running
  the whole install over the disk it had just finished writing. Fix: the
  job clears the override **early** (after ~5 min,
  comfortably past even a slow virtual-media boot into the installer, but
  long before the install can finish) instead of waiting for success.
  iLO refuses that PATCH with `UnableToModifyDuringSystemPOST` whenever the
  host happens to be mid-boot at that exact moment (also confirmed in
  practice) - transient, so it's retried every 10s for up to ~10 more
  minutes. The virtual media itself is left mounted - the running
  installer still reads from it, but doesn't care about the firmware's
  boot-order override once it's already running.
- `AZURE_SAS_TOKEN` expires 2027-11-05. Regenerate it before then with:
  `az storage container generate-sas --account-name <account> --name iso --permissions cw --expiry <date> --account-key <key> -o tsv`
  and update the CI/CD variable - the account key is only ever needed for
  that one command, never stored anywhere in this repo or pipeline.
- Each distinct config variant for a given `PVE_HOSTNAME` leaves its own
  content-addressed custom ISO on Azure Blob (old ones aren't deleted
  automatically). Harmless, but worth an occasional manual cleanup of the
  `iso` container if a server's config changes a lot during testing.
