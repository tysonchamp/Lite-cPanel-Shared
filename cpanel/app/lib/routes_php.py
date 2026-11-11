import json
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, Response, stream_with_context, jsonify
from auth import login_required
from security_mgr import validate_input
from php_mgr import (
    SUPPORTED_PHP_VERSIONS,
    DEFAULT_CORE_EXTENSIONS,
    INSTALLABLE_EXTENSIONS,
    INI_DIRECTIVES,
    get_webserver_type,
    get_default_php_version,
    get_installed_php_versions,
    get_php_extensions,
    get_php_domains,
    set_domain_php_version,
    set_default_php_version,
    manage_php_fpm_service,
    install_php_generator,
    uninstall_php_version,
    install_php_extension,
    get_php_ini_settings,
    save_php_ini_settings,
)

php_bp = Blueprint('php', __name__)


@php_bp.route('/php', methods=['GET', 'POST'])
@login_required
def php_manager():
    role = session.get('role')
    username = session.get('username')
    is_admin = (role == 'admin')

    if request.method == 'POST':
        action = request.form.get('action', '').strip()

        if action == 'install_php':
            if not is_admin:
                return Response(
                    json.dumps({
                        'progress': 100,
                        'message': 'Access denied. Only the root user can install PHP versions.',
                        'error': True
                    }) + "\n",
                    status=403,
                    mimetype='application/x-ndjson'
                )
            version = request.form.get('version', '').strip()
            extensions = request.form.getlist('extensions')
            return Response(
                stream_with_context(install_php_generator(version, extensions)),
                mimetype='application/x-ndjson'
            )

        elif action == 'set_domain_php':
            domain = request.form.get('domain', '').strip()
            php_version = request.form.get('php_version', '').strip()
            v, e = validate_input(domain, 'domain')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('php.php_manager'))

            ok, msg = set_domain_php_version(domain, php_version, role, username)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager'))

        elif action == 'bulk_set_domain_php':
            selected_domains = request.form.getlist('domains')
            php_version = request.form.get('php_version', '').strip()
            if not selected_domains:
                flash("Please select at least one domain.", "warning")
                return redirect(url_for('php.php_manager'))

            updated = 0
            errors = []
            for d in selected_domains:
                d = d.strip()
                v, e = validate_input(d, 'domain')
                if not v:
                    errors.append(f"{d}: {e}")
                    continue
                ok, msg = set_domain_php_version(d, php_version, role, username)
                if ok:
                    updated += 1
                else:
                    errors.append(f"{d}: {msg}")

            if updated:
                flash(f"Updated PHP version for {updated} domain(s).", "success")
            if errors:
                flash("Some domains failed: " + " | ".join(errors), "danger")
            return redirect(url_for('php.php_manager'))

        elif action == 'uninstall_php':
            if not is_admin:
                flash("Access denied. Only the root user can uninstall PHP versions.", "danger")
                return redirect(url_for('php.php_manager'))
            version = request.form.get('version', '').strip()
            ok, msg = uninstall_php_version(version)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager', tab='versions'))

        elif action == 'set_default_php':
            if not is_admin:
                flash("Access denied. Only the root user can change the system default PHP version.", "danger")
                return redirect(url_for('php.php_manager'))
            version = request.form.get('version', '').strip()
            ok, msg = set_default_php_version(version)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager', tab='versions'))

        elif action == 'service_action':
            if not is_admin:
                flash("Access denied. Only the root user can manage PHP-FPM services.", "danger")
                return redirect(url_for('php.php_manager'))
            version = request.form.get('version', '').strip()
            svc_action = request.form.get('svc_action', '').strip()
            ok, msg = manage_php_fpm_service(version, svc_action)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager', tab='versions'))

        elif action == 'install_extension':
            if not is_admin:
                flash("Access denied. Only the root user can install PHP extensions.", "danger")
                return redirect(url_for('php.php_manager'))
            version = request.form.get('version', '').strip()
            extension = request.form.get('extension', '').strip()
            ok, msg = install_php_extension(version, extension)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager', tab='versions'))

        elif action == 'save_ini':
            if not is_admin:
                flash("Access denied. Only the root user can edit global PHP INI settings.", "danger")
                return redirect(url_for('php.php_manager'))
            version = request.form.get('version', '').strip()
            sapi = request.form.get('sapi', 'fpm').strip()
            submitted = {d['key']: request.form.get(d['key'], '') for d in INI_DIRECTIVES}
            ok, msg = save_php_ini_settings(version, sapi, submitted)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('php.php_manager', tab='ini', ini_ver=version, ini_sapi=sapi))

        return redirect(url_for('php.php_manager'))

    installed_versions = get_installed_php_versions()
    fpm_versions = [v for v in installed_versions if v['has_fpm']]
    installed_fpm_set = {v['version'] for v in fpm_versions}
    installable_versions = [v for v in SUPPORTED_PHP_VERSIONS if v not in installed_fpm_set]
    default_php = get_default_php_version()
    domains = get_php_domains(role, username)
    webserver_type = get_webserver_type()

    active_tab = request.args.get('tab', 'domains')
    if not is_admin or active_tab not in ('domains', 'versions', 'ini'):
        active_tab = 'domains'

    selected_ini_ver = ''
    selected_ini_sapi = 'fpm'
    ini_path = None
    ini_values = {}

    if is_admin and installed_versions:
        ver_map = {v['version']: v for v in installed_versions}
        req_ver = request.args.get('ini_ver', '').strip()
        if req_ver in ver_map:
            selected_ini_ver = req_ver
        elif default_php in ver_map:
            selected_ini_ver = default_php
        else:
            selected_ini_ver = installed_versions[0]['version']

        available_sapis = list(ver_map[selected_ini_ver]['ini_files'].keys())
        req_sapi = request.args.get('ini_sapi', '').strip()
        if req_sapi in available_sapis:
            selected_ini_sapi = req_sapi
        elif 'fpm' in available_sapis:
            selected_ini_sapi = 'fpm'
        elif available_sapis:
            selected_ini_sapi = available_sapis[0]

        ini_path, ini_values = get_php_ini_settings(selected_ini_ver, selected_ini_sapi)

    return render_template(
        'php.html',
        is_admin=is_admin,
        webserver_type=webserver_type,
        default_php=default_php,
        installed_versions=installed_versions,
        fpm_versions=fpm_versions,
        installable_versions=installable_versions,
        supported_versions=SUPPORTED_PHP_VERSIONS,
        core_extensions=DEFAULT_CORE_EXTENSIONS,
        installable_extensions=INSTALLABLE_EXTENSIONS,
        domains=domains,
        active_tab=active_tab,
        ini_directives=INI_DIRECTIVES,
        selected_ini_ver=selected_ini_ver,
        selected_ini_sapi=selected_ini_sapi,
        ini_path=ini_path,
        ini_values=ini_values,
    )


@php_bp.route('/api/php/extensions/<version>')
@login_required
def api_php_extensions(version):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Access denied.'}), 403
    modules = get_php_extensions(version)
    return jsonify({'version': version, 'modules': modules})
