import os
import subprocess

import glob

def get_system_logs():
    """
    Returns a dictionary of available common system logs, including vhosts, and their recent contents.
    """
    logs = {
        'Apache Error': '/var/log/apache2/error.log',
        'Apache Access': '/var/log/apache2/access.log',
        'Nginx Error': '/var/log/nginx/error.log',
        'Nginx Access': '/var/log/nginx/access.log',
        'Syslog': '/var/log/syslog',
        'MySQL Error': '/var/log/mysql/error.log',
        'Backup Log': '/var/log/lite-cpanel-backup.log',
        'cPanel Auth': '/var/log/cpanel_auth.log',
        'Mongo Express': '/var/log/mongo-express.log',
        'Letsencrypt': '/var/log/letsencrypt/letsencrypt.log'
    }

    # Alternative paths for some services
    alt_paths = {
        'MySQL Error': ['/var/log/mariadb/mariadb.log', '/var/log/mysql/mariadb.log']
    }

    # Automatically add Apache vhost logs
    for log_path in glob.glob('/var/log/apache2/*-error.log'):
        name = os.path.basename(log_path).replace('-error.log', ' (Apache Error)')
        logs[name] = log_path

    for log_path in glob.glob('/var/log/apache2/*-access.log'):
        name = os.path.basename(log_path).replace('-access.log', ' (Apache Access)')
        logs[name] = log_path

    # Automatically add Nginx vhost logs
    for log_path in glob.glob('/var/log/nginx/*-error.log'):
        name = os.path.basename(log_path).replace('-error.log', ' (Nginx Error)')
        logs[name] = log_path

    results = {}
    core_log_names = ['Apache Error', 'Apache Access', 'Nginx Error', 'Nginx Access', 'Syslog', 'MySQL Error', 'Backup Log', 'Mongo Express', 'Letsencrypt']

    for name, path in logs.items():
        actual_path = path
        exists = os.path.exists(path)
        
        # Try alternatives if not found
        if not exists and name in alt_paths:
            for alt in alt_paths[name]:
                if os.path.exists(alt):
                    actual_path = alt
                    exists = True
                    break
        
        if exists:
            try:
                # Get last 100 lines
                result = subprocess.run(['tail', '-n', '100', actual_path], capture_output=True, text=True)
                if result.returncode == 0:
                    results[name] = {
                        'path': actual_path,
                        'content': result.stdout if result.stdout else "(Log file is empty)"
                    }
                else:
                    results[name] = {
                        'path': actual_path,
                        'content': f"Error reading log: {result.stderr}"
                    }
            except Exception as e:
                results[name] = {'path': actual_path, 'content': f"Error reading log: {str(e)}"}
        elif name in core_log_names:
            # Always show core logs even if missing
            results[name] = {
                'path': actual_path,
                'content': "Log file does not exist yet. This service may not have generated any logs, or the feature is not in use."
            }

    return results

def get_editable_configs():
    """
    Returns a list of common configuration files, including vhosts, that can be edited.
    """
    configs = []
    potential_configs = [
        ('/etc/apache2/apache2.conf', 'Main Apache config'),
        ('/etc/nginx/nginx.conf', 'Main Nginx config'),
        ('/etc/mysql/mariadb.conf.d/50-server.cnf', 'MariaDB Server config'),
        ('/etc/pure-ftpd/pure-ftpd.conf', 'Pure-FTPd config')
    ]

    # Dynamically detect PHP versions and configs (FPM and Apache)
    php_paths = glob.glob('/etc/php/*/*/php.ini')
    for path in php_paths:
        # Path format: /etc/php/8.1/fpm/php.ini or /etc/php/8.1/apache2/php.ini
        parts = path.split('/')
        if len(parts) >= 5:
            version = parts[3]
            variant = parts[4].upper()
            potential_configs.append((path, f"PHP {version} {variant} config"))

    for path, name in potential_configs:
        if os.path.exists(path):
            configs.append({'path': path, 'name': name})

    return configs

def read_config_file(filepath):
    # Security check: only allow reading specific known files to prevent arbitrary file read
    valid_paths = [c['path'] for c in get_editable_configs()]
    if filepath not in valid_paths:
        return False, "File is not in the allowed list for editing."

    try:
        with open(filepath, 'r') as f:
            return True, f.read()
    except Exception as e:
        return False, str(e)

def save_config_file(filepath, content):
    # Security check: only allow writing specific known files
    valid_paths = [c['path'] for c in get_editable_configs()]
    if filepath not in valid_paths:
        return False, "File is not in the allowed list for editing."

    try:
        with open(filepath, 'w') as f:
            f.write(content)

        # Try to reload the relevant service based on the file edited
        if 'apache2' in filepath:
            subprocess.run(['systemctl', 'reload', 'apache2'])
        elif 'nginx' in filepath:
            subprocess.run(['systemctl', 'reload', 'nginx'])
        elif 'mysql' in filepath or 'mariadb' in filepath:
            subprocess.run(['systemctl', 'reload', 'mariadb'])
        elif 'php' in filepath and 'fpm' in filepath:
            # Extract version from path /etc/php/8.1/fpm/php.ini
            parts = filepath.split('/')
            if len(parts) >= 4:
                version = parts[3]
                subprocess.run(['systemctl', 'reload', f'php{version}-fpm'])

        return True, "File saved successfully and service reloaded."
    except Exception as e:
        return False, str(e)

def set_server_hostname(new_hostname):
    """Updates the system hostname."""
    try:
        # 1. Update /etc/hostname
        with open('/etc/hostname', 'w') as f:
            f.write(new_hostname + '\n')
        
        # 2. Update /etc/hosts (replace old hostname with new one)
        old_hostname = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
        with open('/etc/hosts', 'r') as f:
            hosts_content = f.read()
        
        # Try to find a line with the old hostname and replace it, or add a new one for 127.0.1.1
        if old_hostname in hosts_content:
            new_hosts_content = hosts_content.replace(old_hostname, new_hostname)
        else:
            new_hosts_content = hosts_content + f"\n127.0.1.1 {new_hostname}\n"
            
        with open('/etc/hosts', 'w') as f:
            f.write(new_hosts_content)
        
        # 3. Apply via hostnamectl
        subprocess.run(['hostnamectl', 'set-hostname', new_hostname], check=True)
        
        return True, f"Hostname updated to {new_hostname}. You may need to reconnect your SSH session."
    except Exception as e:
        return False, str(e)

def generate_hostname_ssl(hostname):
    """Uses certbot to generate SSL for the panel hostname."""
    try:
        # Ensure certbot is installed
        subprocess.run(['apt-get', 'update'], capture_output=True)
        subprocess.run(['apt-get', 'install', '-y', 'certbot', 'python3-certbot-nginx'], capture_output=True)
        
        # Run certbot in standalone mode (panel port 2083 is different from 80)
        # We assume port 80 is available or managed by Nginx
        cmd = [
            'certbot', 'certonly', '--nginx', 
            '-d', hostname, 
            '--non-interactive', '--agree-tos', 
            '--register-unsafely-without-email'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            return True, f"SSL Certificate generated for {hostname}."
        else:
            return False, f"Certbot failed: {result.stderr}"
    except Exception as e:
        return False, str(e)

def enable_panel_ssl(hostname):
    """Configures the Gunicorn service to use the generated SSL certificates."""
    try:
        cert_path = f"/etc/letsencrypt/live/{hostname}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{hostname}/privkey.pem"
        
        if not os.path.exists(cert_path) or not os.path.exists(key_path):
            return False, "SSL certificates not found. Generate them first."
        
        service_file = "/etc/systemd/system/cpanel.service"
        if not os.path.exists(service_file):
            return False, f"Service file {service_file} not found. Ensure the panel is running as a systemd service."
        
        with open(service_file, 'r') as f:
            lines = f.readlines()
        
        new_lines = []
        for line in lines:
            if 'ExecStart=' in line and 'gunicorn' in line:
                # Add SSL flags if not present
                if '--certfile' not in line:
                    # Insert SSL flags before the app name (usually at the end)
                    parts = line.strip().split()
                    app_name = parts[-1]
                    ssl_flags = f"--certfile={cert_path} --keyfile={key_path}"
                    new_line = " ".join(parts[:-1]) + f" {ssl_flags} {app_name}\n"
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        
        with open(service_file, 'w') as f:
            f.writelines(new_lines)
        
        # Reload systemd
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        return True, "Panel configured for SSL. Please restart the 'cpanel' service manually to apply, then access via https."
    except Exception as e:
        return False, str(e)
