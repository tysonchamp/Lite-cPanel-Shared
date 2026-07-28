import os
import subprocess
import secrets
import string
import json
import time
from database_mgr import create_database

def generate_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def get_installed_wordpress(vhosts):
    wp_domains = []
    for vhost in vhosts:
        domain = vhost.get('domain')
        if not domain: continue
        doc_root = vhost.get('doc_root', f'/var/www/{domain}')
        
        def check_wp(check_path, display_domain):
            if os.path.exists(os.path.join(check_path, 'wp-settings.php')):
                is_configured = os.path.exists(os.path.join(check_path, 'wp-config.php'))
                status = "Installed" if is_configured else "Incomplete"
                wp_domains.append({'domain': display_domain, 'path': check_path, 'status': status})

        check_wp(doc_root, domain)
        
        # Check subdirectories
        if os.path.exists(doc_root):
            try:
                for item in os.listdir(doc_root):
                    subpath = os.path.join(doc_root, item)
                    if os.path.isdir(subpath):
                        check_wp(subpath, f"{domain}/{item}")
            except Exception:
                pass
    return wp_domains

def delete_wordpress(path):
    if not os.path.exists(os.path.join(path, 'wp-settings.php')):
        return False, "WordPress not found at this location."

    db_name = None
    db_user = None
    wp_config = os.path.join(path, 'wp-config.php')
    
    if os.path.exists(wp_config):
        import re
        try:
            with open(wp_config, 'r') as f:
                content = f.read()
                db_name_match = re.search(r"define\(\s*'DB_NAME',\s*'([^']+)'\s*\)", content)
                db_user_match = re.search(r"define\(\s*'DB_USER',\s*'([^']+)'\s*\)", content)
                if db_name_match: db_name = db_name_match.group(1)
                if db_user_match: db_user = db_user_match.group(1)
        except Exception:
            pass

    from database_mgr import delete_database, get_mysql_connection
    if db_name:
        delete_database(db_name)
    if db_user:
        conn = get_mysql_connection()
        if conn:
            try:
                with conn.cursor() as cursor:
                    user_esc = db_user.replace("'", "''")
                    cursor.execute(f"DROP USER IF EXISTS '{user_esc}'@'localhost'")
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()

    wp_items = [
        'wp-admin', 'wp-content', 'wp-includes', 
        'wp-activate.php', 'wp-blog-header.php', 'wp-comments-post.php', 
        'wp-config-sample.php', 'wp-config.php', 'wp-cron.php', 
        'wp-links-opml.php', 'wp-load.php', 'wp-login.php', 
        'wp-mail.php', 'wp-settings.php', 'wp-signup.php', 
        'wp-trackback.php', 'xmlrpc.php', 'index.php', 'license.txt', 'readme.html'
    ]
    for item in wp_items:
        item_path = os.path.join(path, item)
        if os.path.isdir(item_path):
            import shutil
            shutil.rmtree(item_path, ignore_errors=True)
        elif os.path.exists(item_path):
            try: os.remove(item_path)
            except OSError: pass
            
    if path.count('/') > 3:
        try: os.rmdir(path)
        except OSError: pass

    return True, "WordPress successfully removed."

def install_wordpress_generator(domain, target_path=""):
    def emit(progress, message, error=False, success=False):
        return json.dumps({
            "progress": progress,
            "message": message,
            "error": error,
            "success": success
        }) + "\n"

    try:
        yield emit(5, "Validating installation paths...")
        
        # Get actual doc root for this domain
        from domains_mgr import get_virtual_hosts
        from flask import session
        vhosts = get_virtual_hosts(session.get('role', 'admin'), session.get('username', None))
        domain_info = next((v for v in vhosts if v['domain'] == domain), None)
        
        if not domain_info:
            yield emit(100, f"Domain {domain} not found or access denied.", error=True)
            return
            
        base_dir = domain_info.get('doc_root', f'/var/www/{domain}')
        
        if not target_path:
            doc_root = base_dir
        else:
            clean_target = os.path.normpath(f"/{target_path}").lstrip('/')
            doc_root = os.path.join(base_dir, clean_target)

        if not os.path.exists(base_dir):
            yield emit(100, f"Domain directory {base_dir} does not exist. Please create the virtual host first.", error=True)
            return
        
        os.makedirs(doc_root, exist_ok=True)

        if os.path.exists(os.path.join(doc_root, 'wp-config.php')):
            yield emit(100, f"WordPress is already installed in {doc_root}", error=True)
            return

        yield emit(15, "Checking WP-CLI utility...")
        # SECURITY: Remove shell=True, use list-based lookup
        wp_check = subprocess.run(['which', 'wp'], capture_output=True, text=True)
        if wp_check.returncode != 0:
            yield emit(20, "Downloading & Installing WP-CLI...")
            # We use multiple safe steps instead of a piped shell string
            try:
                subprocess.run(['curl', '-sL', 'https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar', '-o', '/tmp/wp-cli.phar'], check=True)
                subprocess.run(['chmod', '+x', '/tmp/wp-cli.phar'], check=True)
                subprocess.run(['mv', '/tmp/wp-cli.phar', '/usr/local/bin/wp'], check=True)
                subprocess.run(['sync'], check=True)
            except subprocess.CalledProcessError as e:
                yield emit(100, f"WP-CLI install failed: {str(e)}", error=True)
                return

        time.sleep(0.5) # ensure sync propagation
        
        yield emit(35, "Generating secure database credentials...")
        db_name_base = domain.replace('.', '_').replace('-', '_')
        if target_path:
            db_name_base = f"{db_name_base}_{target_path.replace('/', '_')}"
        
        db_name = f"{db_name_base[:28]}_wp"
        db_user = f"{db_name_base[:12]}_usr"
        db_pass = generate_password()

        yield emit(45, f"Creating MySQL database `{db_name}`...")
        success, msg = create_database(db_name, db_user, db_pass)
        if not success:
            yield emit(100, f"Database creation failed: {msg}", error=True)
            return

        yield emit(55, "Removing standard index page if present...")
        default_index = os.path.join(doc_root, 'index.php')
        if os.path.exists(default_index):
            with open(default_index, 'r') as f:
                content = f.read()
                if 'WP_USE_THEMES' not in content:
                    os.remove(default_index)

        yield emit(65, "Downloading WordPress core via WP-CLI...")
        res = subprocess.run(['wp', 'core', 'download', f'--path={doc_root}', '--force', '--allow-root'], capture_output=True, text=True)
        if res.returncode != 0:
            yield emit(100, f"WordPress download failed: {res.stderr}", error=True)
            return

        yield emit(85, "Configuring wp-config.php integrations...")
        res = subprocess.run(['wp', 'config', 'create', f'--dbname={db_name}', f'--dbuser={db_user}', f'--dbpass={db_pass}', f'--path={doc_root}', '--allow-root'], capture_output=True, text=True)
        if res.returncode != 0:
            yield emit(100, f"WordPress config creation failed: {res.stderr}", error=True)
            return

        yield emit(95, "Securing filesystem permissions...")
        
        # Determine appropriate ownership based on path
        owner = "www-data"
        if doc_root.startswith('/home/'):
            parts = doc_root.split('/')
            if len(parts) >= 3:
                owner = parts[2]
                
        subprocess.run(['chown', '-R', f'{owner}:{owner}', doc_root])
        
        if owner == "www-data":
            subprocess.run(['chmod', '-R', '755', doc_root])
        else:
            # Set group permissions so web server can read/write, setgid bit for inheritance
            subprocess.run(['find', doc_root, '-type', 'd', '-exec', 'chmod', '770', '{}', '+'])
            subprocess.run(['find', doc_root, '-type', 'f', '-exec', 'chmod', '660', '{}', '+'])
            subprocess.run(['find', doc_root, '-type', 'd', '-exec', 'chmod', 'g+s', '{}', '+'])

        yield emit(100, f"WordPress successfully installed in {doc_root}!", success=True)

    except Exception as e:
        yield emit(100, f"Unexpected error: {str(e)}", error=True)
