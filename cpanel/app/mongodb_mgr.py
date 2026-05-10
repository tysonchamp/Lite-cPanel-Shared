import os
import subprocess
try:
    import pymongo
except ImportError:
    pymongo = None

def check_mongodb_installed():
    import shutil
    return shutil.which('mongod') is not None

def install_mongodb():
    install_script = """#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
apt-get install gnupg curl -y
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor --yes
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.2 multiverse" > /etc/apt/sources.list.d/mongodb-org-8.2.list
apt-get update
apt-get install -y mongodb-org php-pear php-mongodb
systemctl enable mongod
systemctl start mongod
"""
    try:
        script_path = '/tmp/install_mongo.sh'
        with open(script_path, 'w') as f:
            f.write(install_script)
        os.chmod(script_path, 0o755)
        
        log_file = open('/var/log/lite-cpanel-mongo-install.log', 'w')
        subprocess.Popen(['bash', script_path], stdout=log_file, stderr=subprocess.STDOUT)
        return True, "MongoDB installation started in the background. Please wait a few minutes."
    except Exception as e:
        return False, f"Failed to start installation: {e}"

def get_mongo_client():
    if not pymongo:
        return None
    try:
        client = pymongo.MongoClient('mongodb://127.0.0.1:27017/', serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        return client
    except Exception:
        return None

def get_databases():
    client = get_mongo_client()
    if not client: return []
    try:
        system_dbs = ('admin', 'config', 'local')
        
        # Get databases that have actual data
        listed_dbs = set(client.list_database_names()) - set(system_dbs)
        
        # Also discover databases that have users but no collections yet
        # Only add if the DB was explicitly created (has a user), not if it was dropped
        try:
            existing = set(client.list_database_names())
            all_users = client['admin'].system.users.find({}, {'db': 1, 'user': 1})
            for u in all_users:
                db_name = u.get('db')
                if db_name not in system_dbs and db_name in existing:
                    listed_dbs.add(db_name)
        except Exception:
            pass
        
        result = []
        for db_name in sorted(listed_dbs):
            try:
                db_obj = client[db_name]
                users_info = db_obj.command("usersInfo")
                users = [{'User': u['user']} for u in users_info.get('users', [])]
                
                try:
                    stats = db_obj.command("dbStats")
                    size_mb = round(stats.get('dataSize', 0) / (1024 * 1024), 2)
                except Exception:
                    size_mb = 0
                
                result.append({
                    'name': db_name,
                    'users': users,
                    'size_mb': size_mb
                })
            except Exception:
                result.append({
                    'name': db_name,
                    'users': [],
                    'size_mb': 0
                })
        return result
    except Exception:
        return []

def create_database(db_name, db_user, db_pass):
    client = get_mongo_client()
    if not client: return False, "Could not connect to MongoDB."
    try:
        db = client[db_name]
        db.command("createUser", db_user, pwd=db_pass, roles=["dbOwner"])
        # Insert a metadata doc so the database actually materializes
        # (MongoDB won't show a DB in list_database_names until it has data)
        db['_init'].insert_one({'_created_by': 'lite-cpanel', 'info': 'initial collection'})
        return True, "Database and user created successfully."
    except Exception as e:
        return False, f"Error: {str(e)}"

def delete_database(db_name):
    client = get_mongo_client()
    if not client: return False, "Could not connect to MongoDB."
    try:
        # Drop all users belonging to this DB, then drop the DB
        try:
            users_info = client[db_name].command("usersInfo")
            for u in users_info.get('users', []):
                try:
                    client[db_name].command('dropUser', u['user'])
                except Exception:
                    pass
        except Exception:
            pass
        # Also purge any orphaned entries directly from admin.system.users
        try:
            client['admin'].system.users.delete_many({'db': db_name})
        except Exception:
            pass
        client.drop_database(db_name)
        return True, "Database deleted successfully."
    except Exception as e:
        return False, f"Error: {str(e)}"

def change_user_password(db_name, db_user, new_password):
    client = get_mongo_client()
    if not client: return False, "Could not connect to MongoDB."
    try:
        db = client[db_name]
        db.command("updateUser", db_user, pwd=new_password)
        return True, f"Password updated for {db_user}."
    except Exception as e:
        return False, f"Error: {str(e)}"


# --- Mongo Express Management ---

MONGO_EXPRESS_PORT = 8081
_ME_DIR = '/var/lib/lite-cpanel'
MONGO_EXPRESS_CONFIG = os.path.join(_ME_DIR, 'mongo-express.config.js')
_ME_PID_FILE  = os.path.join(_ME_DIR, '.mongo_express.pid')
_ME_CREDS_FILE = os.path.join(_ME_DIR, '.mongo_express_creds')
_ME_LOG_FILE  = '/var/log/mongo-express.log'

def _find_nvm_node():
    """Find node binary inside ~/.nvm, returns (node_bin, npm_root) or (None, None)."""
    home = os.path.expanduser("~")
    nvm_versions = os.path.join(home, ".nvm", "versions", "node")
    if not os.path.exists(nvm_versions):
        return None, None
    versions = os.listdir(nvm_versions)
    # Prefer v20, then v22, then any other version (sorted descending)
    def _ver_key(v):
        try: return tuple(int(x) for x in v.lstrip('v').split('.'))
        except: return (0,)
    sorted_versions = sorted(versions, key=_ver_key, reverse=True)
    preferred = [v for v in sorted_versions if v.startswith('v20')] + \
                [v for v in sorted_versions if not v.startswith('v20')]
    for v in preferred:
        node_bin = os.path.join(nvm_versions, v, 'bin', 'node')
        npm_root = os.path.join(nvm_versions, v, 'lib', 'node_modules')
        if os.path.exists(node_bin):
            return node_bin, npm_root
    # Fallback: check system node
    import shutil
    sys_node = shutil.which('node')
    if sys_node:
        try:
            r = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True)
            if r.returncode == 0:
                return sys_node, r.stdout.strip()
        except Exception:
            pass
    return None, None

def _find_me_app():
    """Return path to mongo-express app.js or None."""
    # Check NVM path first
    _, npm_root = _find_nvm_node()
    if npm_root:
        candidate = os.path.join(npm_root, 'mongo-express', 'app.js')
        if os.path.exists(candidate):
            return candidate
    # Fallback: system npm root
    try:
        r = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True)
        if r.returncode == 0:
            candidate = os.path.join(r.stdout.strip(), 'mongo-express', 'app.js')
            if os.path.exists(candidate):
                return candidate
    except Exception:
        pass
    return None

def check_mongo_express_installed():
    """True only if mongo-express app.js exists."""
    return _find_me_app() is not None

def get_mongo_express_status():
    """Returns 'active', 'inactive', or 'not_installed'."""
    if not check_mongo_express_installed():
        return 'not_installed'
    # Check port 8081 directly — most reliable regardless of PID file staleness
    try:
        r = subprocess.run(['ss', '-tlnp'], capture_output=True, text=True)
        if f':{MONGO_EXPRESS_PORT}' in r.stdout:
            return 'active'
    except Exception:
        pass
    return 'inactive'

def install_mongo_express():
    """Install mongo-express via nvm/npm, write config, and start the process."""
    try:
        # 1. Install via nvm
        home = os.path.expanduser("~")
        nvm_dir = os.path.join(home, ".nvm")
        install_script = f"""#!/bin/bash
export NVM_DIR="{nvm_dir}"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
fi
\\. "$NVM_DIR/nvm.sh"
if [[ ! "$(node -v 2>/dev/null)" == v20* ]]; then
    nvm install 20
fi
nvm use 20 2>/dev/null || nvm install 20
npm install -g mongo-express@1.0.0
"""
        res = subprocess.run(['bash', '-c', install_script],
                             capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            return False, f"npm install failed: {res.stderr}"

        # 2. Write mongo-express config to /var/lib/lite-cpanel
        import secrets
        os.makedirs(_ME_DIR, exist_ok=True)
        admin_pass = secrets.token_urlsafe(16)
        cookie_secret = secrets.token_hex(32)
        config = f"""'use strict';
module.exports = {{
  mongodb: {{
    server: '127.0.0.1',
    port: 27017,
    url: 'mongodb://127.0.0.1:27017',
    admin: true,
  }},
  basicAuth: {{ username: 'admin', password: '{admin_pass}' }},
  options: {{
    documentsPerPage: 50,
    editorTheme: 'rubyblue',
    readOnly: false,
    noDelete: false,
    confirmDelete: true,
  }},
  site: {{
    baseUrl: '/mongo-express',
    cookieKeyName: 'mongo-express',
    cookieSecret: '{cookie_secret}',
    host: '127.0.0.1',
    port: {MONGO_EXPRESS_PORT},
    requestSizeLimit: '50mb',
    sslEnabled: false,
  }},
  useBasicAuth: true,
}};
"""
        with open(MONGO_EXPRESS_CONFIG, 'w') as f:
            f.write(config)
        os.chmod(MONGO_EXPRESS_CONFIG, 0o600)

        # 3. Save credentials
        with open(_ME_CREDS_FILE, 'w') as f:
            f.write(f"Mongo Express Admin Username: admin\nMongo Express Admin Password: {admin_pass}\n")
        os.chmod(_ME_CREDS_FILE, 0o600)

        # 4. Setup Apache proxy (best-effort, requires root)
        try:
            _setup_apache_proxy()
        except Exception:
            pass

        # 5. Start the process
        ok, msg = _start_mongo_express()
        if not ok:
            return False, f"Installed but failed to start: {msg}"

        return True, "Mongo Express installed and started successfully!"
    except subprocess.TimeoutExpired:
        return False, "Installation timed out. Please try again."
    except Exception as e:
        return False, f"Installation failed: {str(e)}"

def _start_mongo_express():
    """Launch mongo-express as a background process and store its PID."""
    me_app = _find_me_app()
    if not me_app:
        return False, "mongo-express not found."
    node_bin, _ = _find_nvm_node()
    if not node_bin:
        node_bin = 'node'   # fallback to system node

    env = os.environ.copy()
    env['NODE_ENV'] = 'production'
    
    # Load creds
    creds = get_mongo_express_credentials()
    
    env['ME_CONFIG_MONGODB_URL'] = 'mongodb://127.0.0.1:27017'
    env['ME_CONFIG_MONGODB_ENABLE_ADMIN'] = 'true'
    env['ME_CONFIG_BASICAUTH_USERNAME'] = creds.get('username', 'admin')
    env['ME_CONFIG_BASICAUTH_PASSWORD'] = creds.get('password', 'pass')
    env['ME_CONFIG_SITE_BASEURL'] = '/mongo-express'
    env['ME_CONFIG_SITE_PORT'] = str(MONGO_EXPRESS_PORT)
    env['ME_CONFIG_SITE_COOKIESECRET'] = 'verysecret'
    env['ME_CONFIG_SITE_HOST'] = '127.0.0.1'
    
    # Ensure the NVM node is on PATH
    nvm_bin = os.path.dirname(node_bin)
    env['PATH'] = nvm_bin + ':' + env.get('PATH', '')

    log = open(_ME_LOG_FILE, 'a')
    try:
        proc = subprocess.Popen(
            [node_bin, me_app],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,   # detach from parent
        )
        with open(_ME_PID_FILE, 'w') as f:
            f.write(str(proc.pid))
        return True, f"Started with PID {proc.pid}"
    except Exception as e:
        return False, str(e)

def _setup_apache_proxy():
    """Add Apache ProxyPass for /mongo-express."""
    conf_path = '/etc/apache2/conf-available/mongo-express.conf'
    content = f"""# Mongo Express Reverse Proxy
<Location /mongo-express>
    ProxyPass http://127.0.0.1:{MONGO_EXPRESS_PORT}/mongo-express
    ProxyPassReverse http://127.0.0.1:{MONGO_EXPRESS_PORT}/mongo-express
</Location>
"""
    with open(conf_path, 'w') as f:
        f.write(content)
    subprocess.run(['a2enmod', 'proxy'], capture_output=True)
    subprocess.run(['a2enmod', 'proxy_http'], capture_output=True)
    subprocess.run(['a2enconf', 'mongo-express'], capture_output=True)
    subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)

def restart_mongo_express():
    """Stop any running instance and start a fresh one."""
    try:
        # Kill existing process if any
        if os.path.exists(_ME_PID_FILE):
            try:
                with open(_ME_PID_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 15)   # SIGTERM
            except (ValueError, ProcessLookupError):
                pass
            os.remove(_ME_PID_FILE)
        import time
        time.sleep(1)
        ok, msg = _start_mongo_express()
        if ok:
            return True, "Mongo Express started successfully."
        return False, msg
    except Exception as e:
        return False, f"Failed to restart: {str(e)}"

def get_mongo_express_credentials():
    """Read saved Mongo Express credentials."""
    creds = {'username': 'admin', 'password': ''}
    if os.path.exists(_ME_CREDS_FILE):
        with open(_ME_CREDS_FILE, 'r') as f:
            for line in f:
                if 'Username:' in line:
                    creds['username'] = line.split(':', 1)[1].strip()
                elif 'Password:' in line:
                    creds['password'] = line.split(':', 1)[1].strip()
    return creds

