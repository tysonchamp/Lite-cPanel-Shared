from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from auth import login_required
from security_mgr import validate_input, is_safe_path, python_grep, check_dns_resolution
import os
import subprocess
import logging

from domains_mgr import get_virtual_hosts, add_virtual_host, toggle_virtual_host, get_port80_webserver

domains_bp = Blueprint('domains', __name__)

@domains_bp.route('/domains', methods=['GET', 'POST'])
@login_required
def domains():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            domain = request.form.get('domain')
            
            # SECURITY: Domain regex validation
            v, e = validate_input(domain, 'domain')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('domains.domains'))

            success, message = add_virtual_host(domain, session.get('role'), session.get('username'))
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'toggle':
            domain = request.form.get('domain')
            enable_str = request.form.get('enable')
            enable = enable_str.lower() == 'true'

            success, message = toggle_virtual_host(domain, enable)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'ssl_generate':
            domain = request.form.get('domain')
            servers = request.form.get('servers', '')
            try:
                # Automatically choose plugin based on which webserver is on port 80
                detected = get_port80_webserver(domain)
                if detected == 'nginx':
                    plugin = '--nginx'
                elif detected == 'apache':
                    plugin = '--apache'
                else:
                    plugin = '--nginx' if 'Nginx' in servers and 'Apache' not in servers else '--apache'
                
                logging.info(f"Generating SSL for {domain} using {plugin} (detected: {detected})")
                
                domain_args = ['-d', domain]
                if check_dns_resolution(f"www.{domain}"):
                    domain_args.extend(['-d', f'www.{domain}'])
                else:
                    logging.info(f"Skipping www.{domain} as it does not resolve in DNS.")

                cmd = ['certbot', plugin] + domain_args + ['--non-interactive', '--agree-tos', '-m', f'admin@{domain}']
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    flash(f"SSL Certificate generated successfully for {domain}!", 'success')
                else:
                    flash(f"SSL Generation failed: {result.stderr}", 'danger')
            except Exception as e:
                flash(f"Error during SSL setup: {str(e)}", 'danger')

        elif action == 'ssl_renew':
            try:
                result = subprocess.run(['certbot', 'renew', '--non-interactive'], capture_output=True, text=True)
                if result.returncode == 0:
                    flash("Certificates renewed successfully. " + result.stdout, 'success')
                else:
                    flash(f"Renewal issue: {result.stderr}", 'danger')
            except Exception as e:
                flash(f"Error during renewal: {str(e)}", 'danger')

        return redirect(url_for('domains.domains'))

    vhosts = get_virtual_hosts(session.get('role'), session.get('username'))
    return render_template('domains.html', vhosts=vhosts)

@domains_bp.route('/domains/logs/<domain>')
@login_required
def domain_logs(domain):
    """Page view: discover all log files for the given domain."""
    available_logs = {}

    for suffix, label in [('_error.log', 'Apache Error'), ('_access.log', 'Apache Access'),
                          ('_ssl_error.log', 'Apache SSL Error'), ('_ssl_access.log', 'Apache SSL Access')]:
        path = f'/var/log/apache2/{domain}{suffix}'
        if os.path.exists(path):
            available_logs[label] = path

    for path, label in [('/var/log/apache2/error.log',  'Apache Error (Global)'),
                        ('/var/log/apache2/access.log', 'Apache Access (Global)')]:
        if os.path.exists(path):
            available_logs[label] = f'__apache_filter__:{path}'

    for suffix, label in [('_error.log', 'Nginx Error'), ('_access.log', 'Nginx Access')]:
        path = f'/var/log/nginx/{domain}{suffix}'
        if os.path.exists(path):
            available_logs[label] = path

    for path, label in [('/var/log/nginx/error.log',  'Nginx Error (Global, filtered)'),
                        ('/var/log/nginx/access.log', 'Nginx Access (Global, filtered)')]:
        if os.path.exists(path):
            available_logs[label] = f'__nginx_filter__:{path}'

    return render_template('domain_logs.html', domain=domain, available_logs=available_logs)

@domains_bp.route('/domains/logs/<domain>/fetch')
@login_required
def domain_logs_fetch(domain):
    """JSON API: return the last N lines of a given log file for this domain."""
    log_key  = request.args.get('log', '')
    lines    = request.args.get('lines', '100')

    available_logs = {}
    for suffix, label in [('_error.log', 'Apache Error'), ('_access.log', 'Apache Access'),
                          ('_ssl_error.log', 'Apache SSL Error'), ('_ssl_access.log', 'Apache SSL Access')]:
        path = f'/var/log/apache2/{domain}{suffix}'
        if os.path.exists(path):
            available_logs[label] = path
    for path, label in [('/var/log/apache2/error.log',  'Apache Error (Global)'),
                        ('/var/log/apache2/access.log', 'Apache Access (Global)')]:
        if os.path.exists(path):
            available_logs[label] = f'__apache_filter__:{path}'
    for suffix, label in [('_error.log', 'Nginx Error'), ('_access.log', 'Nginx Access')]:
        path = f'/var/log/nginx/{domain}{suffix}'
        if os.path.exists(path):
            available_logs[label] = path
    for path, label in [('/var/log/nginx/error.log',  'Nginx Error (Global, filtered)'),
                        ('/var/log/nginx/access.log', 'Nginx Access (Global, filtered)')]:
        if os.path.exists(path):
            available_logs[label] = f'__nginx_filter__:{path}'

    if log_key not in available_logs:
        return jsonify({'error': 'Invalid log file selected.'}), 400

    try:
        lines_int = max(1, min(int(lines), 5000))
    except ValueError:
        lines_int = 100

    path = available_logs.get(log_key)
    if not path:
         return jsonify({'error': 'Invalid log file.'}), 400

    real_path = path.split(':', 1)[1] if ':' in path else path
    
    if not is_safe_path(real_path):
        return jsonify({'error': 'Access denied: Path is not in the allowed log directory list.'}), 403

    try:
        content = python_grep(real_path, domain, lines_int)
        if not content or not content.strip():
            content = '(Log is empty or has no matching entries yet.)'
        return jsonify({'content': content, 'path': real_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@domains_bp.route('/domains/edit-vhost')
@login_required
def edit_vhost():
    """Dedicated editor for Apache/Nginx vhost config files, accessed from the Domains page."""
    filepath = request.args.get('filepath', '')
    if not filepath:
        flash("No file specified.", "danger")
        return redirect(url_for('domains.domains'))

    source = request.args.get('source', 'domains')
    back_url = url_for('nextjs.nextjs') if source == 'nextjs' else url_for('domains.domains')
    back_label = 'Back to Next.js Apps' if source == 'nextjs' else 'Back to Domains'

    if not is_safe_path(filepath):
        flash("Access denied: that file is not in an allowed directory.", "danger")
        return redirect(back_url)

    if not os.path.exists(filepath):
        flash(f"File not found: {filepath}", "danger")
        return redirect(back_url)

    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except Exception as e:
        flash(str(e), "danger")
        return redirect(back_url)

    return render_template('edit_config.html', filepath=filepath, content=content,
                           back_url=back_url, back_label=back_label,
                           save_url=url_for('domains.save_vhost', source=source))

@domains_bp.route('/domains/save-vhost', methods=['POST'])
@login_required
def save_vhost():
    """Save handler for vhost files edited from the Domains page."""
    filepath = request.form.get('filepath', '')
    content  = request.form.get('content', '')

    source = request.args.get('source', 'domains')
    back_url = url_for('nextjs.nextjs') if source == 'nextjs' else url_for('domains.domains')

    if not is_safe_path(filepath):
        flash("Access denied: cannot save to that directory.", "danger")
        return redirect(back_url)

    try:
        with open(filepath, 'w') as f:
            f.write(content)
        if 'apache2' in filepath:
            subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)
        elif 'nginx' in filepath:
            subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        flash("Virtual host config saved and service reloaded.", "success")
    except Exception as e:
        flash(str(e), "danger")

    return redirect(back_url)
