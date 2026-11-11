import os
import subprocess
import re
from hosting_mgr import (
    can_add_resource,
    add_user_resource,
    remove_user_resource,
    get_users,
    get_user_data,
    get_owner_of_resource,
    release_nextjs_port,
)
from security_mgr import validate_input

WWW_BASE = '/var/www'

def get_nextjs_apps(role='admin', username=None):
    """
    Returns a list of dictionaries with Next.js proxy host information, grouped by domain.
    Checks both Nginx and Apache directories for the '# NEXTJS_APP' marker.
    """
    grouped_apps = {}

    # Check Nginx
    nginx_dir = '/etc/nginx/sites-available'
    if os.path.exists(nginx_dir):
        for f in os.listdir(nginx_dir):
            if f not in ['default', 'default-modsecurity.conf']:
                filepath = os.path.join(nginx_dir, f)
                try:
                    with open(filepath, 'r') as file:
                        content = file.read()
                        if '# NEXTJS_APP' in content:
                            enabled = os.path.exists(f'/etc/nginx/sites-enabled/{f}')
                            domain = f
                            
                            # Extract port
                            port_match = re.search(r'proxy_pass\s+http://(?:127\.0\.0\.1|localhost):(\d+)', content)
                            port = port_match.group(1) if port_match else "Unknown"

                            if domain not in grouped_apps:
                                grouped_apps[domain] = {
                                    'domain': domain, 
                                    'port': port,
                                    'servers': [], 
                                    'enabled': enabled, 
                                    'config_paths': {}, 
                                    'has_ssl': False
                                }
                            grouped_apps[domain]['servers'].append('Nginx')
                            grouped_apps[domain]['config_paths']['Nginx'] = filepath
                            grouped_apps[domain]['enabled'] = grouped_apps[domain]['enabled'] or enabled
                except Exception:
                    pass

    # Check Apache
    apache_dir = '/etc/apache2/sites-available'
    if os.path.exists(apache_dir):
        for f in os.listdir(apache_dir):
            if f not in ['000-default.conf', 'default-ssl.conf', 'default-modsecurity.conf']:
                filepath = os.path.join(apache_dir, f)
                try:
                    with open(filepath, 'r') as file:
                        content = file.read()
                        if '# NEXTJS_APP' in content:
                            domain = f.replace('.conf', '')
                            enabled = os.path.exists(f'/etc/apache2/sites-enabled/{f}')
                            
                            # Extract port
                            port_match = re.search(r'ProxyPass\s+/\s+http://(?:127\.0\.0\.1|localhost):(\d+)', content)
                            port = port_match.group(1) if port_match else "Unknown"

                            if domain not in grouped_apps:
                                grouped_apps[domain] = {
                                    'domain': domain, 
                                    'port': port,
                                    'servers': [], 
                                    'enabled': enabled, 
                                    'config_paths': {}, 
                                    'has_ssl': False
                                }
                            grouped_apps[domain]['servers'].append('Apache')
                            grouped_apps[domain]['config_paths']['Apache'] = filepath
                            grouped_apps[domain]['enabled'] = grouped_apps[domain]['enabled'] or enabled
                except Exception:
                    pass

    for domain in grouped_apps:
        if os.path.exists(f'/etc/letsencrypt/live/{domain}/fullchain.pem'):
            grouped_apps[domain]['has_ssl'] = True

    user_db = get_users()
    
    # Attach the correct path for each app
    for app in grouped_apps.values():
        domain = app['domain']
        app['path'] = f"/var/www/{domain}" # Default for admin apps
        
        # Determine if owned by a user
        for uname, udata in user_db.items():
            if domain in udata.get('nextjs_apps', []):
                if domain == udata.get('main_domain'):
                    app['path'] = f"/home/{uname}/public_html"
                else:
                    app['path'] = f"/home/{uname}/public_html/{domain}"
                break

    # Filter by user ownership if not admin
    if role == 'user' and username:
        user_apps = user_db.get(username, {}).get('nextjs_apps', [])
        return [v for k, v in grouped_apps.items() if k in user_apps]

    return list(grouped_apps.values())

def get_webserver_type():
    """Determine the active webserver setup based on installed services and config."""
    config_file = '/var/lib/lite-cpanel/.stack_config'
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                for line in f:
                    if line.startswith('WEBSERVER_TYPE='):
                        return line.split('=')[1].strip()
        except Exception:
            pass
    
    # Fallback to checking installed services
    has_nginx = os.path.exists('/etc/nginx/sites-available')
    has_apache = os.path.exists('/etc/apache2/sites-available')
    
    if has_nginx and has_apache:
        return 'hybrid'
    elif has_nginx:
        return 'nginx'
    elif has_apache:
        return 'apache'
    return 'none'

def create_apache_proxy(domain, port):
    site_file = f"/etc/apache2/sites-available/{domain}.conf"
    content = f"""# NEXTJS_APP
<VirtualHost *:80>
    ServerName {domain}
    ServerAlias www.{domain}
    
    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:{port}/
    ProxyPassReverse / http://127.0.0.1:{port}/

    ErrorLog ${{APACHE_LOG_DIR}}/{domain}_error.log
    CustomLog ${{APACHE_LOG_DIR}}/{domain}_access.log combined
</VirtualHost>
"""
    with open(site_file, 'w') as f:
        f.write(content)
    
    # Ensure proxy modules are enabled
    subprocess.run(['a2enmod', 'proxy', 'proxy_http'], check=False)
    subprocess.run(['a2ensite', f"{domain}.conf"], check=True)
    
def create_nginx_proxy(domain, port, is_hybrid=False):
    site_file = f"/etc/nginx/sites-available/{domain}"
    content = f"""# NEXTJS_APP
server {{
    listen 80;
    server_name {domain} www.{domain};

    access_log /var/log/nginx/{domain}_access.log;
    error_log  /var/log/nginx/{domain}_error.log;
    
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_cache_bypass $http_upgrade;
    }}
}}
"""
    with open(site_file, 'w') as f:
        f.write(content)
    
    enabled_link = f"/etc/nginx/sites-enabled/{domain}"
    if not os.path.exists(enabled_link):
        os.symlink(site_file, enabled_link)

def create_hybrid_proxy(domain, port):
    # In hybrid, we could just let Nginx handle the proxy directly to PM2 
    # to avoid double proxy overhead (Nginx -> Apache -> PM2).
    # Since Apache isn't serving PHP here, proxying direct from Nginx to PM2 is optimal.
    create_nginx_proxy(domain, port, is_hybrid=True)

def add_nextjs_app(domain, port, role='admin', username=None):
    """Creates a reverse proxy for a Next.js app on the specified port."""
    if role == 'user' and username:
        if not can_add_resource(username, 'nextjs_apps'):
            return False, "Next.js App limit reached for your plan."
            
    webserver = get_webserver_type()
    
    if webserver == 'none':
        return False, "No supported webserver found to configure proxy."
        
    try:
        if webserver == 'apache':
            create_apache_proxy(domain, port)
            subprocess.run(['systemctl', 'reload', 'apache2'], check=True)
        elif webserver == 'nginx':
            create_nginx_proxy(domain, port)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        elif webserver == 'hybrid':
            create_hybrid_proxy(domain, port)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            
        if role == 'user' and username:
            add_user_resource(username, 'nextjs_apps', domain)
            
            # Create document root directory automatically
            from hosting_mgr import get_user_data
            user_data = get_user_data(username)
            if user_data and domain == user_data.get('main_domain'):
                doc_root = f"/home/{username}/public_html"
            else:
                doc_root = f"/home/{username}/public_html/{domain}"
                
            if not os.path.exists(doc_root):
                os.makedirs(doc_root, exist_ok=True)
                subprocess.run(['chown', '-R', f"{username}:{username}", doc_root], capture_output=True)
        else:
            doc_root = f"/var/www/{domain}"
            if not os.path.exists(doc_root):
                os.makedirs(doc_root, exist_ok=True)
            
        try:
            import dns_mgr
            dns_owner = username if (role == 'user' and username) else 'root'
            dns_mgr.ensure_zone_for_domain(domain, owner=dns_owner)
        except Exception as exc:
            import logging
            logging.warning("DNS auto-zone hook failed for Next.js app %s: %s", domain, exc)

        return True, f"Next.js proxy for {domain} created successfully."
    except Exception as e:
        return False, f"Failed to create Next.js proxy: {str(e)}"

def toggle_nextjs_app(domain, enable):
    """Enables or disables a Next.js proxy virtual host."""
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
        return True, f"Next.js App {domain} {'enabled' if enable else 'disabled'} successfully on: {', '.join(successes)}."
    else:
        return False, "Configuration not found for any web server."

def _resolve_nextjs_doc_root(domain, owner=None):
    """Resolve the safe document root directory and whether it is a protected main web root."""
    if owner:
        from user_cleanup_mgr import resolve_safe_user_home

        user_home = resolve_safe_user_home(owner)
        public_html = os.path.abspath(os.path.join(user_home, "public_html"))
        user_data = get_user_data(owner)
        if user_data and domain == user_data.get("main_domain"):
            return public_html, True

        doc_root = os.path.abspath(os.path.join(public_html, domain))
        if os.path.dirname(doc_root) != public_html or os.path.basename(doc_root) != domain:
            raise ValueError(f"Unsafe Next.js app directory path: {doc_root!r}")
        return doc_root, False

    www_base = os.path.abspath(WWW_BASE)
    doc_root = os.path.abspath(os.path.join(www_base, domain))
    if (
        os.path.dirname(doc_root) != www_base
        or os.path.basename(doc_root) != domain
        or domain.lower() == "html"
    ):
        raise ValueError(f"Unsafe Next.js app directory path: {doc_root!r}")
    return doc_root, False


def delete_nextjs_app(domain, role='admin', username=None):
    """Delete a Next.js proxy, its PM2 process, allocated port, and app directory."""
    from user_cleanup_mgr import (
        _is_path_within,
        delete_domain_vhost_files,
        delete_path_safely,
        delete_pm2_processes,
        reload_touched_webservers,
    )

    ok_domain, err_domain = validate_input(domain, 'domain')
    if not ok_domain or '/' in domain or '..' in domain:
        return False, f"Invalid domain: {err_domain}"

    owner = get_owner_of_resource('nextjs_apps', domain)
    if role == 'user':
        if not username or owner != username:
            return False, "Access denied: You do not own this Next.js app."

    try:
        doc_root, is_main_domain = _resolve_nextjs_doc_root(domain, owner=owner)
    except ValueError as exc:
        return False, str(exc)

    warnings = []

    # 1. Stop & delete matching PM2 process(es) before removing directory
    def _matches_nextjs_pm2(proc):
        name = proc.get("name", "") or ""
        if name == domain:
            return True
        if is_main_domain:
            return False
        pm2_env = proc.get("pm2_env") or {}
        return any(
            _is_path_within(p, doc_root)
            for p in (pm2_env.get("pm_cwd"), pm2_env.get("cwd"), pm2_env.get("pm_exec_path"))
            if p
        )

    pm2_deleted = delete_pm2_processes(_matches_nextjs_pm2, warnings=warnings)

    # 2. Delete Nginx/Apache vhosts, SSL certs, and logs, reloading modified services once
    servers_deleted, apache_touched, nginx_touched, ssl_deleted = delete_domain_vhost_files(
        domain, warnings=warnings
    )
    reload_touched_webservers(
        apache_touched=apache_touched, nginx_touched=nginx_touched, warnings=warnings
    )

    # 3. Delete app directory (never delete /home/<user>/public_html for a user's main domain)
    dir_deleted = False
    if not is_main_domain:
        try:
            dir_deleted = delete_path_safely(doc_root)
        except Exception as exc:
            warnings.append(f"Failed removing directory {doc_root}: {exc}")

    # 4. Clean hosting tracking & port allocation
    is_main_domain = False
    still_used_by_owner = False
    if owner:
        try:
            from hosting_mgr import get_user_data
            owner_data = get_user_data(owner) or {}
            is_main_domain = owner_data.get('main_domain') == domain
            still_used_by_owner = domain in (owner_data.get('domains') or []) or domain in (owner_data.get('docker_apps') or [])
        except Exception:
            pass
        remove_user_resource(owner, 'nextjs_apps', domain)
    release_nextjs_port(domain)

    dns_deleted = False
    if (servers_deleted or ssl_deleted or pm2_deleted or dir_deleted or owner) and not is_main_domain and not still_used_by_owner:
        try:
            import dns_mgr
            ok_dns, msg_dns = dns_mgr.delete_dns_zone(domain, reload_bind=True)
            if ok_dns:
                dns_deleted = True
            elif msg_dns and "not found" not in msg_dns.lower():
                warnings.append(msg_dns)
        except Exception as exc:
            warnings.append(f"Failed removing DNS zone for {domain}: {exc}")

    if not (servers_deleted or ssl_deleted or pm2_deleted or dir_deleted or dns_deleted or owner):
        if warnings:
            return False, f"Errors deleting Next.js app: {'; '.join(warnings)}"
        return False, "Configuration not found for any web server."

    details = []
    if servers_deleted:
        details.append(f"web servers: {', '.join(servers_deleted)}")
    if dir_deleted:
        details.append(f"directory removed: {doc_root}")
    elif is_main_domain:
        details.append(f"main web root preserved: {doc_root}")
    if pm2_deleted:
        details.append(f"PM2 deleted: {', '.join(pm2_deleted)}")
    if ssl_deleted:
        details.append("SSL certificate removed")

    msg = f"Next.js app for {domain} deleted successfully"
    if details:
        msg += f" ({'; '.join(details)})"
    msg += "."
    if warnings:
        msg += f" Warnings: {'; '.join(warnings)}"
    return True, msg
