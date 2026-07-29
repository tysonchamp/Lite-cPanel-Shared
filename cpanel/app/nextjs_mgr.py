import os
import subprocess
import re
from hosting_mgr import can_add_resource, add_user_resource, get_users

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

def delete_nextjs_app(domain):
    """Deletes a Next.js proxy virtual host."""
    deleted = []
    errors = []

    # Check Nginx
    nginx_avail = f'/etc/nginx/sites-available/{domain}'
    nginx_enabled = f'/etc/nginx/sites-enabled/{domain}'
    if os.path.exists(nginx_avail):
        try:
            if os.path.exists(nginx_enabled):
                os.remove(nginx_enabled)
            os.remove(nginx_avail)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            deleted.append("Nginx")
        except Exception as e:
            errors.append(f"Nginx: {str(e)}")

    # Check Apache
    apache_avail = f'/etc/apache2/sites-available/{domain}.conf'
    if os.path.exists(apache_avail):
        try:
            subprocess.run(['a2dissite', f"{domain}.conf"], check=True, capture_output=True)
            os.remove(apache_avail)
            subprocess.run(['systemctl', 'reload', 'apache2'], check=True)
            deleted.append("Apache")
        except Exception as e:
            errors.append(f"Apache: {str(e)}")

    if errors:
        return False, f"Errors deleting Next.js app: {', '.join(errors)}"
    elif deleted:
        return True, f"Next.js proxy for {domain} deleted successfully from: {', '.join(deleted)}."
    else:
        return False, "Configuration not found for any web server."
