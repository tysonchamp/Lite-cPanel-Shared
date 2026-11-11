import os
import subprocess
from hosting_mgr import (
    can_add_resource,
    add_user_resource,
    remove_user_resource,
    get_users,
    get_owner_of_resource,
)
from security_mgr import validate_input
import user_cleanup_mgr

WWW_BASE = '/var/www'

def get_virtual_hosts(role='admin', username=None):
    """
    Returns a list of dictionaries with virtual host information, grouped by domain.
    Checks both Nginx and Apache directories.
    """
    grouped_vhosts = {}

    nginx_dir = user_cleanup_mgr.NGINX_SITES_AVAIL
    nginx_enabled_dir = user_cleanup_mgr.NGINX_SITES_ENABLED
    if os.path.exists(nginx_dir):
        for f in os.listdir(nginx_dir):
            if f not in ['default', 'default-modsecurity.conf']:
                enabled = os.path.exists(os.path.join(nginx_enabled_dir, f))
                domain = f
                if domain not in grouped_vhosts:
                    grouped_vhosts[domain] = {
                        'domain': domain, 
                        'servers': [], 
                        'enabled': enabled, 
                        'config_paths': {}, 
                        'has_ssl': False
                    }
                grouped_vhosts[domain]['servers'].append('Nginx')
                grouped_vhosts[domain]['config_paths']['Nginx'] = os.path.join(nginx_dir, f)
                grouped_vhosts[domain]['enabled'] = grouped_vhosts[domain]['enabled'] or enabled

    # Check Apache
    apache_dir = user_cleanup_mgr.APACHE_SITES_AVAIL
    apache_enabled_dir = user_cleanup_mgr.APACHE_SITES_ENABLED
    if os.path.exists(apache_dir):
        for f in os.listdir(apache_dir):
            if f not in ['000-default.conf', 'default-ssl.conf', 'default-modsecurity.conf']:
                is_ssl_conf = f.endswith('-le-ssl.conf')
                domain = f[:-len('-le-ssl.conf')] if is_ssl_conf else f.replace('.conf', '')
                enabled = os.path.exists(os.path.join(apache_enabled_dir, f))
                if domain not in grouped_vhosts:
                    grouped_vhosts[domain] = {
                        'domain': domain, 
                        'servers': [], 
                        'enabled': enabled, 
                        'config_paths': {}, 
                        'has_ssl': False,
                        'doc_root': os.path.join(WWW_BASE, domain) # fallback
                    }
                if 'Apache' not in grouped_vhosts[domain]['servers']:
                    grouped_vhosts[domain]['servers'].append('Apache')
                config_key = 'Apache SSL' if is_ssl_conf else 'Apache'
                grouped_vhosts[domain]['config_paths'][config_key] = os.path.join(apache_dir, f)
                grouped_vhosts[domain]['enabled'] = grouped_vhosts[domain]['enabled'] or enabled
                
                # Extract DocumentRoot
                if not is_ssl_conf or 'doc_root' not in grouped_vhosts[domain]:
                    try:
                        with open(os.path.join(apache_dir, f), 'r') as conf:
                            for line in conf:
                                if 'DocumentRoot' in line:
                                    grouped_vhosts[domain]['doc_root'] = line.split('DocumentRoot')[1].strip().strip('"\'')
                                    break
                    except Exception:
                        pass

    # Check SSL
    le_live_dir = os.path.join(user_cleanup_mgr.LETSENCRYPT_DIR, 'live')
    for domain in grouped_vhosts:
        if os.path.exists(os.path.join(le_live_dir, domain, 'fullchain.pem')):
            grouped_vhosts[domain]['has_ssl'] = True

    # Next.js App Filtering
    # Remove any domains that contain the '# NEXTJS_APP' marker in any of their configs.
    domains_to_remove = []
    for domain, data in grouped_vhosts.items():
        for server, path in data['config_paths'].items():
            try:
                with open(path, 'r') as f:
                    if '# NEXTJS_APP' in f.read():
                        domains_to_remove.append(domain)
                        break
            except Exception:
                pass
                
    for domain in domains_to_remove:
        del grouped_vhosts[domain]

    user_db = get_users()
    main_domains_map = {}
    domain_owners = {}
    for uname, uinfo in user_db.items():
        if uinfo.get('main_domain'):
            main_domains_map[uinfo['main_domain']] = uname
            domain_owners[uinfo['main_domain']] = uname
        for dom in uinfo.get('domains', []):
            domain_owners.setdefault(dom, uname)

    for domain, vdata in grouped_vhosts.items():
        vdata['is_main_domain'] = domain in main_domains_map
        vdata['owner'] = domain_owners.get(domain)

    # Filter by user ownership if not admin
    if role == 'user' and username:
        user_info = user_db.get(username, {})
        
        # Collect both addon domains and the main domain
        user_domains = set(user_info.get('domains', []))
        if user_info.get('main_domain'):
            user_domains.add(user_info['main_domain'])
            
        return [v for k, v in grouped_vhosts.items() if k in user_domains]

    return list(grouped_vhosts.values())

def add_virtual_host(domain, role='admin', username=None, is_main=False):
    """
    Calls the existing vhost-manager.sh script to create a new virtual host.
    """
    # Try to find the script in the scripts directory
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'scripts', 'web-stack-installer.sh')

    if os.path.exists(script_path):
        try:
            # Check limits if user
            if role == 'user' and username and not is_main:
                if not can_add_resource(username, 'domains'):
                    return False, "Domain limit reached for your plan."
            
            # Determine doc_root
            doc_root = f"/var/www/{domain}"
            if role == 'user' and username:
                if is_main:
                    doc_root = f"/home/{username}/public_html"
                else:
                    doc_root = f"/home/{username}/public_html/{domain}"
                
            # Provide an explicit environment with guaranteed standard PATH to prevent 'command not found' errors
            env = os.environ.copy()
            env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
            
            # We assume it's run as root, so we just execute it directly
            result = subprocess.run(['/bin/bash', script_path, domain, doc_root],
                                  capture_output=True, text=True, check=True, env=env)
            
            # Track ownership if user (main domain is tracked separately during user creation, but adding it here if is_main=False is fine, or we just rely on calling code)
            if role == 'user' and username:
                if not is_main:
                    add_user_resource(username, 'domains', domain)
                subprocess.run(['chown', '-R', f"{username}:{username}", doc_root], capture_output=True)

            try:
                import dns_mgr
                dns_owner = username if (role == 'user' and username) else 'root'
                dns_mgr.ensure_zone_for_domain(domain, owner=dns_owner)
            except Exception as exc:
                import logging
                logging.warning("DNS auto-zone hook failed for %s: %s", domain, exc)

            return True, "Virtual host created successfully."
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else e.stdout.strip()
            return False, f"Error creating virtual host: {err_msg}"
    else:
        # Fallback if the script isn't found
        return False, "vhost-manager.sh script not found."

def toggle_virtual_host(domain, enable):
    """
    Enables or disables a virtual host across all installed web servers.
    """
    successes = []
    errors = []

    # Check Nginx
    nginx_avail = f'/etc/nginx/sites-available/{domain}'
    if os.path.exists(nginx_avail):
        try:
            enabled_link = f'/etc/nginx/sites-enabled/{domain}'
            if enable and not os.path.exists(enabled_link):
                os.symlink(nginx_avail, enabled_link)
            elif not enable and os.path.exists(enabled_link):
                os.remove(enabled_link)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            successes.append("Nginx")
        except Exception as e:
            errors.append(f"Nginx: {str(e)}")

    # Check Apache
    apache_avail = f'/etc/apache2/sites-available/{domain}.conf'
    if os.path.exists(apache_avail):
        try:
            cmd = ['a2ensite', f"{domain}.conf"] if enable else ['a2dissite', f"{domain}.conf"]
            subprocess.run(cmd, check=True, capture_output=True)
            subprocess.run(['systemctl', 'reload', 'apache2'], check=True)
            successes.append("Apache")
        except Exception as e:
            errors.append(f"Apache: {str(e)}")

    if errors:
        return False, f"Errors: {', '.join(errors)}"
    elif successes:
        return True, f"Domain {domain} {'enabled' if enable else 'disabled'} successfully on: {', '.join(successes)}."
    else:
        return False, "Configuration not found for any web server."

def get_port80_webserver(domain):
    """
    Checks the configuration files for a domain to see which webserver is serving port 80.
    Returns 'nginx', 'apache', or None.
    """
    # Check Nginx first (usually the frontend in hybrid stacks)
    nginx_conf = f'/etc/nginx/sites-available/{domain}'
    if os.path.exists(nginx_conf):
        try:
            with open(nginx_conf, 'r') as f:
                content = f.read()
                # Look for listen 80 or listen [::]:80
                if 'listen 80' in content or 'listen [::]:80' in content:
                    return 'nginx'
        except Exception:
            pass

    # Check Apache
    apache_conf = f'/etc/apache2/sites-available/{domain}.conf'
    if os.path.exists(apache_conf):
        try:
            with open(apache_conf, 'r') as f:
                content = f.read()
                # Look for <VirtualHost *:80>
                if '<VirtualHost *:80>' in content or '<VirtualHost _default_:80>' in content:
                    return 'apache'
        except Exception:
            pass

    return None


def _resolve_domain_doc_root(domain, owner=None):
    """Resolve the safe document root path for a non-main domain."""
    if owner:
        user_home = user_cleanup_mgr.resolve_safe_user_home(owner)
        public_html = os.path.abspath(os.path.join(user_home, 'public_html'))
        doc_root = os.path.abspath(os.path.join(public_html, domain))
        if os.path.dirname(doc_root) != public_html or os.path.basename(doc_root) != domain:
            raise ValueError(f"Unsafe domain directory path: {doc_root!r}")
        return doc_root

    www_base = os.path.abspath(WWW_BASE)
    doc_root = os.path.abspath(os.path.join(www_base, domain))
    if (
        os.path.dirname(doc_root) != www_base
        or os.path.basename(doc_root) != domain
        or domain.lower() == 'html'
    ):
        raise ValueError(f"Unsafe domain directory path: {doc_root!r}")
    return doc_root


def delete_virtual_host(domain, role='admin', username=None):
    """Delete a domain virtual host, SSL certs, logs, directory, and user tracking."""
    ok_domain, err_domain = validate_input(domain, 'domain')
    if not ok_domain or '/' in domain or '..' in domain or domain.endswith('-le-ssl'):
        return False, f"Invalid domain: {err_domain or 'unsupported target'}"

    if domain.lower() in user_cleanup_mgr.PROTECTED_VHOSTS:
        return False, f"Protected virtual host '{domain}' cannot be deleted."

    user_db = get_users()
    for uname, uinfo in user_db.items():
        if uinfo.get('main_domain') == domain:
            return (
                False,
                f"Cannot delete '{domain}': it is the primary main domain of user '{uname}'. Delete the user account from Manage Users instead.",
            )

    owner = get_owner_of_resource('domains', domain)
    if not owner:
        for uname, uinfo in user_db.items():
            if domain in uinfo.get('domains', []):
                owner = uname
                break

    if role == 'user':
        if not username or owner != username:
            return False, "Access denied: You do not own this domain."

    try:
        doc_root = _resolve_domain_doc_root(domain, owner=owner)
    except ValueError as exc:
        return False, str(exc)

    warnings = []
    servers_deleted, apache_touched, nginx_touched, ssl_deleted = user_cleanup_mgr.delete_domain_vhost_files(
        domain, warnings=warnings
    )
    user_cleanup_mgr.reload_touched_webservers(
        apache_touched=apache_touched, nginx_touched=nginx_touched, warnings=warnings
    )

    dir_deleted = False
    try:
        dir_deleted = user_cleanup_mgr.delete_path_safely(doc_root)
    except Exception as exc:
        warnings.append(f"Failed removing directory {doc_root}: {exc}")

    dns_deleted = False
    try:
        import dns_mgr
        ok_dns, msg_dns = dns_mgr.delete_dns_zone(domain, reload_bind=True)
        if ok_dns:
            dns_deleted = True
        elif msg_dns and "not found" not in msg_dns.lower():
            warnings.append(msg_dns)
    except Exception as exc:
        warnings.append(f"Failed removing DNS zone for {domain}: {exc}")

    if owner:
        remove_user_resource(owner, 'domains', domain)

    if not (servers_deleted or ssl_deleted or dir_deleted or dns_deleted or owner):
        if warnings:
            return False, f"Errors deleting domain: {'; '.join(warnings)}"
        return False, "Configuration not found for any web server."

    details = []
    if servers_deleted:
        details.append(f"web servers: {', '.join(servers_deleted)}")
    if dir_deleted:
        details.append(f"directory removed: {doc_root}")
    if ssl_deleted:
        details.append("SSL certificate removed")

    msg = f"Domain '{domain}' deleted successfully"
    if details:
        msg += f" ({'; '.join(details)})"
    msg += "."
    if warnings:
        msg += f" Warnings: {'; '.join(warnings)}"
    return True, msg
