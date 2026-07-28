from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from auth import login_required
from security_mgr import validate_input, check_dns_resolution
import subprocess
import logging

from nextjs_mgr import get_nextjs_apps, add_nextjs_app, toggle_nextjs_app, delete_nextjs_app
from domains_mgr import get_port80_webserver
from process_mgr import is_pm2_installed, start_nextjs_app, manage_process, run_npm_command, list_processes, get_process_logs
from hosting_mgr import allocate_nextjs_port, get_nextjs_port, get_user_data

nextjs_bp = Blueprint('nextjs', __name__)

@nextjs_bp.route('/nextjs', methods=['GET', 'POST'])
@login_required
def nextjs():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            domain = request.form.get('domain')
            
            v, e = validate_input(domain, 'domain')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('nextjs.nextjs'))

            port = allocate_nextjs_port(domain)

            success, message = add_nextjs_app(domain, port, session.get('role'), session.get('username'))
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'toggle':
            domain = request.form.get('domain')
            enable_str = request.form.get('enable')
            enable = enable_str.lower() == 'true'

            success, message = toggle_nextjs_app(domain, enable)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'delete':
            domain = request.form.get('domain')
            success, message = delete_nextjs_app(domain)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'ssl_generate':
            domain = request.form.get('domain')
            servers = request.form.get('servers', '')
            try:
                detected = get_port80_webserver(domain)
                if detected == 'nginx':
                    plugin = '--nginx'
                elif detected == 'apache':
                    plugin = '--apache'
                else:
                    plugin = '--nginx' if 'Nginx' in servers and 'Apache' not in servers else '--apache'
                
                logging.info(f"Generating SSL (Next.js) for {domain} using {plugin} (detected: {detected})")
                
                domain_args = ['-d', domain]
                if check_dns_resolution(f"www.{domain}"):
                    domain_args.extend(['-d', f'www.{domain}'])
                
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

        return redirect(url_for('nextjs.nextjs'))

    nextjs_apps = get_nextjs_apps(session.get('role'), session.get('username'))
    return render_template('nextjs.html', nextjs_apps=nextjs_apps)

@nextjs_bp.route('/processes', methods=['GET', 'POST'])
@login_required
def process_manager():
    pm2_ready = is_pm2_installed()
    configured_apps = get_nextjs_apps(session.get('role'), session.get('username'))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add_app':
            domain = request.form.get('domain')
            if not domain:
                flash("Domain is required.", "danger")
                return redirect(url_for('nextjs.process_manager'))
                
            port = get_nextjs_port(domain)
            if not port:
                flash("Port not found for this domain. Did you add it in the Next.js apps section?", "danger")
                return redirect(url_for('nextjs.process_manager'))

            username = session.get('username')
            role = session.get('role')
            
            # Admins starting apps that belong to users?
            # For now, let's just determine directory for the logged-in user.
            user_data = get_user_data(username)
            if user_data and domain == user_data.get('main_domain'):
                path = f"/home/{username}/public_html"
            else:
                path = f"/home/{username}/public_html/{domain}"
                
            # If admin is testing, they might not have a /home/admin/public_html, but let's assume they manage their own
            if role == 'admin' and not user_data:
                 path = f"/var/www/{domain}" # Fallback for admin standalone domains

            success, msg = start_nextjs_app(path, domain, port)
            flash(msg, 'success' if success else 'danger')
            
        elif action in ['stop', 'restart', 'delete', 'start']:
            name = request.form.get('name')
            success, msg = manage_process(action, name)
            flash(msg, 'success' if success else 'danger')
            
        elif action in ['npm_install', 'npm_build']:
            domain = request.form.get('domain')
            username = session.get('username')
            role = session.get('role')
            
            user_data = get_user_data(username)
            if user_data and domain == user_data.get('main_domain'):
                path = f"/home/{username}/public_html"
            else:
                path = f"/home/{username}/public_html/{domain}"
                
            if role == 'admin' and not user_data:
                 path = f"/var/www/{domain}"

            cmd = 'install' if action == 'npm_install' else 'run build'
            success, msg = run_npm_command(path, cmd)
            flash(msg, 'success' if success else 'danger')

        return redirect(url_for('nextjs.process_manager'))

    processes = list_processes()
    return render_template('processes.html', pm2_ready=pm2_ready, processes=processes, configured_apps=configured_apps)

@nextjs_bp.route('/api/processes/logs/<name>')
@login_required
def api_process_logs(name):
    logs = get_process_logs(name)
    return jsonify({'logs': logs})
