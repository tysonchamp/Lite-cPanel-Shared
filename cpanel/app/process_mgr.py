import subprocess
import json
import os
import shutil
import subprocess

PM2_BIN = "/usr/bin/pm2"
PM2_HOME = "/root/.pm2"

def is_pm2_installed():
    """Checks if PM2 is available in the system path."""
    return os.path.exists(PM2_BIN) or shutil.which('pm2') is not None

def get_pm2_cmd():
    return PM2_BIN if os.path.exists(PM2_BIN) else 'pm2'

def list_processes():
    """Returns a list of running PM2 processes in JSON format."""
    if not is_pm2_installed():
        return []
    
    try:
        env = os.environ.copy()
        env["PM2_HOME"] = PM2_HOME
        result = subprocess.run([get_pm2_cmd(), 'jlist'], capture_output=True, text=True, check=True, env=env)
        return json.loads(result.stdout)
    except Exception:
        return []

def manage_process(action, name_or_id):
    """
    Performs an action (start, stop, restart, delete) on a PM2 process.
    """
    if action not in ['start', 'stop', 'restart', 'delete']:
        return False, "Invalid action."
    
    try:
        env = os.environ.copy()
        env["PM2_HOME"] = PM2_HOME
        subprocess.run([get_pm2_cmd(), action, str(name_or_id)], check=True, capture_output=True, text=True, env=env)
        return True, f"Process {action}ed successfully."
    except subprocess.CalledProcessError as e:
        return False, f"Error: {e.stderr.strip()}"

def start_nextjs_app(app_path, app_name, port):
    """
    Starts a Next.js application using PM2.
    """
    if not os.path.exists(app_path):
        return False, f"Path does not exist: {app_path}"
    
    try:
        env = os.environ.copy()
        env["PM2_HOME"] = PM2_HOME
        
        # Check for ecosystem.config.js (Prioritize)
        ecosystem_path = os.path.join(app_path, 'ecosystem.config.js')
        if os.path.exists(ecosystem_path):
            cmd = [get_pm2_cmd(), 'start', 'ecosystem.config.js', '--name', app_name]
        else:
            # Check if it's a standard next.js app (has package.json)
            pkg_json = os.path.join(app_path, 'package.json')
            if not os.path.exists(pkg_json):
                return False, "No package.json or ecosystem.config.js found in the specified path."
            
            # Command to start: npm start -- -p PORT
            if not port:
                return False, "Port is required when no ecosystem.config.js is found."
            
            cmd = [
                get_pm2_cmd(), 'start', 'npm', 
                '--name', app_name, 
                '--', 'start', '--', '-p', str(port)
            ]
        
        subprocess.run(cmd, cwd=app_path, check=True, capture_output=True, text=True, env=env)
        # Save to ensure it persists across reboots
        subprocess.run([get_pm2_cmd(), 'save'], check=True, env=env)
        
        return True, f"Application '{app_name}' started successfully."
    except subprocess.CalledProcessError as e:
        return False, f"Failed to start app: {e.stderr.strip()}"

def run_npm_command(app_path, command):
    """
    Runs an npm command (install or build) in the specified directory.
    """
    if command not in ['install', 'run build']:
        return False, "Invalid npm command."
    
    if not os.path.exists(app_path):
        return False, f"Path does not exist: {app_path}"
    
    try:
        # We use a longer timeout for builds
        cmd = ['npm'] + command.split()
        result = subprocess.run(cmd, cwd=app_path, capture_output=True, text=True, check=True)
        return True, f"npm {command} completed successfully."
    except subprocess.CalledProcessError as e:
        return False, f"npm {command} failed: {e.stderr.strip() or e.stdout.strip()}"
    except Exception as e:
        return False, f"An error occurred: {str(e)}"

def get_process_logs(name, lines=100):
    """Fetches the latest logs for a specific process."""
    try:
        # pm2 logs --lines N --nostream name
        result = subprocess.run(['pm2', 'logs', name, '--lines', str(lines), '--nostream'], 
                                capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as e:
        return f"Error fetching logs: {str(e)}"
