# Updating & Uninstalling ExpressLane

This guide covers upgrading an existing ExpressLane install in place and removing it cleanly. Both the VM Manual Install and the Podman Install paths are covered.

## Checking Your Current Version

- **Web UI:** the version is shown in the footer of every page and on the About page.
- **Command line on the host:**
  ```bash
  python3 -c "from version import __version__; print(__version__)"
  ```

## Before You Upgrade — Back Up State

The upgrade flow preserves `config.json` and the `instance/` SQLite database in both install paths, but it's cheap insurance to snapshot them first. Do this before running any upgrade command.

**VM Manual Install:**

```bash
sudo tar czf /home/opc/expresslane-backup-$(date +%F).tgz \
    /opt/expresslane/config.json \
    /opt/expresslane/instance/
```

**Podman Install:**

```bash
tar czf ~/expresslane-backup-$(date +%F).tgz \
    ~/expresslane/config.json \
    ~/expresslane/instance/
```

## Upgrading — Option 1: VM Manual Install

1. SSH to the instance and download the new release zip:
   ```bash
   ssh opc@<PUBLIC_IP>
   cd ~
   curl -LO https://github.com/oracle-quickstart/expresslane/releases/latest/download/expresslane.zip
   ```

2. Unzip over the existing source directory. The installer rsyncs into `/opt/expresslane`, so the source tree in `~/expresslane` is just the staging area:
   ```bash
   unzip -o expresslane.zip
   ```

3. Re-run the installer. It detects the existing install and prints a version-upgrade line (for example `v1.1.0 -> v1.2.0`):
   ```bash
   cd ~/expresslane/deploy
   sudo bash deploy.sh
   ```
   `deploy.sh` excludes `config.json`, `instance/`, and `cache/` during the rsync, so your settings and migration database are preserved in place.

4. **If you were running HTTPS**, re-run with the same `--fqdn`, `--tls-cert`, and `--tls-key` arguments you used the first time — the installer is idempotent:
   ```bash
   sudo bash deploy.sh \
       --fqdn       expresslane.example.com \
       --tls-cert   /home/opc/fullchain.pem \
       --tls-key    /home/opc/privkey.pem
   ```

5. Confirm the service restarted cleanly:
   ```bash
   sudo systemctl status expresslane
   ```

## Upgrading — Option 2: Podman Install

1. Stop the running stack. Container state is in bind mounts on the host, so `down` is safe — nothing is deleted:
   ```bash
   cd ~/expresslane
   sudo podman-compose down
   ```

2. Download the new release zip on the instance:
   ```bash
   cd ~
   curl -LO https://github.com/oracle-quickstart/expresslane/releases/latest/download/expresslane.zip
   ```

3. Unzip **over** the existing source directory. The `-o` flag overwrites the application files, but the runtime files you created (`config.json`, `.env`, `instance/`, `cache/`, `certs/`) are not in the zip and will be left alone:
   ```bash
   unzip -o expresslane.zip
   ```

4. Rebuild the app image and bring the stack back up:
   ```bash
   cd ~/expresslane
   sudo podman-compose build
   sudo podman-compose up -d
   sudo podman ps
   ```
   `podman ps` should show both `expresslane-app` (healthy) and `expresslane-nginx` bound to ports 80/443.

## About the Startup Schema Migration

On every startup, ExpressLane runs an idempotent schema migration against its SQLite database (`app.py:_migrate_schema()`). New releases occasionally add columns for features like Test Migration state tracking and Warm Sync status. When that happens, you'll see log lines on the first boot after an upgrade:

```
Schema migration: adding ocm_migration.test_status (VARCHAR(30))
Schema migration: adding ocm_migration.sync_status (VARCHAR(20))
```

This is expected and safe. Each migration is guarded by a column-exists check, so subsequent starts are no-ops. **Your existing migration history and settings are preserved** — nothing is dropped or rewritten.

## Rolling Back

If an upgrade misbehaves, the cleanest rollback is to reinstall the previous release zip the same way you installed this one. If you also took the tarball backup at the top of this doc, restore it after reinstalling the old version:

**VM Manual Install rollback:**
```bash
sudo tar xzf /home/opc/expresslane-backup-YYYY-MM-DD.tgz -C /
sudo systemctl restart expresslane
```

**Podman Install rollback:**
```bash
cd ~/expresslane
sudo podman-compose down
tar xzf ~/expresslane-backup-YYYY-MM-DD.tgz -C ~/expresslane/
sudo podman-compose up -d
```

Keep in mind that **schema migrations are not reversible**. In practice this is rarely a problem — older code is tolerant of columns it doesn't know about — so rolling back the code to a prior release against a database that a newer release already migrated is safe. Rolling back the database itself is not supported without the pre-upgrade backup.

## Uninstalling

### VM Manual Install

Run the uninstaller script that ships with the release:

```bash
cd ~/expresslane/deploy
sudo bash uninstall.sh
```

You'll be prompted to confirm. To skip the prompt in automation:

```bash
sudo bash uninstall.sh --yes
```

**Removed:** systemd service (`expresslane.service`), nginx config (`/etc/nginx/conf.d/expresslane.conf`), TLS certificates (`/etc/pki/tls/expresslane/`), application directory (`/opt/expresslane/`).

**Kept:** the `opc` user, `~/.oci/` config, system packages (Python, nginx), firewall rules. Run these cleanups manually if you want a fully clean host:

```bash
sudo dnf remove -y nginx          # only if nothing else uses nginx
sudo firewall-cmd --permanent --remove-service=http
sudo firewall-cmd --permanent --remove-service=https
sudo firewall-cmd --reload
```

### Podman Install

```bash
cd ~/expresslane
sudo podman-compose down
sudo podman rmi -f \
    localhost/expresslane_app \
    docker.io/library/nginx:1.25-alpine \
    docker.io/library/python:3.9-slim
cd ~
rm -rf expresslane expresslane.zip
```

To also remove Podman itself from the host:

```bash
sudo pip3 uninstall -y podman-compose
sudo rm -f /usr/bin/podman-compose
sudo dnf -y remove podman podman-docker
```

---

*ExpressLane — Updating & Uninstalling*
*Copyright (c) 2026 Oracle and/or its affiliates. Released under UPL-1.0.*
