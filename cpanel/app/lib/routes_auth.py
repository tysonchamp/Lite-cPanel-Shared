from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import check_system_password
from hosting_mgr import get_users
import subprocess
import logging

auth_bp = Blueprint('auth', __name__)

auth_logger = logging.getLogger('cpanel_auth')

def log_auth_failure(username, ip):
    auth_logger.info(f"Failed login attempt for user {username} from {ip}")

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if check_system_password(username, password):
            # Auto-Whitelist IP in CSF (Temporary Allow for 1 Hour)
            try:
                # Robust IP detection
                user_ip = request.headers.get('CF-Connecting-IP') or \
                          request.headers.get('X-Real-IP') or \
                          request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
                
                # Run CSF and log result for debugging
                res = subprocess.run(['/usr/sbin/csf', '-ta', user_ip, '3600', f'cPanel Login: {username}'], capture_output=True, text=True)
                with open('/tmp/csf_debug.log', 'a') as f:
                    f.write(f"Login OK: User={username} | IP={user_ip} | Exit={res.returncode} | Out={res.stdout} | Err={res.stderr}\n")
            except Exception as e:
                with open('/tmp/csf_debug.log', 'a') as f:
                    f.write(f"Login Error: {str(e)}\n")

            session['logged_in'] = True
            session['username'] = username
            
            # Determine role
            users_db = get_users()
            if username in users_db:
                session['role'] = 'user'
            else:
                session['role'] = 'admin'
                
            flash('Logged in successfully!', 'success')

            # Prevent open redirects
            next_page = request.args.get('next')
            if not next_page or not next_page.startswith('/'):
                next_page = url_for('dashboard.dashboard')
            return redirect(next_page)
        else:
            # Capture real IP even if behind proxy
            user_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
            log_auth_failure(username, user_ip)
            flash('Invalid username or password', 'danger')
    return render_template('login.html')

@auth_bp.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('username', None)
    session.pop('role', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
