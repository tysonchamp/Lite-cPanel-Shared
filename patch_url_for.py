import os
import re

template_dir = 'cpanel/app/templates'
blueprint_map = {
    'dashboard': 'dashboard',
    'traffic_monitor': 'dashboard',
    'traffic_report': 'dashboard',
    'api_sysinfo': 'dashboard',
    'api_services': 'dashboard',
    'api_service_restart': 'dashboard',
    
    'databases': 'databases',
    'mongodb_route': 'databases',
    'phpmyadmin_login': 'databases',
    
    'filemanager_route': 'filemanager',
    'filemanager_read': 'filemanager',
    'filemanager_download': 'filemanager',
    'ftp': 'filemanager',
    
    'domains': 'domains',
    'domain_logs': 'domains',
    'fetch_domain_logs': 'domains',
    'edit_vhost': 'domains',
    'save_vhost': 'domains',
    
    'admin_plans': 'admin',
    'admin_users': 'admin',
    
    'wordpress': 'wordpress',
    
    'terminal': 'system',
    'cron': 'system',
    'backups': 'system',
    'firewall': 'system',
    'modsecurity': 'system',
    'settings': 'system',
    'edit_config': 'system',
    
    'nextjs': 'nextjs',
    'processes': 'nextjs',
    'process_manager': 'nextjs', # In case we missed it
    'api_process_logs': 'nextjs',
    
    'login': 'auth',
    'logout': 'auth'
}

for root, _, files in os.walk(template_dir):
    for file in files:
        if file.endswith('.html'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r') as f:
                content = f.read()
            
            def replace_url_for(match):
                quote = match.group(1)
                endpoint = match.group(2)
                # Skip if static or already has a blueprint prefix
                if endpoint == 'static' or '.' in endpoint:
                    return f"url_for({quote}{endpoint}{quote}"
                
                # Default to same endpoint name if mapping missing (might break if unknown but let's hope it's exhaustive)
                bp = blueprint_map.get(endpoint)
                if bp:
                    return f"url_for({quote}{bp}.{endpoint}{quote}"
                else:
                    print(f"Warning: Unknown endpoint {endpoint} in {filepath}")
                    return f"url_for({quote}{endpoint}{quote}"

            # Match url_for('endpoint' or url_for("endpoint"
            new_content = re.sub(r"url_for\((['\"])([\w_]+)\1", replace_url_for, content)
            
            if new_content != content:
                with open(filepath, 'w') as f:
                    f.write(new_content)
                print(f"Patched {filepath}")

print("Done patching.")
