import os
import subprocess
import json
import logging

# Define paths relative to this file
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_FILE = os.path.join(BASE_DIR, 'app', 'config.json')

def get_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"auto_update": False, "last_check": None}

def save_settings(settings):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
        return True
    except Exception:
        return False

def run_git(args):
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True
        )
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    except Exception as e:
        return False, str(e)

def get_version_info():
    """Returns local hash, remote hash, and update availability."""
    success_local, local_hash = run_git(['rev-parse', 'HEAD'])
    
    # Fetch remote to get latest refs
    run_git(['fetch', '--quiet', 'origin'])
    
    # Get the remote default branch (HEAD points to it)
    _, remote_default = run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'])
    if remote_default:
        # Extract branch name from refs/remotes/origin/3.0 -> 3.0
        default_branch = remote_default.replace('refs/remotes/origin/', '').strip()
    else:
        # Fallback: try common names
        for candidate in ['3.0', 'main', 'master']:
            ok, _ = run_git(['rev-parse', f'origin/{candidate}'])
            if ok:
                default_branch = candidate
                break
        else:
            default_branch = 'main'
    
    # Get current local branch
    _, local_branch = run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
    
    success_remote, remote_hash = run_git(['rev-parse', f'origin/{default_branch}'])
    
    if not success_local or not success_remote:
        return {
            "local": local_hash if success_local else "Error",
            "remote": remote_hash if success_remote else "Error",
            "update_available": False,
            "branch": local_branch,
            "default_branch": default_branch
        }
        
    return {
        "local": local_hash[:8],
        "remote": remote_hash[:8],
        "full_local": local_hash,
        "full_remote": remote_hash,
        "update_available": local_hash != remote_hash,
        "branch": local_branch,
        "default_branch": default_branch
    }

def perform_update():
    """Performs git pull from the remote default branch, switching if necessary."""
    # Get the remote default branch
    run_git(['fetch', '--quiet', 'origin'])
    _, remote_default = run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'])
    if remote_default:
        default_branch = remote_default.replace('refs/remotes/origin/', '').strip()
    else:
        # Fallback
        for candidate in ['3.0', 'main', 'master']:
            ok, _ = run_git(['rev-parse', f'origin/{candidate}'])
            if ok:
                default_branch = candidate
                break
        else:
            return False, "Could not determine remote default branch."
    
    # Stash tracked changes and remove untracked files that would block merge
    run_git(['stash', '--include-untracked'])
    run_git(['clean', '-fd'])
    
    # Get current local branch
    _, local_branch = run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
    
    # If we're not on the default branch, switch to it
    if local_branch != default_branch:
        # Check if local branch exists
        ok, _ = run_git(['rev-parse', '--verify', default_branch])
        if ok:
            # Local branch exists, checkout and pull
            run_git(['checkout', default_branch])
        else:
            # Create and track the remote branch
            run_git(['checkout', '-b', default_branch, f'origin/{default_branch}'])
    
    # Pull latest from the default branch
    success, output = run_git(['pull', 'origin', default_branch])
    if success:
        return True, f"Updated to latest from '{default_branch}' branch."
    return False, f"Pull failed: {output}"

def restart_service():
    """Triggers a systemd restart for the cPanel service."""
    try:
        # We use a detached process to ensure the restart command completes after this process dies
        subprocess.Popen(['systemctl', 'restart', 'cpanel'])
        return True, "Restarting service..."
    except Exception as e:
        return False, str(e)
