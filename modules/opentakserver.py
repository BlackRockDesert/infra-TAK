# SPDX-License-Identifier: AGPL-3.0-or-later
# infra-TAK — TAK Infrastructure Platform
# Copyright (C) 2026 Andreas Johansson (TAKWERX)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""OpenTAKServer — marketplace module (v10.1.62).

Deploys and manages OpenTAKServer (https://github.com/brian7704/OpenTAKServer),
an open source Python-based TAK Server as an alternative to the tak.gov TAK Server.

Architecture:
  - Docker Compose stack: OTS app, RabbitMQ
  - Caddy handles SSL termination and reverse proxy (ports 8443, 8446)
  - SQLite database (built-in, no PostgreSQL required)
  - config.yml for configuration
  - Built-in CA for certificate management
  - LDAP support via Flask-LDAP3-Login

This file imports NOTHING from app.py — every seam arrives through the ctx dict.
"""
import os
import json
import subprocess
import threading
import time as _time

from . import register_module, job_log, job_state

# ── Constants ────────────────────────────────────────────────────────────────

OTS_REPO = "https://github.com/brian7704/OpenTAKServer.git"
OTS_INSTALL_DIR = os.path.expanduser("~/opentakserver")
OTS_TAG = "v1.7.0"  # pinned release tag
OTS_SHA = "master"  # pinned commit SHA — update when bumping TAG

# Docker container names
OTS_CONTAINER = "opentakserver"
OTS_RABBIT_CONTAINER = "ots-rabbitmq"

# ── Docker Compose ───────────────────────────────────────────────────────────

OTS_DOCKER_COMPOSE = '''\
version: '3.8'
services:
  rabbitmq:
    image: rabbitmq:3-management
    container_name: {rabbit_container}
    hostname: ots-rabbitmq
    environment:
      RABBITMQ_DEFAULT_USER: ots
      RABBITMQ_DEFAULT_PASS: {rabbit_password}
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq
    ports:
      - "127.0.0.1:5672:5672"
      - "127.0.0.1:15672:15672"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "check_port_connectivity"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  opentakserver:
    build:
      context: {ots_dir}
      dockerfile: Dockerfile
    container_name: {ots_container}
    depends_on:
      rabbitmq:
        condition: service_healthy
    environment:
      - OTS_DATA_FOLDER=/app/data
      - OTS_LISTENER_PORT=8081
      - OTS_TCP_STREAMING_PORT=8088
      - OTS_SSL_STREAMING_PORT=8089
      - OTS_MARTI_HTTP_PORT=8080
      - OTS_MARTI_HTTPS_PORT=8443
      - OTS_CERTIFICATE_ENROLLMENT_PORT=8446
      - OTS_RABBITMQ_SERVER_ADDRESS=rabbitmq
      - OTS_RABBITMQ_TTL=86400000
      - OTS_COT_PARSER_PROCESSES=1
      - SECRET_KEY={secret_key}
      - SECURITY_PASSWORD_SALT={password_salt}
      - OTS_CA_NAME={ca_name}
      - OTS_CA_FOLDER=/app/data/ca
      - OTS_CA_PASSWORD={ca_password}
      - OTS_CA_EXPIRATION_TIME=3650
      - OTS_CA_COUNTRY={ca_country}
      - OTS_CA_STATE={ca_state}
      - OTS_CA_CITY={ca_city}
      - OTS_CA_ORGANIZATION={ca_org}
      - OTS_CA_ORGANIZATIONAL_UNIT={ca_ou}
      - OTS_SSL_VERIFICATION_MODE=ssl.CERT_REQUIRED
      - OTS_ENABLE_LDAP={ldap_enabled}
      - OTS_LDAP_ADMIN_GROUP={ldap_admin_group}
      - LDAP_HOST={ldap_host}
      - LDAP_BASE_DN={ldap_base_dn}
      - LDAP_USER_DN={ldap_user_dn}
      - LDAP_GROUP_DN={ldap_group_dn}
      - LDAP_BIND_USER_DN={ldap_bind_user_dn}
      - LDAP_BIND_USER_PASSWORD={ldap_bind_password}
      - PYTHONUNBUFFERED=1
    volumes:
      - {ots_dir}/data:/app/data
    ports:
      - "127.0.0.1:8081:8081"
      - "0.0.0.0:8088:8088"
      - "0.0.0.0:8089:8089"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/api/ots/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

volumes:
  rabbitmq_data:
'''

# ── Helpers ──────────────────────────────────────────────────────────────────

def _plog(msg):
    return job_log('ots', msg)


def ots_dir(ctx):
    """Resolve the OTS install dir. Always ~/opentakserver for now."""
    return OTS_INSTALL_DIR


def _compose(ctx, dirpath, action, timeout=120):
    """Run docker compose in the OTS dir via the broker."""
    return ctx['_broker_compose'](dirpath, action, timeout=timeout)


def _compose_argv(ctx, *action):
    """Argv form for the registry control_view."""
    return ['docker', 'compose', '--project-directory', ots_dir(ctx)] + list(action)


_ots_release_cache = {'version': None, 'ts': 0}


def get_latest_release_tag(use_cache=True):
    """Latest release tag from GitHub API. Cached 4h."""
    if use_cache and _ots_release_cache['version'] and (_time.time() - _ots_release_cache['ts'] < 14400):
        return _ots_release_cache['version']
    try:
        import urllib.request as _ur
        req = _ur.Request(
            'https://api.github.com/repos/brian7704/OpenTAKServer/releases/latest',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'infra-TAK'})
        with _ur.urlopen(req, timeout=10) as resp:
            tag = (json.loads(resp.read().decode()).get('tag_name') or '').strip() or None
            if tag:
                _ots_release_cache['version'] = tag
                _ots_release_cache['ts'] = _time.time()
            return tag
    except Exception as e:
        print(f"version-info: ots latest-release check failed (non-fatal): {e}", flush=True)
        return _ots_release_cache.get('version') or None


def get_version_info(ctx):
    """Return {version, update_available, latest} for OTS."""
    info = {'version': '', 'update_available': False, 'latest': None}
    s = ctx['load_settings']()
    info['version'] = s.get('ots_version', '')
    latest = get_latest_release_tag()
    if latest:
        info['latest'] = latest
        if info['version'] and info['version'] != latest:
            info['update_available'] = True
    return info


# ── Lifecycle ────────────────────────────────────────────────────────────────

def detect(ctx):
    """Settings flag + docker inspect liveness + LDAP credential sync."""
    s = ctx['load_settings']()
    enabled = bool(s.get('ots_enabled', False))
    running = False
    if enabled:
        try:
            r = ctx['probe_run'](
                ['docker', 'inspect', '--format', '{{.State.Running}}', OTS_CONTAINER],
                text=True, timeout=3)
            running = (r.stdout or '').strip() == 'true'
        except Exception:
            pass

        # LDAP credential sync: re-read Authentik .env if LDAP is enabled
        # This ensures the OTS container has current credentials after Authentik password changes
        if running and s.get('ots_ldap_enabled'):
            _sync_ldap_credentials(ctx, s)
    else:
        # Self-heal: container running but flag cleared
        try:
            r = ctx['probe_run'](
                ['docker', 'inspect', '--format', '{{.State.Running}}', OTS_CONTAINER],
                text=True, timeout=3)
            if (r.stdout or '').strip() == 'true':
                s = ctx['load_settings']()
                s['ots_enabled'] = True
                ctx['save_settings'](s)
                enabled = True
                running = True
        except Exception:
            pass
    return {'installed': bool(enabled), 'running': running}


def _sync_ldap_credentials(ctx, settings):
    """Re-read Authentik LDAP credentials and update docker-compose if changed.

    This is a lightweight operation that runs on every page load when LDAP is enabled.
    It ensures the OTS container has current credentials after Authentik password changes.
    """
    try:
        # Read current LDAP password from Authentik
        ak_token = (ctx['_get_authentik_env_value'](settings, 'AUTHENTIK_TOKEN') or
                    ctx['_get_authentik_env_value'](settings, 'AUTHENTIK_BOOTSTRAP_TOKEN'))
        if not ak_token:
            return

        ldap_pass = ctx['_get_authentik_env_value'](settings, 'AUTHENTIK_BOOTSTRAP_LDAPSERVICE_PASSWORD') or ''
        stored_pass = settings.get('ots_ldap_bind_password', '')

        # Only update if password changed
        if ldap_pass and ldap_pass != stored_pass:
            settings['ots_ldap_bind_password'] = ldap_pass
            ctx['save_settings'](settings)

            # Update docker-compose.yml with new password
            dirpath = ots_dir(ctx)
            compose_path = os.path.join(dirpath, 'docker-compose.yml')
            if os.path.exists(compose_path):
                try:
                    content = ctx['_read_priv'](compose_path)
                    import re as _re
                    # Replace LDAP_BIND_USER_PASSWORD value
                    content = _re.sub(
                        r'(LDAP_BIND_USER_PASSWORD=).*',
                        lambda m: m.group(1) + ldap_pass,
                        content)
                    ctx['_write_priv'](compose_path, content)
                    # Restart container to pick up new password
                    _compose(ctx, dirpath, 'restart', timeout=60)
                except Exception:
                    pass  # Non-fatal: container will use old password until next restart
    except Exception:
        pass  # Non-fatal: LDAP sync is best-effort


def deploy(ctx, job, params):
    """8-step deploy: Docker check, clone, RabbitMQ, config, build, firewall, certs, register."""
    import secrets as _sec
    plog = _plog
    ots_dir_ = OTS_INSTALL_DIR
    try:
        s = ctx['load_settings']()
        fqdn = (s.get('fqdn') or '').strip()

        # Guard: refuse if tak.gov TAK Server is installed
        modules = ctx['detect_modules']()
        if modules.get('takserver', {}).get('installed'):
            plog('✗ Cannot deploy: tak.gov TAK Server is already installed.')
            plog('  Uninstall TAK Server first, then deploy OpenTAKServer.')
            job.update({'running': False, 'error': True})
            return

        # Step 1: Docker check
        plog('━━━ Step 1/8: Checking Docker ━━━')
        _rc, _dv = ctx['_docker_probe']()
        if _rc != 0:
            plog('  Docker not found — installing...')
            if not ctx['_install_docker_engine'](plog):
                raise RuntimeError('Docker install failed')
            plog('✓ Docker installed')
        else:
            plog(f'✓ Docker present: {_dv}')

        # Step 2: Clone repository
        plog('')
        plog('━━━ Step 2/8: Cloning Repository ━━━')
        if os.path.isdir(os.path.join(ots_dir_, '.git')):
            plog(f'  Repo already cloned at {ots_dir_} — pulling latest...')
            subprocess.run(['git', '-C', ots_dir_, 'checkout', '--', '.'],
                           capture_output=True, text=True, timeout=30)
            r = subprocess.run(['git', '-C', ots_dir_, 'pull', '--ff-only'],
                               capture_output=True, text=True, timeout=60)
            plog(f'  git pull: {r.stdout.strip() or r.stderr.strip()}')
        else:
            os.makedirs(ots_dir_, exist_ok=True)
            plog(f'  Cloning {OTS_REPO} → {ots_dir_}')
            r = subprocess.run(['git', 'clone', '--depth=1', '--branch', OTS_TAG,
                                OTS_REPO, ots_dir_],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise RuntimeError(f'git clone failed: {r.stderr[:300]}')
            plog('✓ Repository cloned')

        # Record version
        version_r = subprocess.run(['git', '-C', ots_dir_, 'describe', '--tags', '--always'],
                                   capture_output=True, text=True)
        version = version_r.stdout.strip() or OTS_TAG
        plog(f'  Version: {version}')

        # Step 3: Write configuration
        plog('')
        plog('━━━ Step 3/8: Writing Configuration ━━━')
        os.makedirs(os.path.join(ots_dir_, 'data'), exist_ok=True)
        os.makedirs(os.path.join(ots_dir_, 'data', 'ca'), exist_ok=True)

        # Generate secrets
        secret_key = s.get('ots_secret_key') or _sec.token_hex(32)
        password_salt = s.get('ots_password_salt') or str(_sec.randbits(128))
        ca_password = s.get('ots_ca_password') or _sec.token_hex(16)
        rabbit_password = s.get('ots_rabbit_password') or _sec.token_hex(16)

        # CA fields from params or settings
        ca_country = params.get('ca_country') or s.get('ots_ca_country', 'WW')
        ca_state = params.get('ca_state') or s.get('ots_ca_state', 'XX')
        ca_city = params.get('ca_city') or s.get('ots_ca_city', 'YY')
        ca_org = params.get('ca_org') or s.get('ots_ca_org', 'ZZ')
        ca_ou = params.get('ca_ou') or s.get('ots_ca_ou', '')
        ca_name = params.get('ca_name') or s.get('ots_ca_name', 'OpenTAKServer-CA')

        # LDAP settings
        ldap_enabled = 'true' if params.get('ldap_enabled') else 'false'
        ldap_host = 'ldap://127.0.0.1:389'
        ldap_base_dn = 'DC=takldap'
        ldap_user_dn = 'ou=users,DC=takldap'
        ldap_group_dn = 'ou=groups,DC=takldap'
        ldap_bind_user_dn = 'cn=adm_ldapservice,ou=users,DC=takldap'
        ldap_bind_password = ''
        ldap_admin_group = 'ots_admin'

        # Read LDAP creds from Authentik if available
        ak_token = (ctx['_get_authentik_env_value'](s, 'AUTHENTIK_TOKEN') or
                    ctx['_get_authentik_env_value'](s, 'AUTHENTIK_BOOTSTRAP_TOKEN'))
        if ak_token and fqdn:
            try:
                ldap_bind_password = (ctx['_get_authentik_env_value'](s, 'AUTHENTIK_BOOTSTRAP_LDAPSERVICE_PASSWORD') or '')
                if ldap_bind_password:
                    ldap_enabled = 'true'
                    plog('  ✓ Authentik LDAP credentials loaded')
            except Exception:
                plog('  ⚠ Could not read Authentik LDAP credentials — LDAP disabled')

        # Write docker-compose.yml
        compose_content = OTS_DOCKER_COMPOSE.format(
            ots_dir=ots_dir_,
            ots_container=OTS_CONTAINER,
            rabbit_container=OTS_RABBIT_CONTAINER,
            secret_key=secret_key,
            password_salt=password_salt,
            rabbit_password=rabbit_password,
            ca_name=ca_name,
            ca_password=ca_password,
            ca_country=ca_country,
            ca_state=ca_state,
            ca_city=ca_city,
            ca_org=ca_org,
            ca_ou=ca_ou,
            ldap_enabled=ldap_enabled,
            ldap_admin_group=ldap_admin_group,
            ldap_host=ldap_host,
            ldap_base_dn=ldap_base_dn,
            ldap_user_dn=ldap_user_dn,
            ldap_group_dn=ldap_group_dn,
            ldap_bind_user_dn=ldap_bind_user_dn,
            ldap_bind_password=ldap_bind_password,
        )
        ctx['_write_priv'](os.path.join(ots_dir_, 'docker-compose.yml'), compose_content)
        plog('✓ docker-compose.yml written')

        # Step 4: Build and start containers
        plog('')
        plog('━━━ Step 4/8: Building & Starting Containers ━━━')
        plog('  ⏳ First build may take several minutes...')
        r = _compose(ctx, ots_dir_, 'up -d --build', timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f'docker compose up --build failed:\n{r.stderr[-500:]}')
        plog('✓ Containers built and started')

        # Step 5: Wait for OTS to be healthy
        plog('')
        plog('━━━ Step 5/8: Waiting for OpenTAKServer to start ━━━')
        for i in range(30):
            try:
                r = ctx['probe_run'](
                    ['docker', 'inspect', '--format', '{{.State.Health.Status}}', OTS_CONTAINER],
                    text=True, timeout=5)
                status = (r.stdout or '').strip()
                if status == 'healthy':
                    plog('✓ OpenTAKServer is healthy')
                    break
            except Exception:
                pass
            if i < 29:
                _time.sleep(5)
        else:
            plog('  ⚠ OpenTAKServer may not be fully healthy yet — check logs')

        # Step 6: Firewall
        plog('')
        plog('━━━ Step 6/8: Configuring Firewall ━━━')
        for port_proto in ['8088/tcp', '8089/tcp']:
            ok, msg = ctx['_fw_allow'](8088 if '8088' in port_proto else 8089, 'tcp')
            plog(f'  {"✓" if ok else "⚠"} {port_proto}: {msg}')
        # Admin ports (8080, 8443, 8446) are Caddy-loopback only

        # Step 7: Caddy
        plog('')
        plog('━━━ Step 7/8: Configuring Caddy ━━━')
        if fqdn:
            plog(f'  Caddy vhosts will be: ots.{fqdn}, ots.{fqdn}:8443, ots.{fqdn}:8446')
        else:
            plog('  No FQDN configured — Caddy vhosts skipped')

        # Step 8: Register
        plog('')
        plog('━━━ Step 8/8: Registering Module ━━━')
        s = ctx['load_settings']()
        s['ots_enabled'] = True
        s['ots_secret_key'] = secret_key
        s['ots_password_salt'] = password_salt
        s['ots_ca_password'] = ca_password
        s['ots_rabbit_password'] = rabbit_password
        s['ots_ca_country'] = ca_country
        s['ots_ca_state'] = ca_state
        s['ots_ca_city'] = ca_city
        s['ots_ca_org'] = ca_org
        s['ots_ca_ou'] = ca_ou
        s['ots_ca_name'] = ca_name
        s['ots_version'] = version
        s['ots_ldap_enabled'] = ldap_enabled == 'true'
        ctx['save_settings'](s)
        ctx['generate_caddyfile'](s)
        if ctx['_caddy_reload'](plog):
            plog('✓ Caddy reloaded')

        plog('')
        plog('✓ OpenTAKServer deployed successfully.')
        plog(f'  Version: {version}')
        plog(f'  CoT TCP:  port 8088')
        plog(f'  CoT SSL:  port 8089')
        if fqdn:
            plog(f'  Web UI:   https://ots.{fqdn}')
        else:
            plog(f'  Web UI:   https://<server-ip>:8443')
        plog(f'  Enrollment: port 8446')
        job.update({'running': False, 'complete': True, 'error': False})
    except Exception as exc:
        plog(f'ERROR: {exc}')
        job.update({'running': False, 'complete': False, 'error': True})


def uninstall(ctx, job, params):
    """Compose down + Caddy/Authentik dereg."""
    steps = []
    dirpath = ots_dir(ctx)
    if subprocess.run(ctx['_sudo_wrap'](['test', '-f', os.path.join(dirpath, 'docker-compose.yml')]),
                      capture_output=True, timeout=10).returncode == 0:
        _compose(ctx, dirpath, 'down -v', timeout=120)
        steps.append('Containers stopped and removed')
    s = ctx['load_settings']()
    s['ots_enabled'] = False
    ctx['save_settings'](s)
    ctx['generate_caddyfile'](s)
    ctx['_caddy_reload']()
    steps.append('Caddy subdomains removed')
    ctx['_deregister_authentik_proxy_app'](s, 'ots', 'OpenTAKServer Proxy')
    steps.append('Authentik proxy app deregistered')
    return {'success': True, 'steps': steps}


# ── Self-update ──────────────────────────────────────────────────────────────

_update_status = {'running': False, 'complete': False, 'error': False, 'log': []}


def _run_update(ctx):
    global _update_status
    log = []
    def plog(msg):
        log.append(msg)
        _update_status['log'] = list(log)
    try:
        dirpath = ots_dir(ctx)

        # Step 0: Pre-upgrade snapshot
        plog('━━━ Step 0/4: Pre-upgrade snapshot ━━━')
        snapshot_dir = os.path.join(dirpath, 'snapshots')
        os.makedirs(snapshot_dir, exist_ok=True)
        import datetime as _dt
        snapshot_name = f"pre-upgrade-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        snapshot_path = os.path.join(snapshot_dir, snapshot_name)
        os.makedirs(snapshot_path, exist_ok=True)

        # Backup config files
        for f in ['docker-compose.yml', 'data/config.yml']:
            src = os.path.join(dirpath, f)
            if os.path.exists(src):
                dst = os.path.join(snapshot_path, os.path.basename(f))
                subprocess.run(['cp', src, dst], capture_output=True, timeout=10)

        # Backup data directory (excluding large files)
        data_dir = os.path.join(dirpath, 'data')
        if os.path.isdir(data_dir):
            subprocess.run(['tar', '-czf', os.path.join(snapshot_path, 'data.tar.gz'),
                           '--exclude= recordings', '--exclude=logs',
                           '-C', dirpath, 'data'], capture_output=True, timeout=60)

        plog(f'✓ Snapshot saved: {snapshot_name}')

        # Step 1: Pull latest source
        plog('')
        plog('━━━ Step 1/4: Pulling latest source ━━━')
        ctx['_module_git'](dirpath, 'checkout', '--', '.', timeout=60)
        r = ctx['_module_git'](dirpath, 'pull', '--ff-only', timeout=120)
        plog((r.stdout + r.stderr).strip() or '(no output)')
        if r.returncode != 0:
            raise RuntimeError(f'git pull failed: {r.stderr[:300]}')

        # Step 2: Rebuild containers
        plog('')
        plog('━━━ Step 2/4: Rebuilding containers ━━━')
        r = _compose(ctx, dirpath, 'up -d --build', timeout=600)
        plog((r.stdout + r.stderr).strip()[-600:] or '(no output)')
        if r.returncode != 0:
            raise RuntimeError(f'docker compose build failed: {r.stderr[:300]}')

        # Step 3: Sync LDAP credentials if Authentik is configured
        plog('')
        plog('━━━ Step 3/4: Syncing LDAP credentials ━━━')
        s = ctx['load_settings']()
        fqdn = (s.get('fqdn') or '').strip()
        if fqdn and s.get('ots_ldap_enabled'):
            try:
                ak_token = (ctx['_get_authentik_env_value'](s, 'AUTHENTIK_TOKEN') or
                            ctx['_get_authentik_env_value'](s, 'AUTHENTIK_BOOTSTRAP_TOKEN'))
                if ak_token:
                    ldap_pass = ctx['_get_authentik_env_value'](s, 'AUTHENTIK_BOOTSTRAP_LDAPSERVICE_PASSWORD') or ''
                    if ldap_pass:
                        # Update LDAP password in settings
                        s['ots_ldap_bind_password'] = ldap_pass
                        ctx['save_settings'](s)
                        plog('✓ LDAP credentials synced from Authentik')
                    else:
                        plog('  ⚠ No LDAP password found in Authentik')
                else:
                    plog('  ⚠ Authentik not configured — skipping LDAP sync')
            except Exception as e:
                plog(f'  ⚠ LDAP sync failed (non-fatal): {e}')
        else:
            plog('  LDAP not enabled — skipping')

        # Step 4: Save version
        plog('')
        plog('━━━ Step 4/4: Saving version ━━━')
        version_r = ctx['_module_git'](dirpath, 'describe', '--tags', '--always', timeout=8)
        new_version = version_r.stdout.strip()
        s = ctx['load_settings']()
        s['ots_version'] = new_version
        ctx['save_settings'](s)
        plog(f'✓ OpenTAKServer updated to {new_version}')
        _update_status.update({'running': False, 'complete': True, 'error': False})
    except Exception as exc:
        plog(f'ERROR: {exc}')
        _update_status.update({'running': False, 'complete': False, 'error': str(exc)})


# ── Register ─────────────────────────────────────────────────────────────────

def register(ctx):
    from flask import request, jsonify

    def deploy_status_view():
        job = job_state('ots')
        return jsonify({'running': job['running'], 'complete': job['complete'],
                        'error': job['error'], 'log': list(job['log'])})

    def logs_view():
        try:
            r = subprocess.run(
                ctx['_sudo_wrap'](['docker', 'logs', '--tail', '200', OTS_CONTAINER]),
                capture_output=True, text=True, timeout=15)
            raw = (r.stdout + r.stderr).splitlines()
            return jsonify({'lines': raw[-200:]})
        except Exception as e:
            return jsonify({'lines': [], 'error': str(e)})

    def version_view():
        return jsonify(get_version_info(ctx))

    def update_view():
        global _update_status
        if _update_status.get('running'):
            return jsonify({'started': False, 'error': 'Update already in progress'})
        _update_status = {'running': True, 'complete': False, 'error': False, 'log': []}
        threading.Thread(target=_run_update, args=(ctx,), daemon=True).start()
        return jsonify({'started': True})

    def update_status_view():
        return jsonify(_update_status)

    def db_size_view():
        """Get OTS SQLite database size and stats."""
        try:
            dirpath = ots_dir(ctx)
            db_path = os.path.join(dirpath, 'data', 'ots.db')
            if not os.path.exists(db_path):
                return jsonify({'error': 'Database not found'}), 404

            # Get file size
            size_bytes = os.path.getsize(db_path)
            size_mb = round(size_bytes / (1024 * 1024), 2)

            # Get table counts via docker exec
            r = subprocess.run(
                ctx['_sudo_wrap'](['docker', 'exec', OTS_CONTAINER,
                                   'python3', '-c',
                                   'import sqlite3; '
                                   'conn = sqlite3.connect("/app/data/ots.db"); '
                                   'c = conn.cursor(); '
                                   'tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type=\'table\'").fetchall()]; '
                                   'counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}; '
                                   'import json; print(json.dumps(counts))']),
                capture_output=True, text=True, timeout=10)
            table_counts = {}
            if r.returncode == 0 and r.stdout.strip():
                try:
                    table_counts = json.loads(r.stdout.strip())
                except json.JSONDecodeError:
                    pass

            return jsonify({
                'size_bytes': size_bytes,
                'size_mb': size_mb,
                'tables': table_counts,
                'path': db_path
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    def vacuum_view():
        """Run SQLite VACUUM on OTS database."""
        try:
            dirpath = ots_dir(ctx)
            db_path = os.path.join(dirpath, 'data', 'ots.db')
            if not os.path.exists(db_path):
                return jsonify({'error': 'Database not found'}), 404

            # Get size before
            size_before = os.path.getsize(db_path)

            # Run VACUUM via docker exec
            r = subprocess.run(
                ctx['_sudo_wrap'](['docker', 'exec', OTS_CONTAINER,
                                   'python3', '-c',
                                   'import sqlite3; '
                                   'conn = sqlite3.connect("/app/data/ots.db"); '
                                   'conn.execute("VACUUM"); '
                                   'conn.close()']),
                capture_output=True, text=True, timeout=300)

            if r.returncode != 0:
                return jsonify({'error': f'VACUUM failed: {r.stderr[:300]}'}), 500

            # Get size after
            size_after = os.path.getsize(db_path)
            freed = size_before - size_after

            return jsonify({
                'success': True,
                'size_before_mb': round(size_before / (1024 * 1024), 2),
                'size_after_mb': round(size_after / (1024 * 1024), 2),
                'freed_mb': round(freed / (1024 * 1024), 2)
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    register_module({
        'key': 'ots',
        'api_base': '/api/ots',
        'name': 'OpenTAKServer',
        'description': 'Python-based open source TAK Server — RabbitMQ, SQLite, built-in CA',
        'icon': '\U0001F310',  # 🌐 as escape
        'route': '/opentakserver',
        'template': 'opentakserver.html',
        'priority': 4,  # before takserver (priority 3)
        'conflicts': ['takserver'],
        'detect': detect,
        'deploy': deploy,
        'uninstall': uninstall,
        'control_map': {
            'start':   lambda c: _compose_argv(c, 'up', '-d'),
            'stop':    lambda c: _compose_argv(c, 'stop'),
            'restart': lambda c: _compose_argv(c, 'restart'),
        },
        'extra_routes': [
            {'url': '/api/ots/deploy-status', 'methods': ['GET'],
             'endpoint': 'ots_deploy_status', 'view': deploy_status_view},
            {'url': '/api/ots/logs', 'methods': ['GET'],
             'endpoint': 'ots_logs', 'view': logs_view},
            {'url': '/api/ots/version', 'methods': ['GET'],
             'endpoint': 'ots_version', 'view': version_view},
            {'url': '/api/ots/update', 'methods': ['POST'],
             'endpoint': 'ots_update', 'view': update_view},
            {'url': '/api/ots/update-status', 'methods': ['GET'],
             'endpoint': 'ots_update_status', 'view': update_status_view},
            {'url': '/api/ots/db-size', 'methods': ['GET'],
             'endpoint': 'ots_db_size', 'view': db_size_view},
            {'url': '/api/ots/vacuum', 'methods': ['POST'],
             'endpoint': 'ots_vacuum', 'view': vacuum_view},
        ],
        'ports': ['8088/tcp', '8089/tcp'],
        'service_units': [],
        'settings_keys': [
            'ots_enabled', 'ots_secret_key', 'ots_password_salt',
            'ots_ca_password', 'ots_rabbit_password',
            'ots_ca_country', 'ots_ca_state', 'ots_ca_city',
            'ots_ca_org', 'ots_ca_ou', 'ots_ca_name',
            'ots_version', 'ots_ldap_enabled',
        ],
    })
