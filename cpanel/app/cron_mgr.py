import subprocess
import os
import random

def get_raw_crontab(username=None):
    """Retrieve the raw crontab contents as a list of lines."""
    try:
        cmd = ['crontab', '-l']
        if username:
            cmd = ['crontab', '-u', username, '-l']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.splitlines()
        return []
    except Exception:
        return []

def write_raw_crontab(lines, username=None):
    """Write a list of lines back to the crontab."""
    content = "\n".join(lines) + "\n"
    try:
        # Use subprocess to pipe content into crontab
        cmd = ['crontab', '-']
        if username:
            cmd = ['crontab', '-u', username, '-']
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.communicate(input=content.encode())
        return process.returncode == 0
    except Exception:
        return False

def get_cron_jobs(username=None):
    """Parse the crontab into a structured list of dictionaries."""
    lines = get_raw_crontab(username)
    jobs = []
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
            
        job = {
            'index': i,
            'line': line,
            'is_comment': line.startswith('#'),
            'schedule': '',
            'command': ''
        }
        
        if not job['is_comment']:
            # A standard cron job starts with 5 time/date fields or a macro like @daily
            if line.startswith('@'):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    job['schedule'] = parts[0]
                    job['command'] = parts[1]
            else:
                parts = line.split(None, 5)
                if len(parts) == 6:
                    job['schedule'] = " ".join(parts[0:5])
                    job['command'] = parts[5]
                else:
                    # Malformed or unusual cron line
                    job['is_comment'] = True
                    job['line'] = line
                    
        jobs.append(job)
        
    return jobs

def add_cron_job(schedule, command, username=None):
    """Add a new cron job to the end of the crontab."""
    lines = get_raw_crontab(username)
    
    # Simple validation
    if not schedule or not command:
        return False, "Schedule and command cannot be empty."
        
    new_job = f"{schedule.strip()} {command.strip()}"
    lines.append(new_job)
    
    if write_raw_crontab(lines, username):
        return True, "Cron job added successfully."
    return False, "Failed to add cron job to system."

def delete_cron_job(index, username=None):
    """Delete a cron job by its original line index."""
    try:
        index = int(index)
    except ValueError:
        return False, "Invalid job index."
        
    lines = get_raw_crontab(username)
    if 0 <= index < len(lines):
        del lines[index]
        if write_raw_crontab(lines, username):
            return True, "Cron job deleted successfully."
        return False, "Failed to update system crontab."
    return False, "Cron job not found."

def enable_ssl_renewal():
    """Add the recommended Certbot auto-renewal job if it doesn't exist."""
    lines = get_raw_crontab()
    
    # Check if certbot renew is already in the crontab
    for line in lines:
        if 'certbot renew' in line and not line.strip().startswith('#'):
            return False, "Certbot auto-renewal is already enabled in the crontab."
            
    # EFF recommends running it twice a day at a random minute
    minute = random.randint(0, 59)
    hour1 = random.randint(0, 11)
    hour2 = hour1 + 12
    
    schedule = f"{minute} {hour1},{hour2} * * *"
    command = "certbot renew --quiet"
    
    lines.append(f"# Let's Encrypt Auto-Renewal (Added by Lite-cPanel)")
    lines.append(f"{schedule} {command}")
    
    if write_raw_crontab(lines):
        return True, "Let's Encrypt auto-renewal has been successfully enabled."
    return False, "Failed to add Let's Encrypt auto-renewal."
