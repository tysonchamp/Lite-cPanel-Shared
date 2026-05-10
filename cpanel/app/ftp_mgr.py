import subprocess
import os
import logging
import grp
import pwd
import re

def run_system_command(args, input_str=None, check=False):
    """
    Runs a command with sudo if not already root.
    """
    cmd = args
    if os.getuid() != 0:
        cmd = ['sudo', '-n'] + args
    
    if input_str:
        return subprocess.run(cmd, input=input_str, capture_output=True, text=True, check=check)
    else:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

def check_pureftpd_installed():
    try:
        run_system_command(['pure-pw', '--help'], check=True)
        return True
    except:
        return False

def get_user_directory(username):
    """
    Robustly retrieves the home directory for an FTP/SFTP user from multiple sources.
    """
    # 1. Try pure-pw show
    if check_pureftpd_installed():
        try:
            show_res = subprocess.run(['pure-pw', 'show', username], capture_output=True, text=True)
            if show_res.returncode == 0:
                # Flexible regex for different versions of pure-pw
                m = re.search(r'(?:Directory|Home directory|Relative home directory)\s*:\s*(.*)', show_res.stdout)
                if m:
                    return m.group(1).strip().replace('/./', '/').rstrip('/')
        except: pass

    # 2. Try pure-pw list
    if check_pureftpd_installed():
        try:
            result = subprocess.run(['pure-pw', 'list'], capture_output=True, text=True)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.strip().startswith(f"{username} "):
                        parts = line.split(maxsplit=1)
                        if len(parts) == 2:
                            return parts[1].strip().replace('/./', '/').rstrip('/')
        except: pass

    # 3. Try direct passwd file parsing
    passwd_files = ['/etc/pure-ftpd/pureftpd.passwd', '/etc/pureftpd.passwd']
    for pf in passwd_files:
        if os.path.exists(pf):
            try:
                with open(pf, 'r') as f:
                    for line in f:
                        if line.startswith(f"{username}:"):
                            parts = line.split(':')
                            if len(parts) >= 6:
                                return parts[5].strip().replace('/./', '/').rstrip('/')
            except: pass

    # 4. Try system user home directory (for SFTP users)
    try:
        user_info = pwd.getpwnam(username)
        rel_home = user_info.pw_dir
        if rel_home.startswith('/var/www'):
            return rel_home
        
        # Only prepend /var/www if it's a relative-style path (e.g. /demoftp) 
        # and the resulting directory exists.
        full_path = os.path.join('/var/www', rel_home.lstrip('/'))
        if os.path.exists(full_path) and rel_home.count('/') <= 1:
            return full_path
            
        return rel_home
    except: pass

    return "/var/www"

def ensure_system_user(username, directory, password=None):
    """
    Ensures a system user exists with the correct directory and group.
    """
    try:
        run_system_command(['groupadd', '-f', 'lite_sftp'])
        
        # Determine relative home for jail
        relative_home = directory.replace('/var/www', '')
        if not relative_home: relative_home = "/"
        
        # Try to find if user exists
        user_exists = False
        try:
            pwd.getpwnam(username)
            user_exists = True
        except KeyError:
            user_exists = False

        if user_exists:
            # Update existing
            run_system_command(['usermod', '-d', relative_home, '-s', '/usr/sbin/nologin', '-G', 'lite_sftp', username])
        else:
            # Create new
            # -N tells useradd NOT to create a group with the same name as the user
            # We then try to use the username as the group if it exists, or let it use default
            res = run_system_command(['useradd', '-d', relative_home, '-s', '/usr/sbin/nologin', '-G', 'lite_sftp', username])
            if res.returncode != 0:
                # If it failed because the GROUP already exists, try with -g to use that group
                if "group" in (res.stderr or "") and "exists" in (res.stderr or ""):
                    res = run_system_command(['useradd', '-g', username, '-d', relative_home, '-s', '/usr/sbin/nologin', '-G', 'lite_sftp', username])
                
                if res.returncode != 0 and "already exists" not in (res.stderr or ""):
                    raise Exception(f"useradd failed (status {res.returncode}): {res.stderr}")
        
        if password:
            run_system_command(['chpasswd'], input_str=f"{username}:{password}\n")
            # For security, we lock it unless SFTP is explicitly enabled by the user later
            run_system_command(['passwd', '-l', username])
            
        return True
    except Exception as e:
        logging.error(f"Error in ensure_system_user: {str(e)}")
        raise e

def get_ftp_users():
    """
    Returns a list of pure-ftpd virtual users with their status.
    Merges results from pure-pw list, direct file parsing, and system group.
    """
    users_map = {} # username -> directory

    # 1. Source: pure-pw list
    if check_pureftpd_installed():
        try:
            result = subprocess.run(['pure-pw', 'list'], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    # Handle both space and colon separators
                    if ':' in line and not any(line.startswith(p) for p in ['/', './']):
                        parts = line.split(':')
                        if len(parts) >= 6:
                            users_map[parts[0].strip()] = parts[5].strip()
                        elif len(parts) == 2:
                            users_map[parts[0].strip()] = parts[1].strip()
                    else:
                        parts = line.split(maxsplit=1)
                        if len(parts) == 2:
                            users_map[parts[0].strip()] = parts[1].strip()
        except Exception as e:
            logging.debug(f"pure-pw list failed: {str(e)}")

    # 2. Source: Direct file parsing (merge)
    passwd_files = ['/etc/pure-ftpd/pureftpd.passwd', '/etc/pureftpd.passwd']
    for pf in passwd_files:
        if os.path.exists(pf):
            try:
                with open(pf, 'r') as f:
                    for line in f:
                        if line.strip() and ':' in line:
                            parts = line.split(':')
                            if len(parts) >= 6:
                                username = parts[0].strip()
                                directory = parts[5].strip()
                                if username not in users_map:
                                    users_map[username] = directory
            except Exception as e:
                logging.debug(f"Error reading {pf}: {str(e)}")

    # 3. Source: System group 'lite_sftp' (merge)
    try:
        # Use getent to get ALL members (secondary)
        res = subprocess.run(['getent', 'group', 'lite_sftp'], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split(':')
            if len(parts) >= 4 and parts[3]:
                for u in parts[3].split(','):
                    u_name = u.strip()
                    if u_name and u_name not in users_map:
                        users_map[u_name] = get_user_directory(u_name)
        
        # Also check for users with lite_sftp as their PRIMARY group
        try:
            group_info = grp.getgrnam('lite_sftp')
            target_gid = group_info.gr_gid
            # Add secondary members from gr_mem as well
            for u in group_info.gr_mem:
                if u not in users_map:
                    users_map[u] = get_user_directory(u)
            
            # Check for primary group members
            for u in pwd.getpwall():
                if u.pw_gid == target_gid and u.pw_name not in users_map:
                    users_map[u.pw_name] = get_user_directory(u.pw_name)
        except: pass
    except Exception as e:
        logging.debug(f"Error scanning lite_sftp group: {str(e)}")

    # Convert map to list and add status
    users = []
    for username, directory in users_map.items():
        # Normalize directory: remove trailing /./ and /
        clean_dir = directory.replace('/./', '/').rstrip('/')
        if not clean_dir:
            clean_dir = "/"
        # SFTP/System Status check
        system_enabled = False
        try:
            # Check if locked
            pw_res = run_system_command(['passwd', '-S', username])
            # Format: 'username status date ...' where status is P, L, NP, etc.
            # We split and check the second field.
            pw_parts = pw_res.stdout.split()
            if len(pw_parts) >= 2:
                status_code = pw_parts[1]
                # P = Usable password, PS = Password set (some systems), NP = No password (locked usually)
                if status_code in ['P', 'PS']:
                    system_enabled = True
        except: pass

        # Pure-FTPd Virtual Status Check (Primary)
        # If we can't determine it easily, we assume True if the system check is enough,
        # but let's be more robust by checking if it's NOT expired.
        # For now, we'll favor the system check but allow 'enabled' if either is true 
        # or if it's explicitly a virtual-only user (though the system user should exist).
        enabled = system_enabled

        users.append({
            'username': username,
            'directory': clean_dir,
            'enabled': enabled
        })

    users.sort(key=lambda x: x['username'])
    return users

def create_ftp_user(username, password, directory):
    """
    Creates a new pure-ftpd virtual user AND a matching system user for SFTP.
    Starts DISABLED by default.
    """
    if not check_pureftpd_installed():
        return False, "pure-ftpd is not installed."

    try:
        if not directory.startswith('/var/www'):
            return False, "Access denied: FTP directory must be within /var/www"
            
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            subprocess.run(['chown', '-R', 'www-data:www-data', directory], check=True)

        # 1. Create Virtual User (Step 1: Add to text file)
        passwd_file = '/etc/pure-ftpd/pureftpd.passwd'
        # Using numeric UIDs (33 for www-data) for better compatibility
        res = run_system_command(
            ['pure-pw', 'useradd', username, '-u', '33', '-g', '33', '-d', directory, '-f', passwd_file],
            input_str=f"{password}\n{password}\n"
        )

        if res.returncode != 0:
            return False, f"Failed to create Virtual User: {res.stderr or res.stdout}"

        # Step 2: Manually commit the changes to the binary database
        mkdb_res = run_system_command(['pure-pw', 'mkdb', '-f', passwd_file])
        if mkdb_res.returncode != 0:
             return False, f"User added to text file, but failed to update binary database: {mkdb_res.stderr}"

        # VERIFICATION: Immediately check if Pure-FTPd sees the user
        verify_res = run_system_command(['pure-pw', 'show', username, '-f', passwd_file])
        if verify_res.returncode != 0:
            return False, f"Pure-pw reported success, but verification failed: {verify_res.stderr}. This usually means the web server does not have permission to write to {passwd_file}."

        # 2. Create System User (Locked, No Login)
        try:
            ensure_system_user(username, directory, password)
        except Exception as system_e:
            return True, f"FTP created, but SFTP bridge failed: {str(system_e)}"
        
        return True, f"FTP user {username} created (Disabled). Toggle to Enable."
    except Exception as e:
        return False, str(e)

def delete_ftp_user(username):
    if not check_pureftpd_installed():
        return False, "pure-ftpd is not installed."
    try:
        # 1. Try to delete from Pure-FTPd
        # We don't use check=True because the user might not exist in Pure-FTPd virtual database
        subprocess.run(['pure-pw', 'userdel', username, '-m'], capture_output=True)
        
        # 2. Try to delete the system user (SFTP Bridge)
        subprocess.run(['userdel', username], capture_output=True)
        
        return True, f"User {username} deleted."
    except Exception as e:
        return False, str(e)

def change_ftp_password(username, new_password):
    if not check_pureftpd_installed():
        return False, "pure-ftpd is not installed."
    try:
        # 1. Try to update Pure-FTPd password
        process = subprocess.Popen(['pure-pw', 'passwd', username, '-m'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.communicate(input=f"{new_password}\n{new_password}\n")
        
        # 2. Try to update system password (SFTP Bridge)
        try:
            pw_proc = subprocess.Popen(['chpasswd'], stdin=subprocess.PIPE, text=True)
            pw_proc.communicate(input=f"{username}:{new_password}\n")
        except: pass
        
        return True, f"Password updated for {username}."
    except Exception as e:
        return False, str(e)

def get_sftp_status():
    try:
        if not os.path.exists('/etc/ssh/sshd_config'):
            return False
        with open('/etc/ssh/sshd_config', 'r') as f:
            for line in f:
                if 'Subsystem' in line and 'sftp' in line and 'internal-sftp' in line:
                    return not line.strip().startswith('#')
        return False
    except:
        return False

def toggle_sftp(enable=True):
    try:
        sshd_path = '/etc/ssh/sshd_config'
        if not os.path.exists(sshd_path):
            return False, "SSH config not found."
            
        subprocess.run(['groupadd', '-f', 'lite_sftp'], check=True)
            
        with open(sshd_path, 'r') as f:
            lines = f.readlines()
        
        # Clean up existing Lite-cPanel configurations to start fresh
        new_lines = []
        skip = False
        for line in lines:
            if '# Added by Lite-cPanel' in line or 'Match Group lite_sftp' in line:
                skip = True
                continue
            if skip and (line.startswith('Match') or line.startswith('# Subsystem')):
                skip = False
            if skip:
                continue
            
            # Update subsystem line
            if 'Subsystem' in line and 'sftp' in line:
                if enable:
                    new_lines.append('Subsystem sftp internal-sftp\n')
                else:
                    new_lines.append('# Subsystem sftp internal-sftp\n')
                continue
                
            new_lines.append(line)
            
        if enable:
            # Always append at the very bottom
            new_lines.append('\n# Added by Lite-cPanel for Jailed SFTP\n')
            new_lines.append('Match Group lite_sftp\n')
            new_lines.append('    ChrootDirectory /var/www\n')
            new_lines.append('    ForceCommand internal-sftp\n')
            new_lines.append('    AllowTcpForwarding no\n')
            new_lines.append('    X11Forwarding no\n')
            new_lines.append('    PasswordAuthentication yes\n')
            
        with open(sshd_path, 'w') as f:
            f.writelines(new_lines)
            
        subprocess.run(['systemctl', 'restart', 'ssh'], check=True)
        return True, f"SFTP {'enabled (Jailed)' if enable else 'disabled'} successfully."
    except Exception as e:
        return False, str(e)

def toggle_ftp_user_status(username, enable=True):
    try:
        date_val = "0" if enable else "19700101"
        run_system_command(['pure-pw', 'usermod', username, '-X', date_val, '-m'])
        user_exists = False
        try:
            pwd.getpwnam(username)
            user_exists = True
        except KeyError:
            user_exists = False
        if enable:
            # Robustly get the directory
            directory = get_user_directory(username)
            ensure_system_user(username, directory)
            
            # THE MAGIC FIX: Add the web server (www-data) to the USER'S group
            run_system_command(['usermod', '-aG', username, 'www-data'])
            
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)

            # Ownership: User owns their folder, group is their PRIVATE group
            run_system_command(['chown', f'{username}:{username}', directory])
            run_system_command(['chmod', '770', directory])
            run_system_command(['chmod', 'g+s', directory])

            # Recursively ensure everything inside is manageable by user and web server
            run_system_command(['find', directory, '-mindepth', '1', '-exec', 'chown', f'{username}:{username}', '{}', '+'])
            run_system_command(['find', directory, '-mindepth', '1', '-type', 'd', '-exec', 'chmod', '770', '{}', '+'])
            run_system_command(['find', directory, '-mindepth', '1', '-type', 'f', '-exec', 'chmod', '660', '{}', '+'])
            run_system_command(['find', directory, '-mindepth', '1', '-type', 'd', '-exec', 'chmod', 'g+s', '{}', '+'])
            
            # Ensure the parent (/var/www) is 755 (SSH requirement for jail root)
            run_system_command(['chown', 'root:root', '/var/www'])
            run_system_command(['chmod', '755', '/var/www'])

            try:
                run_system_command(['systemctl', 'restart', 'apache2'])
            except: pass
            
            try:
                run_system_command(['systemctl', 'restart', 'nginx'])
            except: pass
            
            # Restart PHP-FPM
            try:
                php_versions = subprocess.run("ls /var/run/php/php*-fpm.sock 2>/dev/null | cut -d- -f1 | rev | cut -d/ -f1 | rev", shell=True, capture_output=True, text=True).stdout.splitlines()
                for v in php_versions:
                    run_system_command(['systemctl', 'restart', f'{v}-fpm'])
            except: pass
            
            # Unlock system user
            res = run_system_command(['passwd', '-u', username])
            if res.returncode != 0:
                # If unlock fails (common if no password is set), set a default one and try again
                run_system_command(['chpasswd'], input_str=f"{username}:SetPasswordInPanel123!\n")
                run_system_command(['passwd', '-u', username])
        else:
            if user_exists:
                run_system_command(['passwd', '-l', username])
                run_system_command(['gpasswd', '-d', username, 'lite_sftp'])
                run_system_command(['gpasswd', '-d', 'www-data', username])
        return True, f"User {username} {'enabled (Jailed)' if enable else 'disabled'}."
    except Exception as e:
        return False, str(e)
