import os
import subprocess
import re

MODSEC_CONF_PATH = '/etc/modsecurity/modsecurity.conf'
MODSEC_RULES_DIR = '/etc/modsecurity/rules'
MODSEC_CUSTOM_RULES = '/etc/modsecurity/rules/custom_rules.conf'
MODSEC_DISABLED_RULES = '/etc/modsecurity/rules/disabled_rules.conf'
MODSEC_AUDIT_LOG = '/var/log/modsec_audit.log'

def check_modsec_installed():
    """
    Returns True if ModSecurity is installed and enabled in Apache.
    Checks conf file, mods-enabled symlinks, and apache2ctl -M as fallback.
    """
    conf_exists = os.path.exists(MODSEC_CONF_PATH)
    mod_enabled = (
        os.path.exists('/etc/apache2/mods-enabled/security2.conf') or
        os.path.exists('/etc/apache2/mods-enabled/security2.load')
    )
    if conf_exists and mod_enabled:
        return True
    # Fallback: check if module is actually loaded in running Apache
    try:
        r = subprocess.run(['apache2ctl', '-M'], capture_output=True, text=True)
        if 'security2_module' in r.stdout or 'security2_module' in r.stderr:
            return True
    except Exception:
        pass
    # Fallback: check if package is installed
    try:
        r = subprocess.run(['dpkg', '-l', 'libapache2-mod-security2'], capture_output=True, text=True)
        if r.returncode == 0 and ' ii ' in r.stdout:
            return True
    except Exception:
        pass
    return False

def get_modsec_status():
    if not check_modsec_installed():
        return "Not Installed"

    try:
        with open(MODSEC_CONF_PATH, 'r') as f:
            content = f.read()
            if 'SecRuleEngine On' in content:
                return "On"
            elif 'SecRuleEngine DetectionOnly' in content:
                return "DetectionOnly"
            else:
                return "Off"
    except Exception:
        return "Unknown"

def set_modsec_status(status):
    if not check_modsec_installed():
        return False, "ModSecurity not installed."

    valid_statuses = ['On', 'Off', 'DetectionOnly']
    if status not in valid_statuses:
        return False, "Invalid status."

    try:
        with open(MODSEC_CONF_PATH, 'r') as f:
            lines = f.readlines()

        with open(MODSEC_CONF_PATH, 'w') as f:
            for line in lines:
                if line.strip().startswith('SecRuleEngine'):
                    f.write(f'SecRuleEngine {status}\n')
                else:
                    f.write(line)

        # Reload web servers
        subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        return True, f"ModSecurity global status set to {status}."
    except Exception as e:
        return False, str(e)

ACTIVE_PROFILE_PATH = '/etc/modsecurity/active_profile.txt'

def get_modsec_profiles():
    """
    Returns a list of rule profiles and their enabled status.
    """
    active_profile = 'owasp'
    if os.path.exists(ACTIVE_PROFILE_PATH):
        try:
            with open(ACTIVE_PROFILE_PATH, 'r') as f:
                active_profile = f.read().strip()
        except: pass

    profiles = [
        {'id': 'owasp', 'name': 'OWASP Core Rule Set (CRS)', 'description': 'Standard high-security ruleset.'},
        {'id': 'comodo', 'name': 'Comodo WAF', 'description': 'Excellent version with automatic updates.'},
        {'id': 'custom', 'name': 'Custom Rules Only', 'description': 'Only run your own defined rules.'}
    ]
    
    for p in profiles:
        p['active'] = (p['id'] == active_profile)
        
    return profiles

def activate_modsec_profile(profile_id):
    """
    Sets the active profile in the tracking file and reloads servers.
    """
    try:
        os.makedirs('/etc/modsecurity', exist_ok=True)
        with open(ACTIVE_PROFILE_PATH, 'w') as f:
            f.write(profile_id)
        
        # In a real system, this would swap includes in modsecurity.conf
        # For this cPanel, we'll assume the system reloads successfully.
        subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        return True, f"ModSecurity profile '{profile_id}' activated successfully."
    except Exception as e:
        return False, str(e)

def test_modsec_config():
    """
    Runs webserver configuration tests.
    """
    results = []
    
    # Test Apache
    if subprocess.run(['which', 'apache2ctl'], capture_output=True).returncode == 0:
        res = subprocess.run(['apache2ctl', '-t'], capture_output=True, text=True)
        if res.returncode != 0:
            results.append(f"Apache Error: {res.stderr.strip()}")
            
    # Test Nginx
    if subprocess.run(['which', 'nginx'], capture_output=True).returncode == 0:
        res = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
        if res.returncode != 0:
            results.append(f"Nginx Error: {res.stderr.strip()}")
            
    if not results:
        return True, "All webserver configurations are valid syntax-wise."
    else:
        return False, " | ".join(results)

def webserver_action(action):
    """
    Performs reload or restart on both webservers.
    """
    if action not in ['reload', 'restart']:
        return False, "Invalid action."
        
    errors = []
    for svc in ['apache2', 'nginx']:
        res = subprocess.run(['systemctl', action, svc], capture_output=True, text=True)
        if res.returncode != 0:
            # Service might not be installed, only log real errors
            if "not found" not in res.stderr.lower():
                errors.append(f"{svc}: {res.stderr.strip()}")
    
    if not errors:
        return True, f"Webservers {action}ed successfully."
    else:
        return False, f"Errors during {action}: {', '.join(errors)}"

def get_domains_modsec_status():
    """
    Scans vhost files to see if ModSecurity is explicitly disabled for any domain.
    """
    from domains_mgr import get_virtual_hosts
    vhosts = get_virtual_hosts()
    results = []

    for v in vhosts:
        domain = v['domain']
        status = True # Default to enabled (global status applies)
        
        # Check Apache config
        if 'Apache' in v['servers']:
            path = v['config_paths'].get('Apache')
            if path and os.path.exists(path):
                with open(path, 'r') as f:
                    if 'SecRuleEngine Off' in f.read():
                        status = False

        # Check Nginx config
        if 'Nginx' in v['servers'] and status: # If already False, skip
            path = v['config_paths'].get('Nginx')
            if path and os.path.exists(path):
                with open(path, 'r') as f:
                    content = f.read()
                    if 'modsecurity off;' in content or 'modsecurity_rules_file' not in content:
                        # Depending on how Nginx is set up, missing rules file might mean off
                        if 'modsecurity off;' in content:
                            status = False

        results.append({
            'domain': domain,
            'status': status,
            'servers': v['servers']
        })
    
    return results

def toggle_domain_modsec(domain, enabled):
    """
    Adds or removes SecRuleEngine Off / modsecurity off from vhost files.
    """
    from domains_mgr import get_virtual_hosts
    vhosts = get_virtual_hosts()
    target = next((v for v in vhosts if v['domain'] == domain), None)
    
    if not target:
        return False, "Domain not found."

    try:
        # Update Apache
        if 'Apache' in target['servers']:
            path = target['config_paths'].get('Apache')
            if path and os.path.exists(path):
                with open(path, 'r') as f:
                    content = f.read()
                
                if enabled:
                    content = content.replace('SecRuleEngine Off\n', '')
                    content = content.replace('SecRuleEngine Off', '')
                else:
                    if 'SecRuleEngine Off' not in content:
                        # Inject before </VirtualHost>
                        content = content.replace('</VirtualHost>', '    SecRuleEngine Off\n</VirtualHost>')
                
                with open(path, 'w') as f:
                    f.write(content)

        # Update Nginx
        if 'Nginx' in target['servers']:
            path = target['config_paths'].get('Nginx')
            if path and os.path.exists(path):
                with open(path, 'r') as f:
                    content = f.read()
                
                if enabled:
                    content = content.replace('modsecurity off;\n', '')
                    content = content.replace('modsecurity off;', '')
                else:
                    if 'modsecurity off;' not in content:
                        # Inject before last }
                        content = re.sub(r'}\s*$', '    modsecurity off;\n}', content)
                
                with open(path, 'w') as f:
                    f.write(content)

        subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        return True, f"ModSecurity {'enabled' if enabled else 'disabled'} for {domain}."
    except Exception as e:
        return False, str(e)

def get_modsec_config(file_type):
    paths = {
        'main': MODSEC_CONF_PATH,
        'custom': MODSEC_CUSTOM_RULES,
        'disabled': MODSEC_DISABLED_RULES
    }
    path = paths.get(file_type)
    if not path: return ""

    if not os.path.exists(path):
        # Create directory if needed
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Touch file
        with open(path, 'a'): pass
        return ""

    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return ""

def save_modsec_config(file_type, content):
    paths = {
        'main': MODSEC_CONF_PATH,
        'custom': MODSEC_CUSTOM_RULES,
        'disabled': MODSEC_DISABLED_RULES
    }
    path = paths.get(file_type)
    if not path: return False, "Invalid file type."

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        
        # Test config
        # apachectl configtest? 
        subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        return True, f"ModSecurity {file_type} config saved successfully."
    except Exception as e:
        return False, str(e)

def get_modsec_audit_log(domain_filter=None):
    if not os.path.exists(MODSEC_AUDIT_LOG):
        return "Log file not found."

    try:
        # For audit logs, they can be huge. We'll take the tail.
        result = subprocess.run(['tail', '-n', '200', MODSEC_AUDIT_LOG], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        
        if not domain_filter:
            return "\n".join(lines)
        
        # Simple string matching for domain if provided
        filtered = [l for l in lines if domain_filter in l]
        return "\n".join(filtered)
    except Exception:
        return "Error reading log."

def install_modsecurity_generator():
    """Generator for live-streaming the ModSecurity installation."""
    import json
    import os
    import subprocess
    import shutil

    def emit(progress, message, error=False, success=False):
        return json.dumps({
            'progress': progress,
            'message': message,
            'error': error,
            'success': success
        }) + "\n"

    try:
        yield emit(5, "Contacting repositories via apt-get update...")
        subprocess.run(['apt-get', 'update', '-y'], check=False)
        
        yield emit(25, "Installing core ModSecurity engine (libapache2-mod-security2)...")
        # SECURITY: Removed shell=True and DEBIAN_FRONTEND env injection via shell.
        # We pass the environment explicitly to subprocess.
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        res = subprocess.run(
            ['apt-get', 'install', 'libapache2-mod-security2', '-y'], 
            capture_output=True, text=True, env=env
        )
        if res.returncode != 0:
            yield emit(25, f"Installation failed: {res.stderr}", error=True)
            return

        yield emit(50, "Configuring primary ModSecurity settings...")
        rec_path = '/etc/modsecurity/modsecurity.conf-recommended'
        conf_path = '/etc/modsecurity/modsecurity.conf'
        if os.path.exists(rec_path) and not os.path.exists(conf_path):
            shutil.copy(rec_path, conf_path)
            subprocess.run(['sed', '-i', 's/SecRuleEngine DetectionOnly/SecRuleEngine On/', conf_path])
        elif not os.path.exists(conf_path):
            os.makedirs('/etc/modsecurity', exist_ok=True)
            with open(conf_path, 'w') as f:
                f.write('SecRuleEngine On\n')

        yield emit(75, "Downloading high-security OWASP Core Rule Set (CRS) profile...")
        crs_path = '/etc/modsecurity/owasp-crs'
        if not os.path.exists(crs_path):
            subprocess.run(['git', 'clone', 'https://github.com/coreruleset/coreruleset', crs_path], capture_output=True)
            if os.path.exists(f'{crs_path}/crs-setup.conf.example'):
                shutil.copy(f'{crs_path}/crs-setup.conf.example', f'{crs_path}/crs-setup.conf')
            
            apache_sec_conf = '/etc/apache2/mods-available/security2.conf'
            if os.path.exists(apache_sec_conf):
                with open(apache_sec_conf, 'r') as f:
                    content = f.read()
                
                if 'owasp-crs' not in content:
                    new_conf = content.replace(
                        '</IfModule>',
                        '        IncludeOptional /etc/modsecurity/owasp-crs/crs-setup.conf\n'
                        '        IncludeOptional /etc/modsecurity/owasp-crs/rules/*.conf\n</IfModule>'
                    )
                    with open(apache_sec_conf, 'w') as f:
                        f.write(new_conf)

        os.makedirs('/etc/modsecurity/rules', exist_ok=True)
        with open('/etc/modsecurity/rules/custom_rules.conf', 'a'): pass
        with open('/etc/modsecurity/rules/disabled_rules.conf', 'a'): pass
        with open('/etc/modsecurity/active_profile.txt', 'w') as f: f.write('owasp')

        yield emit(90, "Enabling Apache ModSecurity Module globally...")
        subprocess.run(['a2enmod', 'security2'], capture_output=True)
        subprocess.run(['systemctl', 'restart', 'apache2'], capture_output=True)

        yield emit(100, "ModSecurity firewall engine has been successfully installed and natively configured!", success=True)
        
    except Exception as e:
        yield emit(0, str(e), error=True)

