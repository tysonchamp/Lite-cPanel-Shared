import os
import sqlite3
import subprocess

DB_PATH = '/var/lib/lite-cpanel/hosting.db'

def _get_conn():
    # Ensure directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn

def _init_db(conn):
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS plans (
            name TEXT PRIMARY KEY,
            max_domains INTEGER,
            max_databases INTEGER,
            max_nextjs INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            plan_name TEXT,
            FOREIGN KEY(plan_name) REFERENCES plans(name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            resource_type TEXT,
            resource_name TEXT,
            FOREIGN KEY(username) REFERENCES users(username)
        )
    ''')
    
    # Run migrations for existing databases
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN main_domain TEXT")
    except sqlite3.OperationalError:
        pass # Column already exists
        
    conn.commit()

# Plans Management
def get_plans():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plans")
    plans = {}
    for row in cursor.fetchall():
        plans[row['name']] = {
            "max_domains": row['max_domains'],
            "max_databases": row['max_databases'],
            "max_nextjs": row['max_nextjs']
        }
    conn.close()
    return plans

def add_plan(name, max_domains, max_databases, max_nextjs):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM plans WHERE name = ?", (name,))
    if cursor.fetchone():
        conn.close()
        return False, "Plan already exists."
    
    cursor.execute(
        "INSERT INTO plans (name, max_domains, max_databases, max_nextjs) VALUES (?, ?, ?, ?)",
        (name, int(max_domains), int(max_databases), int(max_nextjs))
    )
    conn.commit()
    conn.close()
    return True, "Plan created successfully."

def delete_plan(name):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE plan_name = ?", (name,))
    user = cursor.fetchone()
    if user:
        conn.close()
        return False, f"Plan is in use by user {user['username']}."
    
    cursor.execute("DELETE FROM plans WHERE name = ?", (name,))
    if cursor.rowcount == 0:
        conn.close()
        return False, "Plan does not exist."
    
    conn.commit()
    conn.close()
    return True, "Plan deleted."

# Users Management
def get_users():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    users = {}
    for row in cursor.fetchall():
        username = row['username']
        users[username] = {
            "plan": row['plan_name'],
            "main_domain": row['main_domain'] if 'main_domain' in row.keys() else '',
            "domains": [],
            "databases": [],
            "nextjs_apps": []
        }
        
    cursor.execute("SELECT * FROM user_resources")
    for res in cursor.fetchall():
        username = res['username']
        rtype = res['resource_type']
        rname = res['resource_name']
        if username in users and rtype in users[username]:
            users[username][rtype].append(rname)
            
    conn.close()
    return users

def get_user_data(username):
    users = get_users()
    return users.get(username)

def get_user_plan_limits(username):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.* FROM plans p 
        JOIN users u ON p.name = u.plan_name 
        WHERE u.username = ?
    ''', (username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "max_domains": row['max_domains'],
            "max_databases": row['max_databases'],
            "max_nextjs": row['max_nextjs']
        }
    return None

def add_user(username, password, plan_name, main_domain):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM plans WHERE name = ?", (plan_name,))
    if not cursor.fetchone():
        conn.close()
        return False, "Plan does not exist."
    
    cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        return False, "User already exists in cPanel."
        
    # Ensure domain is not already in use
    cursor.execute("SELECT id FROM user_resources WHERE resource_type = 'domains' AND resource_name = ?", (main_domain,))
    if cursor.fetchone():
        conn.close()
        return False, "Main domain is already in use by another user."
    
    # Create Linux User
    try:
        res = subprocess.run(['id', '-u', username], capture_output=True)
        if res.returncode != 0:
            # User doesn't exist, create it
            subprocess.run(['useradd', '-m', '-s', '/bin/bash', username], check=True)
        
        # Set password
        subprocess.run(['chpasswd'], input=f"{username}:{password}", text=True, check=True)
        
        # Create public_html
        home_dir = f"/home/{username}"
        public_html = f"{home_dir}/public_html"
        os.makedirs(public_html, exist_ok=True)
        # Fix permissions so webserver can access it (usually setting parent to 755 or 711)
        subprocess.run(['chmod', '711', home_dir], check=False)
        subprocess.run(['chown', '-R', f"{username}:{username}", public_html], check=True)
        subprocess.run(['chmod', '755', public_html], check=True)
        
    except Exception as e:
        conn.close()
        return False, f"Failed to create system user: {str(e)}"

    cursor.execute("INSERT INTO users (username, plan_name, main_domain) VALUES (?, ?, ?)", (username, plan_name, main_domain))
    
    # Also add the main domain to user_resources
    cursor.execute("INSERT INTO user_resources (username, resource_type, resource_name) VALUES (?, ?, ?)", (username, 'domains', main_domain))
    
    conn.commit()
    conn.close()
    return True, "User created successfully."

def delete_user(username):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
    if not cursor.fetchone():
        conn.close()
        return False, "User not found."
    
    # Here we would normally clean up resources, but for now we just remove from db
    # We optionally can remove the Linux user
    try:
        subprocess.run(['userdel', '-r', username], check=False)
    except Exception:
        pass

    cursor.execute("DELETE FROM user_resources WHERE username = ?", (username,))
    cursor.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return True, "User deleted."

# Resource Tracking
def add_user_resource(username, resource_type, resource_name):
    """resource_type: 'domains', 'databases', 'nextjs_apps'"""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
    if not cursor.fetchone():
        conn.close()
        return False
        
    cursor.execute(
        "SELECT id FROM user_resources WHERE username = ? AND resource_type = ? AND resource_name = ?",
        (username, resource_type, resource_name)
    )
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO user_resources (username, resource_type, resource_name) VALUES (?, ?, ?)",
            (username, resource_type, resource_name)
        )
        conn.commit()
    conn.close()
    return True

def remove_user_resource(username, resource_type, resource_name):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_resources WHERE username = ? AND resource_type = ? AND resource_name = ?",
        (username, resource_type, resource_name)
    )
    conn.commit()
    conn.close()
    return True

def can_add_resource(username, resource_type):
    """Check if the user has reached their plan limit."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.max_domains, p.max_databases, p.max_nextjs 
        FROM plans p 
        JOIN users u ON p.name = u.plan_name 
        WHERE u.username = ?
    ''', (username,))
    plan = cursor.fetchone()
    if not plan:
        conn.close()
        return False
        
    cursor.execute(
        "SELECT COUNT(*) as count FROM user_resources WHERE username = ? AND resource_type = ?",
        (username, resource_type)
    )
    current_count = cursor.fetchone()['count']
    conn.close()
    
    if resource_type == "domains":
        return current_count < plan['max_domains']
    elif resource_type == "databases":
        return current_count < plan['max_databases']
    elif resource_type == "nextjs_apps":
        return current_count < plan['max_nextjs']
    return False

def get_owner_of_resource(resource_type, resource_name):
    """Find which user owns a domain, db, etc."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username FROM user_resources WHERE resource_type = ? AND resource_name = ?",
        (resource_type, resource_name)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row['username']
    return None
