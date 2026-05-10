from flask import Blueprint, render_template, request, redirect, url_for, session, flash, Response, stream_with_context
import json
from auth import login_required
from security_mgr import validate_input, is_safe_path

from domains_mgr import get_virtual_hosts
from wordpress_mgr import get_installed_wordpress, install_wordpress_generator, delete_wordpress

wordpress_bp = Blueprint('wordpress', __name__)

@wordpress_bp.route('/wordpress', methods=['GET', 'POST'])
@login_required
def wordpress():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'install_wp':
            domain = request.form.get('domain')
            target_path = request.form.get('target_path', '').strip()
            
            v, e = validate_input(domain, 'domain')
            if not v:
                 return Response(json.dumps({"progress": 100, "message": f"Validation failed: {e}", "error": True}) + "\n", mimetype='application/x-ndjson')

            return Response(stream_with_context(install_wordpress_generator(domain, target_path)), mimetype='application/x-ndjson')
            
        elif action == 'delete_wp':
            path = request.form.get('path')
            
            if not is_safe_path(path):
                flash("Access denied: Invalid deletion path.", "danger")
                return redirect(url_for('wordpress.wordpress'))

            success, msg = delete_wordpress(path)
            flash(msg, 'success' if success else 'danger')
            return redirect(url_for('wordpress.wordpress'))

    domains = get_virtual_hosts('admin', None)
    wp_installs = get_installed_wordpress(domains)
    return render_template('wordpress.html', domains=domains, wp_installs=wp_installs)
