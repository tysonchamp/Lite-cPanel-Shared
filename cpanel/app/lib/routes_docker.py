from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required
from security_mgr import validate_input, check_dns_resolution
import logging
import subprocess

from docker_mgr import (
    is_docker_installed, list_containers, manage_container, run_container,
    get_docker_apps, add_docker_app, toggle_docker_app, delete_docker_app, run_docker_compose
)
from hosting_mgr import can_add_resource
from domains_mgr import get_port80_webserver

docker_bp = Blueprint('docker', __name__)

@docker_bp.route('/docker', methods=['GET', 'POST'])
@login_required
def docker_route():
    if request.method == 'POST':
        action = request.form.get('action')

        if action in ['start', 'stop', 'restart', 'rm']:
            container_id = request.form.get('container_id')
            success, message = manage_container(action, container_id)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'run':
            image = request.form.get('image')
            name = request.form.get('name', '')
            port_mapping = request.form.get('port_mapping', '')
            env_vars = request.form.get('env_vars', '')
            
            if not image:
                flash("Image name is required.", "danger")
            else:
                success, message = run_container(image, name, port_mapping, env_vars)
                if success:
                    flash(message, 'success')
                else:
                    flash(message, 'danger')

        elif action == 'proxy_add':
            domain = request.form.get('domain')
            port_str = request.form.get('port')
            
            v, e = validate_input(domain, 'domain')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('docker.docker_route'))
                
            if not port_str or not port_str.isdigit():
                flash("Valid port number is required.", "danger")
                return redirect(url_for('docker.docker_route'))
                
            port = int(port_str)
            role = session.get('role')
            username = session.get('username')
            
            if role == 'user':
                can_add, err = can_add_resource(username, 'docker_apps')
                if not can_add:
                    flash(err, 'danger')
                    return redirect(url_for('docker.docker_route'))

            success, message = add_docker_app(domain, port, role, username)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'proxy_toggle':
            domain = request.form.get('domain')
            enable_str = request.form.get('enable')
            enable = enable_str.lower() == 'true'

            success, message = toggle_docker_app(domain, enable)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')

        elif action == 'proxy_delete':
            domain = request.form.get('domain')
            role = session.get('role')
            username = session.get('username')
            success, message = delete_docker_app(domain, role, username)
            if success:
                flash(message, 'success')
            else:
                flash(message, 'danger')
                
        elif action == 'proxy_compose_up':
            domain = request.form.get('domain')
            role = session.get('role')
            username = session.get('username')
            success, message = run_docker_compose(domain, role, username)
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
                
                logging.info(f"Generating SSL (Docker) for {domain} using {plugin} (detected: {detected})")
                
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

        return redirect(url_for('docker.docker_route'))

    docker_installed = is_docker_installed()
    containers = list_containers() if docker_installed else []
    docker_apps = get_docker_apps(session.get('role'), session.get('username'))
    
    return render_template('docker.html', 
                           docker_installed=docker_installed, 
                           containers=containers, 
                           docker_apps=docker_apps)
