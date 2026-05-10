import os
import subprocess
from hosting_mgr import can_add_resource, add_user_resource, get_users, get_owner_of_resource

def get_virtual_hosts(role='admin', username=None):
    """
    Returns a list of dictionaries with virtual host information, grouped by domain.
    Checks both Nginx and Apache directories.
    """
    grouped_vhosts = {}

    nginx_dir = '/etc/nginx/sites-available'
    if os.path.exists(nginx_dir):
        for f in os.listdir(nginx_dir):
            if f not in ['default', 'default-modsecurity.conf']:
                enabled = os.path.exists(f'/etc/nginx/sites-enabled/{f}')
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
    apache_dir = '/etc/apache2/sites-available'
    if os.path.exists(apache_dir):
        for f in os.listdir(apache_dir):
            if f not in ['000-default.conf', 'default-ssl.conf', 'default-modsecurity.conf']:
                domain = f.replace('.conf', '')
                enabled = os.path.exists(f'/etc/apache2/sites-enabled/{f}')
                if domain not in grouped_vhosts:
                    grouped_vhosts[domain] = {
                        'domain': domain, 
                        'servers': [], 
                        'enabled': enabled, 
                        'config_paths': {}, 
                        'has_ssl': False
                    }
                grouped_vhosts[domain]['servers'].append('Apache')
                grouped_vhosts[domain]['config_paths']['Apache'] = os.path.join(apache_dir, f)
                grouped_vhosts[domain]['enabled'] = grouped_vhosts[domain]['enabled'] or enabled

    # Check SSL
    for domain in grouped_vhosts:
        if os.path.exists(f'/etc/letsencrypt/live/{domain}/fullchain.pem'):
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

    # Filter by user ownership if not admin
    if role == 'user' and username:
        user_db = get_users()
        user_domains = user_db.get(username, {}).get('domains', [])
        return [v for k, v in grouped_vhosts.items() if k in user_domains]

    return list(grouped_vhosts.values())

def add_virtual_host(domain, role='admin', username=None):
    """
    Calls the existing vhost-manager.sh script to create a new virtual host.
    """
    # Try to find the script in the scripts directory
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'scripts', 'web-stack-installer.sh')

    if os.path.exists(script_path):
        try:
            # Check limits if user
            if role == 'user' and username:
                if not can_add_resource(username, 'domains'):
                    return False, "Domain limit reached for your plan."
            
            # Determine doc_root
            doc_root = f"/var/www/{domain}"
            if role == 'user' and username:
                doc_root = f"/home/{username}/public_html/{domain}"
                
            # Provide an explicit environment with guaranteed standard PATH to prevent 'command not found' errors
            env = os.environ.copy()
            env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
            
            # We assume it's run as root, so we just execute it directly
            result = subprocess.run(['/bin/bash', script_path, domain, doc_root],
                                  capture_output=True, text=True, check=True, env=env)
            
            # Track ownership if user
            if role == 'user' and username:
                add_user_resource(username, 'domains', domain)
                subprocess.run(['chown', '-R', f"{username}:{username}", doc_root], capture_output=True)
                
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
