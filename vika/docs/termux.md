# Native Termux (Android/Bionic)

This source installation uses Termux's Node and a locally built lark-cli. It
does not download glibc Node binaries, impersonate Linux, or require PRoot.
Pi and Durable are installed locally at the pinned **1.5.0** versions; global
installations are left alone. Both runtime checks must pass before activation.

## Install

Use Termux private storage, not `/sdcard` (execution, symlinks and file modes
are needed). Install Node >=22.19.0 with `node:sqlite`, npm, bash, git and tar
from Termux. Build/install a native lark-cli separately; its executable must
support the same CLI/API used by Vika. This branch pins the desktop CLI to
1.0.94; a different native build needs connector testing.

From this checkout's `vika/` directory:

```sh
export LARK_CLI_BIN="$HOME/.local/bin/lark-cli"
node scripts/install-termux.mjs
export PATH="$HOME/.vika/bin:$PATH"
vika setup
vika start
vika status
```

The installer creates a fresh directory under `~/.vika/releases`, runs npm
with download hooks disabled, checks Pi and Durable, and then creates
`~/.vika/current` and `~/.vika/bin/vika`. The launcher remembers the native
lark-cli path (an explicit `LARK_CLI_BIN` still overrides it). It does not start
services or change shell profiles. Failed checks leave no active installation.
The npm lark wrapper is optional so its Android platform restriction does not
block installing the rest of Vika. The native executable is required by this
installer; no Slack CLI is downloaded (set `SLACK_CLI_BIN` separately if used).

## runit

Install `termux-services` and restart the Termux shell to start its supervisor.
After setup, stop a manually started Vika before enabling the service:

```sh
vika stop
vika autostart enable
sv status "$PREFIX/var/service/vika"
# Later:
vika autostart disable
```

The service runs PM2 in the foreground with the same private `~/.vika/pm2`
home as the Vika CLI; runit supervises PM2 and PM2 supervises the configured
instances and Web. `SVDIR` can select another service root. `disable` writes
the persistent `down` marker and asks `sv` to stop the service; it does not
delete runit's supervision directory. Re-enable removes that marker and
signals an already running supervisor.

On service startup, any leftover PM2 daemon in Vika's private PM2 home is
stopped before starting foreground PM2. Other PM2 homes are not touched.

The service captures PATH and Termux loader variables, not model API keys.
Keep runtime credentials in their normal local credential files or the Vika
configuration. `vika stop` controls apps; use `vika autostart disable` to stop
the supervisor persistently. Device reboot additionally requires Termux:Boot
configured to start termux-services. Android battery/background-process limits
still apply; installing a runit service alone does not bypass them.

## Manual updates

Release upgrades are disabled: Android has no compatible published artifact.
The installer refuses to replace an existing installation. To update, disable
the runit service if enabled, stop Vika, and move `~/.vika/current` and
`~/.vika/bin/vika` to backup names. Run the new checkout's Termux installer,
then start Vika (or re-enable runit). Keep the old release until the new one is
verified; restoring both saved entries rolls back the application code.
Configuration/data stay in place; code rollback does not undo data migrations.

## Validation scope

Automated tests cover Android capability policy, loader environment forwarding,
installer failure cleanup and refusal to overwrite, and runit enable/disable.
Pi and Durable 1.5.0 handshake and faux-provider integration tests run on Linux.
Native Android end-to-end validation is still needed: install, setup, a Pi and
Durable turn/tool call, restart/session recovery, then runit stop/start and a
real connector message round trip. Do not infer those from a successful npm
installation or a `--version` response.
