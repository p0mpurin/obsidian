# Luna implementation plan: Minecraft Dashboard rebuild

## Mission

Turn the current prototype into a safe, understandable Minecraft Java server manager that can be operated from the website after the one-time host installation. A normal user must be able to choose a Minecraft version and server distribution, accept the EULA, create a server, watch installation progress, start and stop it, diagnose failures, install compatible add-ons, back it up, update it, and delete or restore it without opening an SSH session.

Keep the existing FastAPI + systemd foundation and the production layout:

- Dashboard application: `/opt/minecraftdash`
- Managed server root: `${MC_ROOT_DIR}/instances/<server-id>`
- Production `MC_ROOT_DIR`: `/home/purin/minecraft`
- Recoverable trash: `${MC_ROOT_DIR}/.trash`
- Existing unmanaged server: the files directly inside `/home/purin/minecraft`

Never move, overwrite, adopt, or delete the existing top-level server automatically. Treat it as legacy/unmanaged until the user explicitly completes an import workflow.

## What is wrong with the current app

This is an audit of the current files, not a wishlist.

1. `static/index.html` is a single minified HTML/CSS/JavaScript file. The create dialog has a free-text version input and only a three-item Vanilla/Paper/Fabric selector. It has no compatibility guidance, loader/build choice, installation progress, or useful recovery screen.
2. `app.py` hard-codes `ALLOWED_SOFTWARE = {"vanilla", "paper", "fabric"}`. Forge and NeoForge are not represented at all.
3. Every distribution is reduced to a URL that becomes `server.jar`. This model is invalid for Forge and NeoForge: their official installation flow runs an installer and starts through generated Java argument files/scripts.
4. Fabric silently picks the first loader and installer returned by Fabric Meta. The user cannot see or pin those versions.
5. Paper uses the old downloads API. Replace it with PaperMC's current Fill v3 API, send a specific contact-bearing User-Agent, default to stable builds, and verify the published checksum.
6. `POST /api/servers` performs resolution, download, filesystem setup, systemd enablement, and database insertion in one blocking request. There is no durable job, progress, cancellation, or safe resume. A browser timeout leaves the user unsure whether anything happened.
7. Artifact downloads are not uniformly checksum-verified. Provider metadata is not cached, exact resolved build data is not retained, and installation is not reproducible.
8. The systemd wrapper always requires `/server.jar`. It cannot launch installer-generated Forge/NeoForge layouts or choose a compatible Java runtime per server.
9. Lifecycle status is inferred almost entirely from systemd's `active` state. The UI disables Stop when the unit is crash-looping, failed, or activating, which caused the current greyed-out Stop problem.
10. A generic Mods upload panel is shown regardless of distribution. Vanilla cannot load mods, while Paper primarily uses plugins. Uploaded JARs are not inspected for loader/game compatibility.
11. The EULA checkbox writes `eula=true`, but the UI does not link clearly to the agreement and the database does not record when and which URL was accepted.
12. There is no authentication. A control panel bound to `0.0.0.0` can create/delete files, control systemd units, and issue server-console commands. LAN exposure still requires login and CSRF protection.
13. There are no managed backups/restores, updates, import flow, player/allowlist administration, audit trail, runtime manager, disk checks, port auto-allocation, or actionable diagnostics.
14. The code is one backend module with raw dictionaries and hand-written validation. Provider logic, persistence, provisioning, systemd control, and API routes need explicit boundaries before more distributions are added.

## Product rules Luna must preserve

- The site manages Java Edition servers only in this release. Do not imply Bedrock support.
- Call Vanilla, Paper, Fabric, Forge, and NeoForge **server types** or **distributions**. Fabric/Forge/NeoForge also have a loader version; Paper has a build. Keep those concepts separate from the Minecraft version.
- Defaults should be safe: release Minecraft versions, stable/recommended distribution builds, automatic free port, EULA unchecked, server left stopped after installation, and no snapshots/betas unless the user opens Advanced options.
- A server is not `ready` until every artifact is downloaded, verified, installed, permission-checked, and its launch specification passes preflight.
- Never expose arbitrary shell execution. Provider installers and launch commands must be assembled from validated argument arrays, not interpolated shell strings.
- Destructive operations remain recoverable. Delete moves an instance to trash; restore is available in the site. Permanent purge is a separate, strongly confirmed action.
- Existing instance data and unrelated files in `/home/purin/minecraft` must survive upgrades and failed provisioning.

## Supported distribution matrix

| Distribution | User chooses | Provisioning model | Add-on UI | Launch model |
| --- | --- | --- | --- | --- |
| Vanilla | Minecraft release | Mojang version manifest -> server artifact | Datapacks only; hide Mods/Plugins | Direct executable JAR |
| Paper | Minecraft release, optional Paper build | Paper Fill v3 stable build and published checksum | Plugins and datapacks | Direct executable JAR |
| Fabric | Minecraft release, optional loader version | Fabric Meta server launcher or official CLI installer | Fabric mods and datapacks | Fabric server launcher JAR |
| Forge | Minecraft release, optional Forge build | Official Forge installer with `--installServer` | Forge mods and datapacks | Generated Unix argument file(s) |
| NeoForge | Minecraft release, optional NeoForge build | Official NeoForge installer with `--installServer` | NeoForge mods and datapacks | Generated `run.sh`/Unix argument file(s), represented safely as argv |
| Quilt (phase after the five required types) | Minecraft release, optional loader version | Official Quilt CLI installer | Quilt/Fabric-compatible mods with explicit compatibility results | `quilt-server-launch.jar` |

Do not display a server type if its provider cannot return a compatible release for the selected Minecraft version. Explain disabled choices, for example: “NeoForge does not publish a build for Minecraft 1.20.1; Forge is the supported choice for that version.”

## Target creation experience

Replace the current modal with a full-page or large stepper named **Create a server**. Preserve values when moving backward. Each async selector needs loading, empty, stale-cache, and retry states.

### Step 1 — Name

- Display name, with a live-generated filesystem-safe ID underneath.
- The ID is editable in Advanced options and must be unique.
- Choice: create a new world, import an uploaded world archive, or adopt an existing server. Only “new world” is required in the first usable milestone.

### Step 2 — Minecraft version

- Real searchable dropdown populated from Mojang's version manifest.
- Show stable releases by default, newest first, with release date.
- Put snapshots behind an explicit “Show snapshots and previews” switch and a warning.
- Never accept an arbitrary version string in the normal flow.
- Cache the catalog and show “Catalog last updated …” if stale data is used.

### Step 3 — Server type

Use selectable cards with plain-language explanations:

- Vanilla — official server; no plugins or loader mods.
- Paper — performance improvements and Bukkit/Paper plugins.
- Fabric — lightweight mod loader; usually quick to update.
- Forge — established mod-loader ecosystem, especially relevant for older packs.
- NeoForge — modern Forge-family loader for supported newer releases.

Cards must show Compatible, Unavailable, or Experimental for the chosen Minecraft version. The recommendation should be contextual, not a permanent badge: Paper for a plugin server, Fabric for lightweight mods, and the loader required by a supplied modpack.

### Step 4 — Build and Java

- Resolve loader/build choices only after Minecraft version + distribution are selected.
- Default to the latest stable/recommended compatible build.
- Advanced users can pin an exact loader/build. Display channel, publication date, and build identifier.
- Show the required Java major and whether a compatible runtime is ready.
- If it is absent, offer “Download managed Java runtime” in the flow. Download an Eclipse Temurin JRE into `${MC_ROOT_DIR}/runtimes/java-<major>/`, verify SHA-256, and record the exact runtime. Do not mutate Debian's system Java from a web request.

### Step 5 — World and gameplay

Offer friendly controls with short help text:

- World name, optional seed, world type, and generate structures.
- Survival/Creative/Adventure/Spectator.
- Peaceful/Easy/Normal/Hard.
- Max players, PvP, online mode, whitelist, flight, command blocks, spawn protection.
- View distance and simulation distance, with a performance warning for large values.
- MOTD.

Keep less common `server.properties` settings under Advanced. Validate all values server-side from typed schemas. Label settings that require a restart.

### Step 6 — Resources and network

- Memory slider/presets in MiB/GiB; enforce min <= max and compare against host available RAM.
- Automatic port allocation is the default. Manual port is Advanced and must be checked against the database and current listening sockets.
- Show local connection address after creation. Treat port forwarding/firewall work as an explicit separate networking feature, never silently reconfigure it.
- Show estimated disk requirement and block when safe free space is unavailable.

### Step 7 — Review and EULA

- Summarize exact Minecraft version, distribution/build, Java, memory, port, and path.
- Link to the current official Minecraft EULA in a new tab.
- Require an unchecked statement such as “I have read and accept the Minecraft EULA.”
- Persist `eula_accepted_at`, the EULA URL, and the authenticated actor. Writing `eula=true` remains part of the install job.
- The final button says **Create server** and immediately transitions to the progress screen.

### Provisioning progress screen

Return a job within one second; do not hold the POST request open while downloading. Show these named phases:

1. Queued
2. Resolving compatible versions
3. Preparing a staging directory
4. Downloading artifacts (bytes and percent when available)
5. Verifying checksums
6. Running the distribution installer, if required
7. Writing configuration and EULA
8. Checking Java, permissions, launch target, disk, and port
9. Registering the systemd unit
10. Ready to start

On failure, retain a sanitized diagnostic, the failed phase, and buttons for Retry, View details, and Remove partial setup. Do not expose secrets or an unrestricted traceback. A browser refresh must return to the same job.

## Proposed backend structure

Keep FastAPI, but split `app.py` into testable modules. Replace raw request dictionaries with Pydantic models and generated OpenAPI schemas.

```text
minecraftdash/
  main.py
  config.py
  db.py
  migrations/
  models/
    api.py
    domain.py
  api/
    auth.py
    catalog.py
    servers.py
    jobs.py
    addons.py
    backups.py
    system.py
  providers/
    base.py
    vanilla.py
    paper.py
    fabric.py
    forge.py
    neoforge.py
    quilt.py
  services/
    provisioning.py
    lifecycle.py
    downloads.py
    java_runtime.py
    systemd.py
    properties.py
    addons.py
    backups.py
    import_server.py
  security/
    paths.py
    archives.py
    sessions.py
  worker.py
frontend/
  src/
  tests/
tests/
```

Use a Vite + React + TypeScript frontend for the multi-step form, job progress, route-level error states, and accessible reusable controls. Build static assets during release and serve the compiled output from FastAPI; Node must not be required on the production host at runtime. Do not rewrite the backend or systemd layer in JavaScript.

If the frontend migration must be incremental, first split the existing page into CSS and ES modules, but the final UI must not remain one minified hand-maintained file.

## Provider contract

Define one provider interface and make provisioning depend only on it. A provider should return data, not manipulate API responses directly.

```python
class ServerProvider(Protocol):
    key: Distribution
    capabilities: ProviderCapabilities

    async def compatible_minecraft_versions(self) -> list[MinecraftVersion]: ...
    async def builds_for(self, minecraft_version: str) -> list[ProviderBuild]: ...
    async def resolve(self, request: InstallRequest) -> ResolvedInstall: ...
    async def install(self, resolved: ResolvedInstall, staging_dir: Path,
                      progress: ProgressReporter) -> LaunchSpec: ...
    async def inspect_existing(self, instance_dir: Path) -> InspectionResult: ...
```

`ResolvedInstall` must contain exact IDs/versions, source URLs, file sizes when known, checksum algorithm/value, Java major, release channel, and metadata retrieval time. `LaunchSpec` must be a JSON document containing a validated executable path, argument array, working directory, environment allowlist, and expected files. Never store a shell command string.

Provider-specific requirements:

- Vanilla: use Mojang's manifest and the per-version server download SHA-1. Reject versions with no server artifact.
- Paper: use Fill v3; send a meaningful User-Agent containing project name and contact; show stable builds by default; consume the server download object and checksum. Do not silently auto-update a running production server.
- Fabric: query Fabric Meta for game, loader, and installer versions; expose loader choice; select a stable loader by default; pin both loader and installer in the resolved record.
- Forge: resolve official installer versions from Forge's official download/Maven data; distinguish Recommended from Latest where available; run `java -jar <installer> --installServer` in staging; discover and validate generated Unix argument files.
- NeoForge: resolve from the official NeoForged Maven repository; run `java -jar <installer> --installServer` in staging; validate generated launch files. Follow the official compatibility guidance rather than guessing from version strings.
- Quilt: use the official installer CLI and `quilt-server-launch.jar`; ship only after the five required types pass end-to-end tests.

Catalog requests need bounded timeouts, retries with jitter for transient failures, concurrency limits, conditional requests where supported, and a 6–24 hour cache. If a source is down, serve recent cached metadata marked stale; never invent builds.

## Durable data model and migrations

Introduce numbered, transactional SQLite migrations. Do not recreate the current database. Back it up before the first migration.

### `servers`

- `id`, `display_name`
- `minecraft_version`
- `distribution`
- `distribution_version` (Paper build or loader version)
- `installer_version` when applicable
- `release_channel`
- `port`
- `min_memory_mib`, `max_memory_mib`
- `java_major`, `java_runtime_id`
- `install_path`
- `launch_spec_path`
- `install_state`, `desired_state`, `last_error_code`, `last_error_message`
- `eula_accepted_at`, `eula_url`, `eula_accepted_by`
- `created_at`, `updated_at`

### Additional tables

- `jobs`: id, kind, server_id, state, phase, progress, message, error fields, timestamps, retry parent, cancellation flag.
- `artifacts`: provider, exact version/build, URL, checksum, local cache path, size, retrieval time.
- `catalog_cache`: provider/key, ETag or Last-Modified, payload, fetched/expires times.
- `java_runtimes`: major, exact Temurin release, path, checksum, architecture, installed time.
- `addons`: project source/id, version id, filename, loader, game version, checksum, dependency metadata, enabled state.
- `backups`: id, server_id, path, size, checksum, kind, game/distribution version, state, created time.
- `trash`: original server metadata, trash path, deletion time, purge-after time.
- `audit_events`: actor, action, target, success, safe metadata, timestamp, source IP.
- `users` and `sessions` if application-native login is selected.

Migration mapping for existing rows: `software -> distribution`, `version -> minecraft_version`, RAM strings -> MiB, current instances -> `ready` only if launch preflight succeeds; otherwise mark `repair_required`. Do not assume the current JAR is valid merely because the database row exists.

## Provisioning transaction and filesystem safety

- Allocate a UUID job and a reserved server ID/port in one short database transaction.
- Build under `${MC_ROOT_DIR}/.staging/<job-id>/`, never directly in the final instance path.
- Download to `.part` files with maximum-size limits, HTTPS-only redirects, host allowlists per provider, and checksum verification before use.
- Run installers as the unprivileged Minecraft service account or an equally restricted installer account. Apply a timeout and capture bounded logs.
- Validate archive extraction against absolute paths, `..`, symlink escapes, device files, and decompression bombs.
- Apply deterministic ownership/modes: setgid instance directories, directories `2770`, ordinary files `0660` or stricter, executable launch helpers only where required. Confirm readability as the actual `minecraft` account.
- Write configuration atomically, fsync important metadata, then rename staging to `${MC_ROOT_DIR}/instances/<id>` only after all checks pass.
- Enable the systemd instance only after the atomic move. If that fails, disable it and roll back the final path or mark a recoverable failed state.
- Use a global provisioning semaphore plus a per-server lock. Creation, update, restore, delete, and start must not race.
- On dashboard restart, recover queued/running jobs deterministically: resume safe download phases or mark installer phases interrupted with a Retry action.

## Java runtime management

The dashboard must stop relying on a single `/usr/bin/java` for every Minecraft version.

- Create a compatibility resolver that considers Minecraft version plus provider requirements.
- Prefer an already installed compatible runtime.
- Offer managed Eclipse Temurin JRE downloads through Adoptium's official API when needed. Verify SHA-256 and optionally the GPG signature; unpack with archive traversal protections.
- Keep runtimes versioned and immutable under `${MC_ROOT_DIR}/runtimes` and reference an exact executable in each launch spec.
- Show runtime health and updates on a System page. Never remove a runtime still referenced by a server.
- A runtime download is a tracked job with the same progress/error behavior as server provisioning.

## Lifecycle and systemd redesign

Define dashboard states independently from raw systemd strings:

```text
provisioning -> ready/stopped -> starting -> running -> stopping -> stopped
                         |          |           |
                         +-------> failed <-----+
failed -> retry | repair | stop/reset-failed | delete
```

The server API must return `allowed_actions`, for example `can_start`, `can_stop`, `can_restart`, `can_repair`, `can_delete`, plus an explanation for each disabled action. The frontend must use these fields instead of guessing from `running`.

- Stop remains available for `active`, `activating`, `reloading`, `deactivating`, and systemd auto-restart/crash-loop conditions.
- Add a guarded **Force stop** only after a graceful stop times out. It maps to a fixed helper operation, never a free-form PID/command.
- Add systemd start-rate limits so bad installs cannot log the same error forever.
- Preserve `RestartPreventExitStatus=78` and make every permanent preflight/configuration failure exit 78.
- Replace the one-JAR shell wrapper with a small root-owned/deployed runner that loads `launch.json`, validates all paths remain inside the instance/runtime roots, opens the console FIFO, and calls `execve` with an argument array.
- Preflight checks: launch file exists, Java executable works, required Java major matches, all launch inputs are readable by `minecraft`, port is free, enough disk is available, EULA is accepted, and no conflicting job is active.
- Store recent failure reason and provide a one-click copyable diagnostic bundle containing versions, unit state, bounded logs, and redacted configuration.

## Main dashboard information architecture

### Server list/home

- Searchable cards or table with name, distribution badge, Minecraft version, running/failed/provisioning status, players, memory, port, and last backup.
- Filters for Running, Stopped, Needs attention, and Installing.
- Empty state explains what a server type is and leads directly to the wizard.
- Persistent, specific error banners; toasts are only for brief confirmations.

### Server detail routes

- **Overview:** status, connection address, players, resource charts, version/build, start/stop/restart, and current problem with a recommended fix.
- **Console:** reconnect-safe log stream, pause/autoscroll, filter, download recent log, command history, and clear connection state. Commands require authorization and audit entries.
- **Players:** online players, allowlist, ops, bans, and safe command-backed actions. Validate usernames/UUIDs; do not expose arbitrary file JSON editing.
- **Add-ons:** label as Plugins for Paper, Mods for Fabric/Forge/NeoForge/Quilt, and Datapacks for Vanilla. Hide impossible tabs.
- **World:** seed display only when appropriate, world folders, save/flush action, upload/import flow, and reset workflow with backup.
- **Settings:** grouped gameplay/network/performance settings with validation, dirty state, and explicit “Restart required.”
- **Backups:** create, download, restore, retention, progress, and integrity state.
- **Versions:** current resolved versions, available update, compatibility report, changelog/source link, backup-before-update workflow, and rollback.
- **Advanced:** JVM options with validated tokens, file browser jailed to the instance, diagnostics, and move to trash.

Make all controls keyboard accessible, label icons, maintain visible focus, meet contrast requirements, and provide usable layouts at phone and desktop widths. Destructive confirmation dialogs must explain impact and recovery, not rely on browser `prompt()`.

## Add-on manager

Manual upload alone is not a complete mod/plugin workflow.

1. Integrate Modrinth first. Search must include exact Minecraft version, exact loader, server-side environment, and project type facets. Store stable project and version IDs, not only slugs/filenames.
2. Resolve required dependencies before installation. Present optional/incompatible dependencies separately.
3. Download the selected version file and verify its SHA-1/SHA-512 from metadata.
4. Refuse known client-only or wrong-loader files. Explain the mismatch.
5. Detect available compatible updates but never replace JARs while the server is running. Create a backup, stage updates, and apply on restart.
6. Keep manual JAR upload under Advanced. Inspect ZIP metadata (`fabric.mod.json`, Forge/NeoForge TOML metadata, `plugin.yml`, etc.) and warn when it conflicts with the server type/version. Filename extension is not sufficient validation.
7. CurseForge is optional and disabled until an API key is configured; its official API requires a key. Do not scrape the website.
8. Never execute an uploaded JAR in the dashboard process. It runs only later inside the unprivileged Minecraft service.

## Backups, restores, upgrades, and imports

- Backup jobs must coordinate a consistent save (`save-all flush`, then `save-off` where appropriate) or require a stopped server. Always restore `save-on` in a `finally` path.
- Include world folders, configs, EULA, add-on manifests/files, launch metadata, and exact version records. Exclude transient logs/cache unless explicitly requested.
- Produce a manifest and archive checksum. Default to rolling retention and show disk usage before creating a backup.
- Restore requires the server stopped, verifies the archive, creates a pre-restore safety snapshot, extracts to staging, validates it, and atomically swaps directories.
- Every distribution/Minecraft update starts with a tested backup. Never update a running JAR or loader installation in place.
- Import wizard should inspect an existing directory read-only first: detect `server.properties`, worlds, EULA, Vanilla/Paper/Fabric/Forge/NeoForge markers, add-ons, Java needs, and current port. Present findings and conflicts before copying/adopting anything.
- For `/home/purin/minecraft`, offer an explicit **Import existing server** card. Default to copy into a managed instance. In-place adoption is Advanced and needs a backup plus exact confirmation.

## Authentication and network safety

Complete this before advertising LAN-wide access.

- Provide a first-run admin account with Argon2id password hashing, rate-limited login, secure session rotation, HttpOnly/SameSite cookies, logout, and CSRF protection for HTTP mutations and WebSocket authentication.
- Alternatively support a documented trusted reverse-proxy identity mode, but never trust forwarded identity headers from arbitrary clients.
- Default bind remains `127.0.0.1`. The System page may explain how the installed deployment is exposed, but changing host firewall/router settings is out of scope until implemented by a narrowly constrained helper.
- Recommend HTTPS through Caddy/Nginx or VPN for anything beyond a trusted LAN. Do not claim plain HTTP is safe because the address is private.
- Apply authorization to REST and WebSocket endpoints, redact secrets from logs, validate `Origin`, rate-limit sensitive actions, and audit create/start/stop/console/delete/restore/account actions.
- Keep the sudo helper fixed-action and ID-validated. Extend it only with narrowly defined operations such as `reset-failed` or `kill`; never pass arbitrary unit names or command fragments.

## API contract

Use versioned routes (`/api/v1`) and stable machine-readable error codes. Every error response should have `code`, `message`, optional safe `details`, and `request_id`.

Minimum endpoints:

```text
GET    /api/v1/system/health
GET    /api/v1/system/preflight
GET    /api/v1/catalog/minecraft-versions?channel=release
GET    /api/v1/catalog/distributions?minecraft_version=1.21.x
GET    /api/v1/catalog/distributions/{key}/builds?minecraft_version=...
POST   /api/v1/servers                         -> 202 + job
GET    /api/v1/servers
GET    /api/v1/servers/{id}
POST   /api/v1/servers/{id}/actions/{action}   -> 202 when asynchronous
DELETE /api/v1/servers/{id}                    -> trash job
GET    /api/v1/jobs/{id}
GET    /api/v1/jobs/{id}/events                -> SSE, polling fallback
POST   /api/v1/jobs/{id}/retry
POST   /api/v1/jobs/{id}/cancel
GET/PUT /api/v1/servers/{id}/settings
GET/POST/DELETE /api/v1/servers/{id}/addons/...
GET/POST /api/v1/servers/{id}/backups
POST   /api/v1/servers/{id}/backups/{backup_id}/restore
GET    /api/v1/trash
POST   /api/v1/trash/{id}/restore
DELETE /api/v1/trash/{id}                      -> permanent purge with strong confirmation
WS     /api/v1/servers/{id}/console
```

Example create request:

```json
{
  "display_name": "Friends Survival",
  "id": "friends-survival",
  "minecraft_version": "<catalog id>",
  "distribution": "fabric",
  "distribution_version": "latest-stable",
  "java_runtime": "managed",
  "port": null,
  "memory": {"min_mib": 2048, "max_mib": 4096},
  "world": {
    "seed": null,
    "gamemode": "survival",
    "difficulty": "normal",
    "max_players": 20,
    "online_mode": true,
    "whitelist": true
  },
  "motd": "Friends Survival",
  "eula_accepted": true
}
```

The create response is `202 Accepted` with `job_id`, `server_id`, and links to the job/server. Do not return a fake ready server while work continues.

## Delivery phases and acceptance gates

### Phase 0 — Stabilize and freeze the prototype behavior

- Add characterization tests for current create/list/start/stop/delete/properties/console behavior.
- Preserve the existing JAR permission fix and service-account access checks.
- Add systemd start-rate limiting and ensure permanent missing/unreadable launch-file errors exit 78.
- Record a migration backup and test against a copy of the current SQLite database.

Acceptance: an existing direct-JAR server can start and stop; a missing/unreadable JAR produces one actionable failed state instead of an endless restart log; Stop is available during activating/restarting states.

### Phase 1 — Foundation, typed API, jobs, and frontend shell

- Create package structure, migrations, typed models, error format, audit logging, and job worker.
- Add authenticated React shell, routing, server list, persistent alerts, and job progress component.
- Add health/preflight page and preserve old API temporarily behind compatibility routes.

Acceptance: refresh-safe jobs survive app restart; all mutations require authentication/CSRF; browser and API tests cover errors and loading states.

### Phase 2 — Catalog and excellent Vanilla/Paper creation

- Implement Mojang and Paper providers, cache, version dropdown, distribution cards, automatic port, Java resolver, staged provisioning, checksum verification, and full wizard.
- Migrate Paper from v2 to Fill v3.

Acceptance: from a clean host state, a user can create current stable Vanilla and Paper servers entirely in the site, see deterministic progress, start them, connect, stop them, and understand every failure without SSH.

### Phase 3 — Fabric

- Add compatible loader/installer catalog, loader pinning, managed Java selection, Fabric launch spec, and loader-aware Mods UI.

Acceptance: test at least one Java 17-era and one Java 21-era Fabric server; exact loader/installer versions survive restart and appear in the UI.

### Phase 4 — Forge and NeoForge

- Implement installer-backed providers in isolated staging directories.
- Introduce generated-argument launch support and installer log display.
- Add compatibility messaging and loader-specific add-on classification.

Acceptance: create/start/stop/restart/delete and crash recovery pass for at least two supported Minecraft versions per provider. No code path assumes `server.jar` exists for these providers.

### Phase 5 — Operations quality

- Finish state machine, allowed actions, force-stop flow, diagnostics bundle, player management, settings grouping, resource metrics, and reliable console reconnect.

Acceptance: injected missing-file, wrong-Java, occupied-port, offline-provider, checksum mismatch, installer failure, permission denial, and dashboard-restart scenarios each show a specific remedy and leave recoverable state.

### Phase 6 — Add-ons

- Modrinth search/install/dependencies/updates, advanced manual inspection, distribution-aware labels, and optional CurseForge configuration.

Acceptance: incompatible loader/game-version files are rejected or clearly warned; dependency installs are atomic; add-ons cannot change while the server is running unless staged for restart.

### Phase 7 — Backups, restore, update, trash, and import

- Build backup manifests/checksums/retention, atomic restore, backup-first server updates, trash restore/purge, world upload, and existing-server inspection/import.

Acceptance: complete create -> play -> backup -> update -> restore -> trash -> restore flow is tested end to end with data integrity checks; the legacy top-level server remains untouched unless explicitly imported.

### Phase 8 — Optional Quilt and polish

- Add Quilt through the same provider contract, accessibility audit, phone layout, first-run tour, operational docs, and deployment upgrade script.

Acceptance: no provider-specific conditionals leak into generic wizard/lifecycle components; WCAG-oriented keyboard/focus/contrast checks and complete browser smoke tests pass.

## Test strategy

- Unit tests: validators, compatibility rules, version sorting, property parsing, path jail, checksum handling, state transitions, allowed actions, archive safety, and every provider resolver using recorded official-response fixtures.
- Provider contract tests: the same suite runs against Vanilla/Paper/Fabric/Forge/NeoForge to prove resolve/install/inspect behavior is consistent.
- Integration tests: temporary root + SQLite + fake downloader + fake systemd helper. Test every failed phase and rollback.
- Installer smoke tests: run official installers in disposable directories, never against production instances. Cache artifacts in CI where licensing permits; otherwise use opt-in network tests.
- API tests: authentication, CSRF, schema errors, duplicate ID/port, jobs, cancellation, idempotent retry, and race conditions.
- Browser tests: full wizard with keyboard, compatibility changes after selecting a different Minecraft version, refresh during install, failure/retry, lifecycle controls, add-ons, backup/restore, and mobile viewport.
- Deployment tests: Debian systemd unit, service users/groups/ACLs, environment path, 127.0.0.1 and LAN binding, dashboard restart, host reboot, log rotation, and database migration/rollback.
- Security tests: path traversal and symlink attacks, malicious archives/JAR filenames, oversized uploads, unauthorized WebSocket, CSRF, hostile Origin, command newlines, provider redirect to an unapproved host, and log redaction.

Every phase must pass `pytest`, frontend typecheck/lint/unit tests, production frontend build, browser smoke tests, and a mock-mode end-to-end test before deployment.

## Deployment and migration sequence for this host

1. Back up `/opt/minecraftdash`, its database, unit/helper files, and `/home/purin/minecraft/instances`. Do not include or alter the unmanaged top-level server during app migration.
2. Deploy to a versioned release directory, build/install dependencies, and run database migration preflight against a copied database.
3. Install root-owned helper/runner and systemd unit updates with explicit permissions; validate sudoers before replacement.
4. Stop only the dashboard during cutover. Do not stop existing Minecraft servers unless a unit/wrapper migration specifically requires it and the user confirms.
5. Run migrations, start the dashboard on localhost, execute health/API/browser smoke tests, then restore the configured LAN/reverse-proxy exposure.
6. Keep the previous application release and database backup for rollback. Database migrations need a documented downgrade or restore procedure.
7. Show a one-time in-app migration report: imported rows, servers needing repair, legacy directory detected, available Java runtimes, and any action the user must take.

## Definition of done

The dashboard is complete for the requested milestone only when all of the following are true:

- Minecraft version is selected from live, cached official metadata rather than typed blindly.
- Vanilla, Paper, Fabric, Forge, and NeoForge can each be selected only for compatible versions, with loader/build choice where applicable.
- Creation is asynchronous, refresh-safe, checksum-verified, atomic, and transparent about progress/errors.
- The dashboard can obtain and select a compatible managed Java runtime without requiring SSH after bootstrap.
- Forge/NeoForge run through provider-specific launch specs; the runtime has no universal `server.jar` assumption.
- Start/Stop/Restart/Repair availability comes from a tested backend state machine; a failed or crash-looping service can be stopped from the site.
- Add-on, settings, backup, update, trash/restore, diagnostics, and existing-server import workflows are usable from the site.
- The site requires authentication before it is reachable on the LAN, and all privileged host actions stay narrowly constrained.
- Existing `/home/purin/minecraft` server data is untouched unless the user explicitly imports it.
- End-to-end tests pass on the Debian deployment model, not only in mock mode.

## Primary references Luna should re-check while implementing

Provider APIs and compatibility change. Use these primary sources, keep response fixtures, and verify behavior at implementation time:

- [Minecraft Java server download](https://www.minecraft.net/en-us/download/server)
- [Minecraft EULA](https://www.minecraft.net/en-us/eula)
- [Mojang version manifest v2](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json)
- [Paper getting started and Java requirements](https://docs.papermc.io/paper/getting-started/)
- [Paper Downloads Service / Fill v3](https://docs.papermc.io/misc/downloads-service/)
- [Paper update and backup guidance](https://docs.papermc.io/paper/updating/)
- [Fabric server installation](https://wiki.fabricmc.net/install)
- [Fabric Meta API](https://github.com/FabricMC/fabric-meta/blob/master/README.md)
- [Forge official downloads](https://files.minecraftforge.net/net/minecraftforge/forge/)
- [NeoForge server installation](https://docs.neoforged.net/user/docs/server/)
- [NeoForge user guide and Java compatibility](https://docs.neoforged.net/user/docs/)
- [NeoForged Maven repository](https://maven.neoforged.net/)
- [Quilt server installation](https://quiltmc.org/en/install/server/)
- [Modrinth API overview](https://docs.modrinth.com/api/)
- [Modrinth project search](https://docs.modrinth.com/api/operations/searchprojects/)
- [CurseForge REST API](https://docs.curseforge.com/rest-api/)
- [Adoptium API installation guide and checksum verification](https://adoptium.net/en-GB/installation/ci-scripts)

## Luna's immediate first work order

Do not begin by adding Forge to the current `_download_url()` conditional. First deliver a narrow vertical slice:

1. Add migrations, provider interfaces, catalog cache, and job persistence behind tests.
2. Implement the Mojang provider and version dropdown.
3. Build the new creation wizard through Review/EULA and a real progress page.
4. Provision Vanilla atomically with checksum verification and a launch spec.
5. Replace lifecycle guessing with backend `allowed_actions` and fix the failed/crash-loop Stop path.
6. Deploy that slice to a disposable test root and verify it in a browser.
7. Only then add Paper, Fabric, Forge, and NeoForge one provider at a time, requiring the shared provider contract test suite to pass before exposing each card in production.

