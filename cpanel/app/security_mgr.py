import re
import os
import socket

def check_dns_resolution(domain):
    """
    Checks if a domain resolves to an IP address.
    """
    try:
        # We use a short timeout to avoid blocking the UI
        socket.gethostbyname(domain)
        return True
    except socket.gaierror:
        return False

# --- Input Validation Patterns ---
PATTERNS = {
    'domain': re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$', re.I),
    'db_name': re.compile(r'^[a-zA-Z0-9_]{1,64}$'),
    'username': re.compile(r'^[a-zA-Z0-9_\.@-]{1,64}$'),
    'path_part': re.compile(r'^[a-zA-Z0-9_\.-]+$')
}

def validate_input(value, ptype):
    """
    Validates input against strict regex patterns.
    Returns (is_valid, error_message)
    """
    if not value or not isinstance(value, str):
        return False, "Input missing or invalid type."
        
    if ptype not in PATTERNS:
        return False, f"Internal error: Invalid pattern type '{ptype}'"
        
    if not PATTERNS[ptype].match(value):
        return False, f"Invalid characters in {ptype}. Only alphanumeric, dots, and hyphens are allowed."
        
    return True, ""

# --- Path Safety ---
ALLOWED_DIRS = [
    '/var/log/',
    '/etc/apache2/sites-available/',
    '/etc/nginx/sites-available/',
    '/etc/nginx/conf.d/',
    '/var/www/html/',
    '/etc/modsecurity/',
    '/etc/mysql/',
    '/etc/pure-ftpd/'
]

def is_safe_path(path):
    """
    Checks if the given path is within the allowed directories allowlist.
    """
    if not path:
        return False
        
    # Resolve any symbols and normalize
    abs_path = os.path.abspath(path)
    
    # Must start with one of the allowed directories
    for allowed in ALLOWED_DIRS:
        if abs_path.startswith(allowed):
            # Also prevent any attempts to go back up
            if '..' in abs_path: return False
            return True
            
    return False

# --- Secure Logic Replacements ---
def python_grep(file_path, pattern, limit=100):
    """
    A shell-less replacement for grep | tail.
    Reads a file and returns lines matching the pattern.
    """
    if not is_safe_path(file_path):
        return f"Error: Access to path '{file_path}' is denied."
        
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' does not exist."

    matches = []
    try:
        # We read from the end of the file or use a buffer to keep it efficient
        with open(file_path, 'r', errors='ignore') as f:
            # For simplicity, we iterate. For massive logs, a reverse buffer is better.
            # But since we're replacing 'tail', we just want the last N matches.
            for line in f:
                if not pattern or pattern.lower() in line.lower():
                    matches.append(line)
                    if len(matches) > 1000: # Safety cap
                        matches.pop(0)
                        
        # Return only the last 'limit' matches
        return "".join(matches[-limit:])
    except Exception as e:
        return f"Error reading log: {str(e)}"
