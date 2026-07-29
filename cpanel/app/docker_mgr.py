import os
import subprocess
import json
import re
import shutil
from hosting_mgr import can_add_resource, add_user_resource, get_users

def is_docker_installed():
    """Checks if docker is available in the system path."""
    return shutil.which('docker') is not None

def list_containers():
    """Returns a list of all Docker containers."""
    if not is_docker_installed():
        return []
    
    try:
        # We use a custom format to easily parse the output
        cmd = ['docker', 'ps', '-a', '--format', '{{json .}}']
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        containers = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return containers
    except subprocess.CalledProcessError:
        return []

def manage_container(action, container_id):
    """Starts, stops, restarts, or deletes a container."""
    if action not in ['start', 'stop', 'restart', 'rm']:
        return False, "Invalid action."
        
    try:
        cmd = ['docker', action]
        if action == 'rm':
            cmd.append('-f') # Force remove
        cmd.append(str(container_id))
        
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, f"Container {action}ed successfully."
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else e.stdout.strip()
        return False, f"Error: {err_msg}"

def run_container(image, name, port_mapping="", env_vars=""):
    """Starts a new docker container from an image."""
    if not is_docker_installed():
        return False, "Docker is not installed."
        
    try:
        cmd = ['docker', 'run', '-d']
        
        if name:
            cmd.extend(['--name', name])
            
        if port_mapping:
            # allow multiple port mappings, comma-separated (e.g. "8080:80,443:443")
            for p in port_mapping.split(','):
                cmd.extend(['-p', p.strip()])
                
        if env_vars:
            # allow multiple env vars, comma-separated (e.g. "MYSQL_ROOT_PASSWORD=pass,MYSQL_DATABASE=db")
            for e in env_vars.split(','):
                cmd.extend(['-e', e.strip()])
                
        cmd.append(image)
        
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, f"Container '{name or image}' started successfully."
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else e.stdout.strip()
        return False, f"Failed to run container: {err_msg}"

# --- Domain Proxy Logic for Docker ---

def get_docker_apps(role='admin', username=None):
    """
    Returns a list of dictionaries with Docker proxy host information, grouped by domain.
    Checks both Nginx and Apache directories for the '# DOCKER_APP' marker.
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
                        if '# DOCKER_APP' in content:
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
                        if '# DOCKER_APP' in content:
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

    # Check SSL
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
            if domain in udata.get('docker_apps', []):
                if domain == udata.get('main_domain'):
                    app['path'] = f"/home/{uname}/public_html"
                else:
                    app['path'] = f"/home/{uname}/public_html/{domain}"
                break

    # Filter by user ownership if not admin
    if role == 'user' and username:
        user_apps = user_db.get(username, {}).get('docker_apps', [])
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
    
    # Fallback
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
    content = f"""# DOCKER_APP
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
    
    subprocess.run(['a2ensite', f"{domain}.conf"], check=True)

def create_nginx_proxy(domain, port, is_hybrid=False):
    site_file = f"/etc/nginx/sites-available/{domain}"
    content = f"""# DOCKER_APP
server {{
    listen 80;
    server_name {domain} www.{domain};

    access_log /var/log/nginx/{domain}_docker_access.log;
    error_log  /var/log/nginx/{domain}_docker_error.log;
    
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
    create_nginx_proxy(domain, port, is_hybrid=True)

def add_docker_app(domain, port, role='admin', username=None):
    """Creates a reverse proxy for a Docker app on the specified port and sets up a document root."""
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
            add_user_resource(username, 'docker_apps', domain)
            
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
            
        return True, f"Docker proxy for {domain} created successfully."
    except Exception as e:
        return False, f"Failed to create Docker proxy: {str(e)}"

def toggle_docker_app(domain, enable):
    """Enables or disables a Docker proxy virtual host."""
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
        return True, f"Docker App {domain} {'enabled' if enable else 'disabled'} successfully on: {', '.join(successes)}."
    else:
        return False, "Configuration not found for any web server."

def delete_docker_app(domain, role='admin', username=None):
    """Deletes a Docker proxy virtual host."""
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
        return False, f"Errors deleting Docker app: {', '.join(errors)}"
    elif deleted:
        doc_root = f'/var/www/{domain}'
        if role == 'user' and username:
            from hosting_mgr import get_user_data
            user_data = get_user_data(username)
            if user_data and domain == user_data.get('main_domain'):
                # We don't completely delete the main domain's doc_root (public_html)
                pass
            else:
                doc_root = f"/home/{username}/public_html/{domain}"
                if os.path.exists(doc_root):
                    shutil.rmtree(doc_root, ignore_errors=True)
        else:
            if os.path.exists(doc_root):
                shutil.rmtree(doc_root, ignore_errors=True)
        return True, f"Docker proxy for {domain} deleted successfully from: {', '.join(deleted)}."
    else:
        return False, "Configuration not found for any web server."

def run_docker_compose(domain, role='admin', username=None):
    """
    Checks for a docker-compose.yml in the domain's document root
    and runs docker compose up -d --build in the background.
    """
    doc_root = f'/var/www/{domain}'
    if role == 'user' and username:
        from hosting_mgr import get_user_data
        user_data = get_user_data(username)
        if user_data and domain == user_data.get('main_domain'):
            doc_root = f"/home/{username}/public_html"
        else:
            doc_root = f"/home/{username}/public_html/{domain}"
            
    if not os.path.exists(doc_root):
        return False, f"Document root {doc_root} does not exist."
        
    yml_path = os.path.join(doc_root, 'docker-compose.yml')
    yaml_path = os.path.join(doc_root, 'docker-compose.yaml')
    
    if not os.path.exists(yml_path) and not os.path.exists(yaml_path):
        return False, "No docker-compose.yml or docker-compose.yaml found in the document root."
        
    log_file = f'/var/log/docker_compose_{domain}.log'
    
    try:
        # We use a shell command to allow redirection easily and start in background
        cmd = f"cd {doc_root} && (docker compose up -d --build > {log_file} 2>&1 &)"
        subprocess.Popen(cmd, shell=True)
        return True, f"Docker compose build started in background. Logs are written to {log_file}"
    except Exception as e:
        return False, f"Failed to run docker compose: {str(e)}"
