from flask import Flask
import os
import glob
import subprocess
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
import logging

# Ensure consistent environment for PM2 and other system tools
if os.getuid() == 0:
    os.environ["HOME"] = "/root"
    os.environ["PM2_HOME"] = "/root/.pm2"

# Explicitly prioritize the user's working Node/PM2 path
paths = [
    "/root/.nvm/versions/node/v24.15.0/bin",
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"
]
nvm_node_paths = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))
if nvm_node_paths:
    nvm_node_paths.sort(reverse=True)
    for p in nvm_node_paths:
        if p not in paths:
            paths.append(p)

os.environ["PATH"] = ":".join(paths) + ":" + os.environ.get("PATH", "")

# Auto-Resurrect PM2 processes on startup
try:
    from process_mgr import get_pm2_cmd, PM2_HOME
    env = os.environ.copy()
    env["PM2_HOME"] = PM2_HOME
    subprocess.run([get_pm2_cmd(), 'resurrect'], capture_output=True, text=True, env=env)
except Exception:
    pass

load_dotenv()

app = Flask(__name__)
csrf = CSRFProtect(app)

app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    logging.warning("No FLASK_SECRET_KEY set in environment. Using a random key. Sessions will invalidate on restart.")
    app.secret_key = os.urandom(24)

from flask_sock import Sock
sock = Sock(app)
app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024  # 1GB limit

from terminal_mgr import register_terminal_websocket
register_terminal_websocket(sock)

# --- Auto-Updater ---
import threading
import time
from updater_mgr import get_settings, get_version_info, perform_update, restart_service

def auto_updater_worker():
    time.sleep(30)
    while True:
        try:
            settings = get_settings()
            if settings.get("auto_update"):
                info = get_version_info()
                if info.get("update_available"):
                    success, msg = perform_update()
                    if success:
                        restart_service()
        except Exception as e:
            print(f"Updater error: {e}")
        time.sleep(3600)

updater_thread = threading.Thread(target=auto_updater_worker, daemon=True)
updater_thread.start()

# --- Custom Filters ---
import datetime as _dt
@app.template_filter('datetimeformat')
def _datetimeformat(ts):
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''

# --- Register Blueprints ---
import sys
# Add current dir to path to ensure imports work correctly from blueprints
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from lib.routes_auth import auth_bp
from lib.routes_dashboard import dashboard_bp
from lib.routes_domains import domains_bp
from lib.routes_databases import databases_bp
from lib.routes_nextjs import nextjs_bp
from lib.routes_filemanager import filemanager_bp
from lib.routes_system import system_bp
from lib.routes_wordpress import wordpress_bp
from lib.routes_admin import admin_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(domains_bp)
app.register_blueprint(databases_bp)
app.register_blueprint(nextjs_bp)
app.register_blueprint(filemanager_bp)
app.register_blueprint(system_bp)
app.register_blueprint(wordpress_bp)
app.register_blueprint(admin_bp)

if __name__ == '__main__':
    # Run on all interfaces, port 2083
    app.run(host='0.0.0.0', port=2083, debug=True)
