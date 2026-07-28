import re

file_path = 'cpanel/app/templates/base.html'
with open(file_path, 'r') as f:
    content = f.read()

replacements = {
    "url_for('dashboard')": "url_for('dashboard.dashboard')",
    "url_for('traffic_monitor')": "url_for('dashboard.traffic_monitor')",
    "url_for('domains')": "url_for('domains.domains')",
    "url_for('nextjs')": "url_for('nextjs.nextjs')",
    "url_for('process_manager')": "url_for('nextjs.process_manager')",
    "url_for('databases')": "url_for('databases.databases')",
    "url_for('mongodb_route')": "url_for('databases.mongodb_route')",
    "url_for('ftp')": "url_for('filemanager.ftp')",
    "url_for('filemanager_route')": "url_for('filemanager.filemanager_route')",
    "url_for('wordpress')": "url_for('wordpress.wordpress')",
    "url_for('backups')": "url_for('system.backups')",
    "url_for('firewall')": "url_for('system.firewall')",
    "url_for('modsecurity')": "url_for('system.modsecurity')",
    "url_for('terminal')": "url_for('system.terminal')",
    "url_for('cron')": "url_for('system.cron')",
    "url_for('settings')": "url_for('system.settings')",
    "url_for('admin_plans')": "url_for('admin.admin_plans')",
    "url_for('admin_users')": "url_for('admin.admin_users')",
    "url_for('logout')": "url_for('auth.logout')"
}

for old, new in replacements.items():
    content = content.replace(old, new)

with open(file_path, 'w') as f:
    f.write(content)
print("base.html patched locally")
