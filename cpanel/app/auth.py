import os
import pwd
import spwd
import crypt
from functools import wraps
from flask import session, redirect, url_for, flash, request

def check_system_password(username, password):
    """
    Check if the provided username and password match a local system user (like root).
    Note: The script must run with root privileges to read /etc/shadow.
    """
    try:
        # For development purposes, if running as a non-root user and we just want to test
        # We'll allow a fallback admin/password login or simply check if not root.
        if os.geteuid() != 0:
            if username == 'admin' and password == 'password':
                return True
            return False

        # If running as root, authenticate against system users
        shadow_entry = spwd.getspnam(username)
        hashed_password = shadow_entry.sp_pwdp

        # crypt.crypt generates a hash using the same salt found in the hashed_password
        if crypt.crypt(password, hashed_password) == hashed_password:
            return True
    except KeyError:
        # User not found
        pass
    except PermissionError:
        # Cannot read shadow file
        pass

    return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function
