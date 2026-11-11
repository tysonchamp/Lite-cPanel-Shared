"""Comprehensive resource cleanup helpers for shared hosting user deletion."""

import grp
import json
import logging
import os
import pwd
import re
import shutil
import stat
import subprocess
import time

from security_mgr import validate_input

HOME_BASE = "/home"
SFTP_JAILS_DIR = "/var/sftp_jails"
NGINX_SITES_AVAIL = "/etc/nginx/sites-available"
NGINX_SITES_ENABLED = "/etc/nginx/sites-enabled"
APACHE_SITES_AVAIL = "/etc/apache2/sites-available"
APACHE_SITES_ENABLED = "/etc/apache2/sites-enabled"
LETSENCRYPT_DIR = "/etc/letsencrypt"
NGINX_LOG_DIR = "/var/log/nginx"
APACHE_LOG_DIR = "/var/log/apache2"
VAR_LOG_DIR = "/var/log"

COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
SKIP_WALK_DIRS = frozenset({"node_modules", ".git", ".next", "vendor", "__pycache__", ".venv", "venv"})
PROTECTED_VHOSTS = frozenset({"default", "000-default", "default-ssl", "default-modsecurity"})
PROTECTED_USERNAMES = frozenset({
    "root", "www-data", "nobody", "ubuntu", "admin", "mysql", "mongodb",
    "pure-ftpd", "ftp", "sshd", "daemon", "bin", "sys", "sync", "games",
    "man", "lp", "mail", "news", "uucp", "proxy", "backup", "list", "irc",
    "gnats", "_apt", "systemd-network", "systemd-resolve", "postgres",
    "redis", "messagebus",
})
PROTECTED_MYSQL_USERS = frozenset({
    "root", "mysql.sys", "mysql.session", "mysql.infoschema",
    "mariadb.sys", "debian-sys-maint", "phpmyadmin",
})
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")


def is_valid_target_username(username):
    """Return True if username is a valid, non-protected shared hosting username."""
    if not username or not isinstance(username, str):
        return False
    if username in (".", "..") or username.startswith("-"):
        return False
    if "/" in username or "\\" in username or "\x00" in username:
        return False
    if username.lower() in PROTECTED_USERNAMES:
        return False
    return bool(USERNAME_RE.match(username))


def _is_path_within(path, root_dir):
    """Return True if path equals or resides directly within root_dir."""
    if not path or not root_dir:
        return False
    norm_path = os.path.abspath(path)
    norm_root = os.path.abspath(root_dir)
    return norm_path == norm_root or norm_path.startswith(norm_root + os.sep)


def resolve_safe_user_home(username):
    """Return the validated /home/{username} path, or raise ValueError if unsafe."""
    if not is_valid_target_username(username):
        raise ValueError(f"Refusing unsafe target username: {username!r}")
    base_abs = os.path.abspath(HOME_BASE)
    candidate = os.path.abspath(os.path.join(base_abs, username))
    if os.path.dirname(candidate) != base_abs or os.path.basename(candidate) != username:
        raise ValueError(f"Refusing unsafe home directory path: {candidate!r}")
    return candidate


def _is_valid_domain_target(domain):
    """Return True if domain is safe to map to webserver and SSL config paths."""
    if not domain or not isinstance(domain, str):
        return False
    if "/" in domain or "\\" in domain or ".." in domain:
        return False
    if domain.lower() in PROTECTED_VHOSTS:
        return False
    ok, _ = validate_input(domain, "domain")
    return ok


def _gather_user_domains(main_domain, resources):
    """Collect deduplicated, validated domains owned by a shared hosting user."""
    candidates = []
    for key in ("domains", "nextjs_apps", "docker_apps"):
        candidates.extend(resources.get(key, []) or [])
    if main_domain:
        candidates.append(main_domain)

    seen = set()
    valid_domains = []
    for domain in candidates:
        if domain not in seen and _is_valid_domain_target(domain):
            seen.add(domain)
            valid_domains.append(domain)
    return sorted(valid_domains)


def _match_user_prefix(name, prefix):
    """Return True if name equals prefix or starts with prefix followed by '-', '_', or '/'."""
    if not name or not prefix:
        return False
    return (
        name == prefix
        or name.startswith(f"{prefix}-")
        or name.startswith(f"{prefix}_")
        or name.startswith(f"{prefix}/")
    )


def _unlink_if_exists(path):
    """Remove a file or symlink without following symlinks; return True if removed."""
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, onerror=_handle_rmtree_readonly)
        else:
            os.unlink(path)
        return True
    return False


def _handle_rmtree_readonly(func, path, exc_info):
    """Retry rmtree operations after clearing read-only permission bits."""
    parent = os.path.dirname(path)
    try:
        if parent and os.path.exists(parent):
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        if os.path.lexists(path) and not os.path.islink(path):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
        func(path)
    except Exception:
        raise exc_info[1]


def _kill_user_processes(target_user, warnings):
    """Terminate all active OS processes belonging to a non-root Linux user."""
    if not is_valid_target_username(target_user):
        return
    try:
        pw = pwd.getpwnam(target_user)
    except KeyError:
        return

    if pw.pw_uid == 0:
        warnings.append(f"Refused to kill processes for UID 0 user {target_user}.")
        return

    try:
        if shutil.which("loginctl"):
            subprocess.run(["loginctl", "terminate-user", target_user], capture_output=True, text=True, timeout=10)
    except Exception:
        pass

    try:
        term_res = subprocess.run(["pkill", "-TERM", "-u", target_user], capture_output=True, text=True, timeout=10)
        if term_res.returncode == 0:
            time.sleep(0.2)
        subprocess.run(["pkill", "-KILL", "-u", target_user], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        pass
    except Exception as exc:
        warnings.append(f"Process cleanup warning for {target_user}: {exc}")


def _cleanup_pm2_processes(username, domains, user_home, report):
    """Delete all PM2 processes owned by the user or running from their home directory."""
    import process_mgr

    if not process_mgr.is_pm2_installed():
        return

    pm2_cmd = process_mgr.get_pm2_cmd()
    domain_set = set(domains)

    # Stop any per-user PM2 daemon if user had one inside their home directory
    user_pm2_home = os.path.join(user_home, ".pm2")
    if os.path.exists(user_pm2_home):
        try:
            env_user = os.environ.copy()
            env_user["PM2_HOME"] = user_pm2_home
            subprocess.run([pm2_cmd, "kill"], capture_output=True, text=True, env=env_user, timeout=15)
        except Exception as exc:
            report["warnings"].append(f"User PM2 daemon stop failed: {exc}")

    try:
        processes = process_mgr.list_processes() or []
    except Exception as exc:
        report["warnings"].append(f"Failed to list PM2 processes: {exc}")
        return

    matched_targets = []
    for proc in processes:
        name = proc.get("name", "") or ""
        pm_id = proc.get("pm_id")
        pm2_env = proc.get("pm2_env") or {}
        nested_env = pm2_env.get("env") or {}

        path_candidates = [
            pm2_env.get("pm_cwd"),
            pm2_env.get("cwd"),
            pm2_env.get("pm_exec_path"),
            pm2_env.get("pm_out_log_path"),
            pm2_env.get("pm_err_log_path"),
            pm2_env.get("pm_pid_path"),
        ]
        env_user = pm2_env.get("USER") or nested_env.get("USER")
        env_home = pm2_env.get("HOME") or nested_env.get("HOME")

        is_owner = (
            name in domain_set
            or _match_user_prefix(name, username)
            or env_user == username
            or env_home == user_home
            or any(_is_path_within(p, user_home) for p in path_candidates if p)
        )
        if is_owner:
            target = str(pm_id) if pm_id is not None else name
            if target:
                matched_targets.append((target, name or target))

    if not matched_targets:
        return

    env_root = os.environ.copy()
    env_root["PM2_HOME"] = process_mgr.PM2_HOME
    deleted_any = False

    for target, label in matched_targets:
        try:
            res = subprocess.run([pm2_cmd, "delete", target], capture_output=True, text=True, env=env_root, timeout=15)
            if res.returncode == 0:
                deleted_any = True
                report["pm2_processes"].append(label)
            else:
                # Fallback to process_mgr API if target differed
                ok, msg = process_mgr.manage_process("delete", label)
                if ok:
                    deleted_any = True
                    report["pm2_processes"].append(label)
                else:
                    report["warnings"].append(f"PM2 delete failed for {label}: {msg}")
        except Exception as exc:
            report["warnings"].append(f"PM2 delete error for {label}: {exc}")

    if deleted_any:
        try:
            save_res = subprocess.run(
                [pm2_cmd, "save", "--force"],
                capture_output=True,
                text=True,
                env=env_root,
                timeout=15,
            )
            if save_res.returncode != 0:
                err = (save_res.stderr or save_res.stdout or "").strip()
                report["warnings"].append(f"PM2 save warning: {err}")
        except Exception as exc:
            report["warnings"].append(f"PM2 save warning: {exc}")


def _find_compose_dirs(user_home, max_depth=4):
    """Find directories inside user_home containing Docker Compose definitions."""
    if not os.path.isdir(user_home) or os.path.islink(user_home):
        return []

    compose_dirs = []
    base_depth = user_home.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(user_home, followlinks=False):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_WALK_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        if any(fname in files for fname in COMPOSE_FILENAMES):
            compose_dirs.append(root)
    return compose_dirs


def _is_container_owned_by_user(inspect_data, username, user_home, domains):
    """Return True if an inspected Docker container belongs to username or their domains."""
    cname = (inspect_data.get("Name") or "").lstrip("/")
    config = inspect_data.get("Config") or {}
    workdir = config.get("WorkingDir") or ""
    labels = config.get("Labels") or {}
    compose_workdir = labels.get("com.docker.compose.project.working_dir") or ""
    compose_cfg_files = labels.get("com.docker.compose.project.config_files") or ""
    compose_project = labels.get("com.docker.compose.project") or ""
    mounts = inspect_data.get("Mounts") or []

    if labels.get("lite_cpanel.user") == username:
        return True
    if _is_path_within(workdir, user_home) or _is_path_within(compose_workdir, user_home):
        return True
    for cfg in compose_cfg_files.split(","):
        if cfg and _is_path_within(cfg.strip(), user_home):
            return True
    for mount in mounts:
        src = mount.get("Source") or ""
        if src and _is_path_within(src, user_home):
            return True

    if _match_user_prefix(cname, username) or compose_project == username:
        return True

    for domain in domains:
        d_nodot = domain.replace(".", "")
        if _match_user_prefix(cname, domain) or _match_user_prefix(cname, d_nodot):
            return True
        if compose_project in (domain, d_nodot):
            return True
    return False


def _is_image_owned_by_user(repo, username, domains):
    """Return True if an image repository name is explicitly scoped to the user or domains."""
    if not repo or repo == "<none>":
        return False
    # Require explicit namespace separator (-, _, /) on username so single-word official images
    # like "node" or "python" are never touched just because of a matching username.
    if repo.startswith((f"{username}/", f"{username}-", f"{username}_")):
        return True
    for domain in domains:
        d_nodot = domain.replace(".", "")
        if _match_user_prefix(repo, domain) or repo.startswith((f"{d_nodot}/", f"{d_nodot}-", f"{d_nodot}_")):
            return True
    return False


def _list_docker_images():
    """Return parsed JSON entries from `docker images --no-trunc`."""
    res = subprocess.run(
        ["docker", "images", "--no-trunc", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode != 0:
        return []
    images = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                images.append(parsed)
            elif isinstance(parsed, list):
                images.extend(item for item in parsed if isinstance(item, dict))
        except json.JSONDecodeError:
            pass
    return images


def _cleanup_docker_resources(username, domains, user_home, report):
    """Tear down user Docker Compose stacks, containers, volumes, networks, and images."""
    import docker_mgr

    # Clean compose build logs regardless of docker binary state
    for domain in domains:
        _unlink_if_exists(os.path.join(VAR_LOG_DIR, f"docker_compose_{domain}.log"))

    if not docker_mgr.is_docker_installed():
        return

    touched_docker = False

    # 1. Tear down Docker Compose projects located in user's directory tree
    for compose_dir in _find_compose_dirs(user_home):
        touched_docker = True
        try:
            res = subprocess.run(
                ["docker", "compose", "down", "--rmi", "all", "--volumes", "--remove-orphans"],
                cwd=compose_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if res.returncode != 0:
                # Fallback without --rmi all in case another tenant container uses a shared base image
                fallback = subprocess.run(
                    ["docker", "compose", "down", "--volumes", "--remove-orphans"],
                    cwd=compose_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if fallback.returncode != 0:
                    err = (fallback.stderr or fallback.stdout or res.stderr or "").strip()
                    report["warnings"].append(f"Docker compose down failed in {compose_dir}: {err}")
                else:
                    report["docker_compose_stacks"].append(compose_dir)
            else:
                report["docker_compose_stacks"].append(compose_dir)
        except Exception as exc:
            report["warnings"].append(f"Docker compose cleanup error in {compose_dir}: {exc}")

    # 2. Inspect remaining containers to find user-owned containers and their image IDs
    candidate_image_ids = set()
    surviving_image_ids = set()
    try:
        ps_res = subprocess.run(
            ["docker", "ps", "-a", "-q", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        cids = [line.strip() for line in ps_res.stdout.splitlines() if line.strip()] if ps_res.returncode == 0 else []
    except Exception as exc:
        report["warnings"].append(f"Failed listing Docker containers: {exc}")
        cids = []

    if cids:
        try:
            insp_res = subprocess.run(
                ["docker", "inspect"] + cids,
                capture_output=True,
                text=True,
                timeout=20,
            )
            inspected = json.loads(insp_res.stdout) if insp_res.returncode == 0 and insp_res.stdout.strip() else []
        except Exception as exc:
            report["warnings"].append(f"Failed inspecting Docker containers: {exc}")
            inspected = []

        for c in inspected:
            cid = c.get("Id", "")
            cname = (c.get("Name") or cid).lstrip("/")
            img_id = c.get("Image", "")
            if _is_container_owned_by_user(c, username, user_home, domains):
                try:
                    rm_res = subprocess.run(
                        ["docker", "rm", "-f", "-v", cid],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if rm_res.returncode == 0:
                        touched_docker = True
                        report["docker_containers"].append(cname)
                        if img_id:
                            candidate_image_ids.add(img_id)
                    else:
                        if img_id:
                            surviving_image_ids.add(img_id)
                        err = (rm_res.stderr or rm_res.stdout or "").strip()
                        report["warnings"].append(f"Failed removing container {cname}: {err}")
                except Exception as exc:
                    if img_id:
                        surviving_image_ids.add(img_id)
                    report["warnings"].append(f"Error removing container {cname}: {exc}")
            elif img_id:
                surviving_image_ids.add(img_id)

    # 3. Remove user/domain images not referenced by any surviving container
    try:
        all_images = _list_docker_images()
    except Exception as exc:
        report["warnings"].append(f"Failed listing Docker images: {exc}")
        all_images = []

    removable_img_targets = {}
    for img in all_images:
        img_id = img.get("ID", "")
        repo = img.get("Repository", "") or ""
        tag = img.get("Tag", "") or ""
        if not img_id or img_id in surviving_image_ids:
            continue
        if img_id in candidate_image_ids or _is_image_owned_by_user(repo, username, domains):
            label = f"{repo}:{tag}" if repo not in ("", "<none>") and tag not in ("", "<none>") else img_id
            # Deduplicate by img_id using img_id for removal so multi-tag leftovers clean together
            removable_img_targets.setdefault(img_id, label)

    for img_id, label in removable_img_targets.items():
        try:
            rmi_res = subprocess.run(
                ["docker", "rmi", "-f", img_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if rmi_res.returncode == 0:
                touched_docker = True
                report["docker_images"].append(label)
            else:
                err = (rmi_res.stderr or rmi_res.stdout or "").strip()
                report["warnings"].append(f"Failed removing Docker image {label}: {err}")
        except Exception as exc:
            report["warnings"].append(f"Error removing Docker image {label}: {exc}")

    # 4. Clean dangling untagged build layers left by user compose builds
    if touched_docker:
        try:
            subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True, timeout=30)
        except Exception:
            pass


def _cleanup_domains_and_ssl(domains, report):
    """Remove Apache/Nginx vhosts, SSL certs, and logs for user domains, reloading once."""
    apache_touched = False
    nginx_touched = False

    for domain in domains:
        domain_removed = False

        # Nginx vhost symlink & config file
        nginx_enabled = os.path.join(NGINX_SITES_ENABLED, domain)
        nginx_avail = os.path.join(NGINX_SITES_AVAIL, domain)
        if _unlink_if_exists(nginx_enabled):
            nginx_touched = True
            domain_removed = True
        if _unlink_if_exists(nginx_avail):
            nginx_touched = True
            domain_removed = True

        # Apache standard & Let's Encrypt SSL virtual hosts
        for suffix in (".conf", "-le-ssl.conf"):
            conf_name = f"{domain}{suffix}"
            apache_avail = os.path.join(APACHE_SITES_AVAIL, conf_name)
            apache_enabled = os.path.join(APACHE_SITES_ENABLED, conf_name)
            if os.path.lexists(apache_enabled) or os.path.lexists(apache_avail):
                try:
                    subprocess.run(["a2dissite", conf_name], capture_output=True, text=True, timeout=15)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    report["warnings"].append(f"a2dissite warning for {conf_name}: {exc}")
                _unlink_if_exists(apache_enabled)
                _unlink_if_exists(apache_avail)
                apache_touched = True
                domain_removed = True

        # Certbot SSL certificates for exact domain
        le_live = os.path.join(LETSENCRYPT_DIR, "live", domain)
        le_archive = os.path.join(LETSENCRYPT_DIR, "archive", domain)
        le_renewal = os.path.join(LETSENCRYPT_DIR, "renewal", f"{domain}.conf")
        if any(os.path.lexists(p) for p in (le_live, le_archive, le_renewal)):
            if shutil.which("certbot"):
                try:
                    c_res = subprocess.run(
                        ["certbot", "delete", "--cert-name", domain, "--non-interactive"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if c_res.returncode != 0:
                        err = (c_res.stderr or c_res.stdout or "").strip()
                        report["warnings"].append(f"Certbot delete warning for {domain}: {err}")
                except Exception as exc:
                    report["warnings"].append(f"Certbot delete error for {domain}: {exc}")
            for p in (le_live, le_archive, le_renewal):
                _unlink_if_exists(p)
            report["ssl_certs"].append(domain)
            domain_removed = True

        # Domain webserver logs
        for log_path in (
            os.path.join(NGINX_LOG_DIR, f"{domain}_access.log"),
            os.path.join(NGINX_LOG_DIR, f"{domain}_error.log"),
            os.path.join(NGINX_LOG_DIR, f"{domain}_docker_access.log"),
            os.path.join(NGINX_LOG_DIR, f"{domain}_docker_error.log"),
            os.path.join(APACHE_LOG_DIR, f"{domain}_access.log"),
            os.path.join(APACHE_LOG_DIR, f"{domain}_error.log"),
            os.path.join(APACHE_LOG_DIR, f"{domain}_ssl_access.log"),
            os.path.join(APACHE_LOG_DIR, f"{domain}_ssl_error.log"),
        ):
            _unlink_if_exists(log_path)

        if domain_removed:
            report["domains"].append(domain)

    for touched, svc in ((apache_touched, "apache2"), (nginx_touched, "nginx")):
        if not touched:
            continue
        try:
            res = subprocess.run(["systemctl", "reload", svc], capture_output=True, text=True, timeout=20)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                report["warnings"].append(f"Service reload warning ({svc}): {err}")
        except FileNotFoundError:
            pass
        except Exception as exc:
            report["warnings"].append(f"Service reload error ({svc}): {exc}")


def _cleanup_mysql_resources(username, tracked_dbs, report):
    """Drop all MySQL/MariaDB databases and DB users owned by username."""
    import database_mgr

    dbs_to_drop = {db for db in (tracked_dbs or []) if db}
    try:
        for db in database_mgr.get_databases() or []:
            if db.startswith(f"{username}_"):
                dbs_to_drop.add(db)
    except Exception as exc:
        report["warnings"].append(f"MySQL database discovery failed: {exc}")

    for db_name in sorted(dbs_to_drop):
        try:
            ok, msg = database_mgr.delete_database(db_name)
            if ok:
                report["mysql_databases"].append(db_name)
            else:
                report["warnings"].append(f"MySQL drop failed for {db_name}: {msg}")
        except Exception as exc:
            report["warnings"].append(f"MySQL drop error for {db_name}: {exc}")

    # Clean any orphaned MySQL users belonging to the shared account
    try:
        conn = database_mgr.get_mysql_connection()
    except Exception as exc:
        report["warnings"].append(f"MySQL user cleanup connection failed: {exc}")
        conn = None

    if not conn:
        return

    try:
        escaped_prefix = username.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT User, Host FROM mysql.user WHERE User = %s OR User LIKE %s",
                (username, f"{escaped_prefix}\\_%"),
            )
            rows = cursor.fetchall() or []
            for row in rows:
                db_user = row["User"] if isinstance(row, dict) else row[0]
                db_host = row["Host"] if isinstance(row, dict) else row[1]
                if db_user in PROTECTED_MYSQL_USERS:
                    continue
                if db_user != username and not db_user.startswith(f"{username}_"):
                    continue
                u_esc = db_user.replace("'", "''")
                h_esc = db_host.replace("'", "''")
                cursor.execute(f"DROP USER IF EXISTS '{u_esc}'@'{h_esc}'")
                report["mysql_users"].append(f"{db_user}@{db_host}")
            if report["mysql_users"]:
                cursor.execute("FLUSH PRIVILEGES")
        conn.commit()
    except Exception as exc:
        report["warnings"].append(f"MySQL user cleanup warning: {exc}")
    finally:
        conn.close()


def _cleanup_mongodb_resources(username, tracked_dbs, report):
    """Drop all MongoDB databases and users owned by username."""
    import mongodb_mgr

    if not mongodb_mgr.check_mongodb_installed():
        return

    dbs_to_drop = {db for db in (tracked_dbs or []) if db}
    try:
        for db_info in mongodb_mgr.get_databases(role="admin") or []:
            name = db_info.get("name") if isinstance(db_info, dict) else str(db_info)
            if name and name.startswith(f"{username}_"):
                dbs_to_drop.add(name)
    except Exception as exc:
        report["warnings"].append(f"MongoDB discovery failed: {exc}")

    for db_name in sorted(dbs_to_drop):
        try:
            ok, msg = mongodb_mgr.delete_database(db_name, role="admin", username=username)
            if ok:
                report["mongodb_databases"].append(db_name)
            else:
                report["warnings"].append(f"MongoDB drop failed for {db_name}: {msg}")
        except Exception as exc:
            report["warnings"].append(f"MongoDB drop error for {db_name}: {exc}")


def _discover_user_ftp_accounts(username, user_home):
    """Discover all Pure-FTPd / SFTP accounts belonging to username."""
    import ftp_mgr

    accounts = {}
    try:
        for entry in ftp_mgr.get_ftp_users() or []:
            ftp_user = entry.get("username", "")
            directory = entry.get("directory", "")
            if not is_valid_target_username(ftp_user):
                continue
            if ftp_user == username or _is_path_within(directory, user_home):
                accounts[ftp_user] = directory
    except Exception:
        pass

    if os.path.isdir(SFTP_JAILS_DIR):
        try:
            for ftp_user in os.listdir(SFTP_JAILS_DIR):
                if not is_valid_target_username(ftp_user):
                    continue
                jail_path = os.path.join(SFTP_JAILS_DIR, ftp_user)
                resolved = os.path.realpath(jail_path)
                if ftp_user == username or _is_path_within(resolved, user_home):
                    accounts.setdefault(ftp_user, resolved)
        except OSError:
            pass

    return accounts


def _cleanup_ftp_accounts(username, user_home, report):
    """Delete Pure-FTPd virtual users, SFTP jails, and sub-FTP Linux accounts for username."""
    import ftp_mgr

    accounts = _discover_user_ftp_accounts(username, user_home)
    pureftpd_ready = ftp_mgr.check_pureftpd_installed()

    # Kill active sessions for sub-FTP accounts before userdel runs
    for ftp_user in sorted(accounts):
        _kill_user_processes(ftp_user, report["warnings"])

    for ftp_user in sorted(accounts):
        deleted_ok = False
        if pureftpd_ready:
            try:
                ok, msg = ftp_mgr.delete_ftp_user(ftp_user)
                if ok:
                    deleted_ok = True
                elif "not found" not in msg.lower():
                    report["warnings"].append(f"FTP delete warning for {ftp_user}: {msg}")
            except Exception as exc:
                report["warnings"].append(f"FTP delete error for {ftp_user}: {exc}")

        # Remove www-data group membership added for SFTP Chroot permissions
        try:
            subprocess.run(["gpasswd", "-d", "www-data", ftp_user], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            pass
        except Exception:
            pass

        # Clean SFTP jail symlink left behind by delete_ftp_user
        jail_path = os.path.join(SFTP_JAILS_DIR, ftp_user)
        try:
            if _unlink_if_exists(jail_path):
                deleted_ok = True
        except Exception as exc:
            report["warnings"].append(f"SFTP jail cleanup warning for {ftp_user}: {exc}")

        # Ensure sub-FTP Linux user and primary group are removed even if Pure-FTPd wasn't used
        if ftp_user != username:
            try:
                pw = pwd.getpwnam(ftp_user)
                if pw.pw_uid != 0:
                    subprocess.run(["userdel", "-f", ftp_user], capture_output=True, text=True, timeout=15)
                    deleted_ok = True
            except KeyError:
                pass
            except Exception as exc:
                report["warnings"].append(f"Sub-FTP userdel warning for {ftp_user}: {exc}")

            try:
                grp.getgrnam(ftp_user)
                subprocess.run(["groupdel", ftp_user], capture_output=True, text=True, timeout=10)
            except KeyError:
                pass
            except Exception:
                pass

        if deleted_ok:
            report["ftp_accounts"].append(ftp_user)


def _remove_user_crontab(username, warnings):
    """Remove the target user's system crontab if present."""
    try:
        res = subprocess.run(["crontab", "-u", username, "-r"], capture_output=True, text=True, timeout=10)
        if res.returncode not in (0, 1):
            err = (res.stderr or res.stdout or "").strip()
            if err and "no crontab" not in err.lower():
                warnings.append(f"Crontab removal warning for {username}: {err}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        warnings.append(f"Crontab removal error for {username}: {exc}")


def _cleanup_linux_user_and_home(username, user_home, report):
    """Delete Linux user/group, SFTP jail residue, and /home/{username}."""
    try:
        subprocess.run(["gpasswd", "-d", "www-data", username], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        pw = pwd.getpwnam(username)
        if pw.pw_uid == 0:
            report["warnings"].append(f"Refused userdel for UID 0 user {username}.")
        else:
            res = subprocess.run(["userdel", "-f", "-r", username], capture_output=True, text=True, timeout=20)
            # 0: success, 6: does not exist, 12: home/mail removal warning (handled explicitly below)
            if res.returncode not in (0, 6, 12):
                err = (res.stderr or res.stdout or "").strip()
                report["warnings"].append(f"userdel warning for {username}: {err}")
            elif res.returncode in (0, 12):
                report["linux_user_deleted"] = True
    except KeyError:
        # Primary Linux user may already have been deleted by delete_ftp_user(username)
        if username in report.get("ftp_accounts", []):
            report["linux_user_deleted"] = True
    except Exception as exc:
        report["warnings"].append(f"Linux user cleanup error for {username}: {exc}")

    try:
        grp.getgrnam(username)
        subprocess.run(["groupdel", username], capture_output=True, text=True, timeout=10)
    except KeyError:
        pass
    except Exception:
        pass

    _unlink_if_exists(os.path.join(SFTP_JAILS_DIR, username))
    for mail_dir in ("/var/spool/mail", "/var/mail"):
        _unlink_if_exists(os.path.join(mail_dir, username))

    # Explicitly remove /home/{username} because SFTP Chroot changes its owner to root:root,
    # causing `userdel -r` to skip removing the directory.
    try:
        if os.path.lexists(user_home):
            _unlink_if_exists(user_home)
            report["home_deleted"] = True
    except Exception as exc:
        report["warnings"].append(f"Failed deleting home directory {user_home}: {exc}")


def cleanup_user_system_resources(username, main_domain=None, resources=None):
    """Execute full system and service resource deletion for a shared hosting user."""
    resources = resources or {}
    user_home = resolve_safe_user_home(username)
    all_domains = _gather_user_domains(main_domain, resources)

    report = {
        "username": username,
        "user_home": user_home,
        "all_domains": all_domains,
        "domains": [],
        "ssl_certs": [],
        "pm2_processes": [],
        "docker_compose_stacks": [],
        "docker_containers": [],
        "docker_images": [],
        "mysql_databases": [],
        "mysql_users": [],
        "mongodb_databases": [],
        "ftp_accounts": [],
        "linux_user_deleted": False,
        "home_deleted": False,
        "warnings": [],
    }

    # 1. Stop & remove PM2 apps while PM2 state and paths still exist
    try:
        _cleanup_pm2_processes(username, all_domains, user_home, report)
    except Exception as exc:
        logging.exception("PM2 cleanup failed for user %s", username)
        report["warnings"].append(f"PM2 cleanup error: {exc}")

    # 2. Tear down Docker Compose stacks, containers, volumes, networks, and images
    try:
        _cleanup_docker_resources(username, all_domains, user_home, report)
    except Exception as exc:
        logging.exception("Docker cleanup failed for user %s", username)
        report["warnings"].append(f"Docker cleanup error: {exc}")

    # 3. Remove Apache/Nginx virtual hosts, SSL certs, and logs, then reload once
    try:
        _cleanup_domains_and_ssl(all_domains, report)
    except Exception as exc:
        logging.exception("Domain/SSL cleanup failed for user %s", username)
        report["warnings"].append(f"Domain/SSL cleanup error: {exc}")

    # 4. Drop MySQL/MariaDB databases and orphaned DB users
    try:
        _cleanup_mysql_resources(username, resources.get("databases", []), report)
    except Exception as exc:
        logging.exception("MySQL cleanup failed for user %s", username)
        report["warnings"].append(f"MySQL cleanup error: {exc}")

    # 5. Drop MongoDB databases and users
    try:
        _cleanup_mongodb_resources(username, resources.get("mongodb", []), report)
    except Exception as exc:
        logging.exception("MongoDB cleanup failed for user %s", username)
        report["warnings"].append(f"MongoDB cleanup error: {exc}")

    # 6. Remove user crontab & terminate running user OS processes before FTP/userdel
    _remove_user_crontab(username, report["warnings"])
    _kill_user_processes(username, report["warnings"])

    # 7. Delete FTP/SFTP virtual accounts, jail symlinks, and sub-FTP Linux users
    try:
        _cleanup_ftp_accounts(username, user_home, report)
    except Exception as exc:
        logging.exception("FTP cleanup failed for user %s", username)
        report["warnings"].append(f"FTP cleanup error: {exc}")

    # 8. Delete primary Linux user, group, and /home/{username}
    try:
        _cleanup_linux_user_and_home(username, user_home, report)
    except Exception as exc:
        logging.exception("Linux user/home cleanup failed for user %s", username)
        report["warnings"].append(f"System user/home cleanup error: {exc}")

    return report


def format_cleanup_summary(username, report):
    """Build a concise human-readable summary of cleaned resources."""
    parts = []
    if report.get("home_deleted"):
        parts.append("home files")
    if report.get("linux_user_deleted"):
        parts.append("Linux user")
    if report.get("domains"):
        parts.append(f"{len(report['domains'])} domain(s)")
    if report.get("ssl_certs"):
        parts.append(f"{len(report['ssl_certs'])} SSL cert(s)")
    if report.get("ftp_accounts"):
        parts.append(f"{len(report['ftp_accounts'])} FTP/SFTP account(s)")
    if report.get("mysql_databases"):
        parts.append(f"{len(report['mysql_databases'])} MySQL DB(s)")
    if report.get("mysql_users"):
        parts.append(f"{len(report['mysql_users'])} MySQL user(s)")
    if report.get("mongodb_databases"):
        parts.append(f"{len(report['mongodb_databases'])} MongoDB DB(s)")
    if report.get("pm2_processes"):
        parts.append(f"{len(report['pm2_processes'])} PM2 process(es)")
    if report.get("docker_compose_stacks"):
        parts.append(f"{len(report['docker_compose_stacks'])} Compose stack(s)")
    if report.get("docker_containers"):
        parts.append(f"{len(report['docker_containers'])} Docker container(s)")
    if report.get("docker_images"):
        parts.append(f"{len(report['docker_images'])} Docker image(s)")

    if parts:
        return f"User '{username}' deleted along with: {', '.join(parts)}."
    return f"User '{username}' deleted completely."
