from flask import Blueprint, render_template, request, redirect, url_for, session, flash, Response, stream_with_context
from auth import login_required
import socket

from cron_mgr import get_cron_jobs, add_cron_job, delete_cron_job, enable_ssl_renewal
from backup_mgr import get_backup_settings, save_backup_settings, trigger_manual_backup, get_local_backups, delete_local_backup
from csf_mgr import (check_csf_installed, get_csf_status, csf_action, csf_ip_action, csf_temp_ip_action,
                           get_csf_file, save_csf_file, get_csf_temp_entries,
                           get_open_ports, get_csf_conf_settings, save_csf_conf_key, remove_from_csf_file)
from modsec_mgr import (check_modsec_installed, get_modsec_status, set_modsec_status,
                        get_modsec_profiles, get_domains_modsec_status, toggle_domain_modsec,
                        get_modsec_config, save_modsec_config, get_modsec_audit_log,
                        activate_modsec_profile, test_modsec_config, webserver_action, install_modsecurity_generator)
from settings_mgr import (get_system_logs, get_editable_configs, read_config_file, save_config_file,
                          set_server_hostname, generate_hostname_ssl, enable_panel_ssl)
from updater_mgr import get_settings, get_version_info, perform_update, restart_service, save_settings

system_bp = Blueprint('system', __name__)

@system_bp.route('/terminal')
@login_required
def terminal():
    return render_template('terminal.html')

@system_bp.route('/cron', methods=['GET', 'POST'])
@login_required
def cron():
    target_user = None if session.get('role') == 'admin' else session.get('username')

    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            schedule = request.form.get('schedule', '')
            command = request.form.get('command', '')
            success, msg = add_cron_job(schedule, command, target_user)
            flash(msg, "success" if success else "danger")
            
        elif action == 'delete':
            index = request.form.get('index')
            success, msg = delete_cron_job(index, target_user)
            flash(msg, "success" if success else "danger")
            
        elif action == 'setup_ssl':
            if session.get('role') != 'admin':
                flash("Access denied.", "danger")
            else:
                success, msg = enable_ssl_renewal()
                flash(msg, "success" if success else "warning")
            
        return redirect(url_for('system.cron'))

    cron_jobs = get_cron_jobs(target_user)
    return render_template('cron.html', cron_jobs=cron_jobs)

@system_bp.route('/backups', methods=['GET', 'POST'])
@login_required
def backups():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'save_settings':
            settings = {
                'local_enabled': request.form.get('local_enabled') == 'yes',
                'ftp_enabled': request.form.get('ftp_enabled') == 'yes',
                'ftp_host': request.form.get('ftp_host', ''),
                'ftp_port': request.form.get('ftp_port', '21'),
                'ftp_user': request.form.get('ftp_user', ''),
                'ftp_pass': request.form.get('ftp_pass', ''),
                'ftp_path': request.form.get('ftp_path', '/'),
                's3_enabled': request.form.get('s3_enabled') == 'yes',
                's3_endpoint': request.form.get('s3_endpoint', ''),
                's3_access_key': request.form.get('s3_access_key', ''),
                's3_secret_key': request.form.get('s3_secret_key', ''),
                's3_bucket': request.form.get('s3_bucket', ''),
                's3_region': request.form.get('s3_region', ''),
                'retention_days': int(request.form.get('retention_days', 7)),
                'schedule': request.form.get('schedule', '').strip()
            }
            success, msg = save_backup_settings(settings)
            flash(msg, "success" if success else "danger")
            
        elif action == 'trigger_backup':
            success, msg = trigger_manual_backup()
            flash(msg, "success" if success else "danger")
            
        elif action == 'delete_backup':
            filename = request.form.get('filename')
            success, msg = delete_local_backup(filename)
            flash(msg, "success" if success else "danger")
            
        return redirect(url_for('system.backups'))

    settings = get_backup_settings()
    local_backups = get_local_backups()
    return render_template('backups.html', settings=settings, local_backups=local_backups)

@system_bp.route('/firewall', methods=['GET', 'POST'])
@login_required
def firewall():
    csf_installed = check_csf_installed()

    if request.method == 'POST':
        action = request.form.get('action')

        if action in ['start', 'stop', 'restart']:
            success, message = csf_action(action)
            flash(message, 'success' if success else 'danger')

        elif action in ['allow_ip', 'deny_ip', 'unallow_ip', 'undeny_ip']:
            ip = request.form.get('ip')
            comment = request.form.get('comment', '')
            action_type = action.split('_')[0]
            success, message = csf_ip_action(action_type, ip, comment)
            flash(message, 'success' if success else 'danger')

        elif action == 'temp_ip':
            ip = request.form.get('ip')
            type_ = request.form.get('type')
            ttl = request.form.get('ttl')
            ports = request.form.get('ports', '')
            direction = request.form.get('direction', '')
            comment = request.form.get('comment', '')
            
            success, message = csf_temp_ip_action(type_, ip, ttl, ports, direction, comment)
            flash(message, 'success' if success else 'danger')

        elif action == 'save_csf_file':
            file_type = request.form.get('file_type')
            content = request.form.get('content')
            success, message = save_csf_file(file_type, content)
            flash(message, 'success' if success else 'danger')

        elif action == 'save_csf_conf_key':
            key   = request.form.get('conf_key')
            value = request.form.get('conf_value')
            success, message = save_csf_conf_key(key, value)
            flash(message, 'success' if success else 'danger')

        elif action == 'remove_rule':
            file_type = request.form.get('file_type')
            rule_raw = request.form.get('rule_raw')
            success, message = remove_from_csf_file(file_type, rule_raw)
            flash(message, 'success' if success else 'danger')

        return redirect(url_for('system.firewall'))

    from csf_mgr import get_parsed_csf_file
    context = {
        'csf_installed':    csf_installed,
        'csf_status':       get_csf_status() if csf_installed else None,
        'csf_allow_file':   get_csf_file('allow')  if csf_installed else "",
        'csf_deny_file':    get_csf_file('deny')   if csf_installed else "",
        'csf_ignore_file':  get_csf_file('ignore') if csf_installed else "",
        'csf_pignore_file': get_csf_file('pignore') if csf_installed else "",
        'csf_regex_file':   get_csf_file('regex') if csf_installed else "",
        'csf_config_file':  get_csf_file('config') if csf_installed else "",
        'parsed_allow':     get_parsed_csf_file('allow') if csf_installed else [],
        'parsed_deny':      get_parsed_csf_file('deny') if csf_installed else [],
        'csf_temp':         get_csf_temp_entries() if csf_installed else [],
        'csf_ports':        get_open_ports()        if csf_installed else {},
        'csf_conf_settings': get_csf_conf_settings() if csf_installed else [],
    }
    return render_template('firewall.html', **context)

@system_bp.route('/modsecurity', methods=['GET', 'POST'])
@login_required
def modsecurity():
    modsec_installed = check_modsec_installed()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'install_modsec':
            return Response(stream_with_context(install_modsecurity_generator()), mimetype='application/x-ndjson')

        if action == 'set_modsec_status':
            status = request.form.get('status')
            success, message = set_modsec_status(status)
            flash(message, 'success' if success else 'danger')
        
        elif action == 'toggle_domain':
            domain = request.form.get('domain')
            enabled = request.form.get('enabled') == 'true'
            success, message = toggle_domain_modsec(domain, enabled)
            flash(message, 'success' if success else 'danger')
            
        elif action == 'save_config':
            file_type = request.form.get('file_type')
            content = request.form.get('content')
            success, message = save_modsec_config(file_type, content)
            flash(message, 'success' if success else 'danger')
        
        elif action == 'activate_profile':
            profile_id = request.form.get('profile_id')
            success, message = activate_modsec_profile(profile_id)
            flash(message, 'success' if success else 'danger')

        elif action == 'test_config':
            success, message = test_modsec_config()
            flash(message, 'success' if success else 'danger')

        elif action in ['reload', 'restart']:
            success, message = webserver_action(action)
            flash(message, 'success' if success else 'danger')

        return redirect(url_for('system.modsecurity'))

    domain_filter = request.args.get('domain')

    context = {
        'modsec_installed': modsec_installed,
        'modsec_status':    get_modsec_status()     if modsec_installed else None,
        'modsec_log':       get_modsec_audit_log(domain_filter) if modsec_installed else "",
        'modsec_profiles':  get_modsec_profiles()   if modsec_installed else [],
        'domain_modsec':    get_domains_modsec_status() if modsec_installed else [],
        'main_config':      get_modsec_config('main')   if modsec_installed else "",
        'custom_rules':     get_modsec_config('custom') if modsec_installed else "",
        'disabled_rules':   get_modsec_config('disabled') if modsec_installed else "",
        'current_filter':   domain_filter
    }
    return render_template('modsecurity.html', **context)

@system_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_config':
            filepath = request.form.get('filepath')
            content = request.form.get('content')
            success, message = save_config_file(filepath, content)
            flash(message, 'success' if success else 'danger')

        elif action == 'update_settings':
            auto_up = request.form.get('auto_update') == 'on'
            s = get_settings()
            s['auto_update'] = auto_up
            save_settings(s)
            flash("Updater settings saved.", "success")

        elif action == 'check_update':
            info = get_version_info()
            if info.get('update_available'):
                flash(f"Update available: {info['remote']}. Click 'Update Now' to apply.", "info")
            else:
                flash("System is up to date.", "success")

        elif action == 'apply_update':
            success, msg = perform_update()
            if success:
                flash("Update applied! Restarting Lite cPanel...", "success")
                restart_service()
            else:
                flash(msg, "danger")

        elif action == 'update_hostname':
            new_hostname = request.form.get('hostname')
            success, msg = set_server_hostname(new_hostname)
            flash(msg, "success" if success else "danger")

        elif action == 'generate_hostname_ssl':
            hostname = request.form.get('hostname')
            success, msg = generate_hostname_ssl(hostname)
            flash(msg, "success" if success else "danger")

        elif action == 'enable_panel_ssl':
            hostname = request.form.get('hostname')
            success, msg = enable_panel_ssl(hostname)
            flash(msg, "success" if success else "danger")

        return redirect(url_for('system.settings'))

    logs = get_system_logs()
    configs = get_editable_configs()
    
    ver_info = get_version_info()
    updater_settings = get_settings()
    current_hostname = socket.gethostname()

    return render_template('settings.html', 
                           logs=logs, 
                           configs=configs, 
                           ver_info=ver_info, 
                           updater_settings=updater_settings,
                           current_hostname=current_hostname)

@system_bp.route('/settings/edit-config')
@login_required
def edit_config():
    filepath = request.args.get('filepath')
    if not filepath:
        flash("No file specified.", "danger")
        return redirect(url_for('system.settings'))

    success, content = read_config_file(filepath)
    if not success:
        flash(content, "danger")
        return redirect(url_for('system.settings'))
    return render_template('edit_config.html', filepath=filepath, content=content,
                           back_url=url_for('system.settings'), back_label='Back to Settings')
