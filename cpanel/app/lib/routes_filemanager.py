from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from auth import login_required
from urllib.parse import quote
from security_mgr import validate_input
import os

from filemanager_mgr import (get_root_dir, create_folder, save_upload, compress_entries,
                             decompress_entry, rename_entry, delete_entry, write_file,
                             list_dir, is_archive, read_file, _safe_path)

from ftp_mgr import (check_pureftpd_installed, create_ftp_user, delete_ftp_user, 
                     change_ftp_password, get_ftp_users, get_sftp_status, 
                     toggle_sftp, toggle_ftp_user_status)

filemanager_bp = Blueprint('filemanager', __name__)

@filemanager_bp.route('/filemanager', methods=['GET', 'POST'], strict_slashes=False)
@login_required
def filemanager_route():
    root = get_root_dir()
    path = request.args.get('path', root)

    if request.method == 'POST':
        action = request.form.get('action')
        redirect_path = request.form.get('redirect_path', path)

        if action == 'mkdir':
            ok, msg = create_folder(request.form.get('path', path), request.form.get('name', ''))
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(request.form.get("path", path))}')

        elif action == 'upload':
            upload_path = request.form.get('path', path)
            files = request.files.getlist('file')
            for f in files:
                ok, msg = save_upload(upload_path, f)
                flash(msg, 'success' if ok else 'danger')
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': True, 'redirect': url_for('filemanager.filemanager_route') + f'?path={quote(upload_path)}'})
                
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(upload_path)}')

        elif action == 'compress':
            names = request.form.getlist('names')
            archive_name = request.form.get('archive_name', 'archive')
            fmt = request.form.get('fmt', 'zip')
            ok, msg = compress_entries(path, names, archive_name, fmt)
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(path)}')

        elif action == 'extract':
            ok, msg = decompress_entry(request.form.get('path', ''), os.path.dirname(request.form.get('path', '')))
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(redirect_path)}')

        elif action == 'rename':
            ok, msg = rename_entry(request.form.get('path', ''), request.form.get('new_name', ''))
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(redirect_path)}')

        elif action == 'delete':
            ok, msg = delete_entry(request.form.get('path', ''))
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(redirect_path)}')

        elif action == 'save':
            ok, msg = write_file(request.form.get('path', ''), request.form.get('content', ''))
            flash(msg, 'success' if ok else 'danger')
            return redirect(url_for('filemanager.filemanager_route') + f'?path={quote(redirect_path)}')

    data, err = list_dir(path)
    if err:
        flash(err, 'danger')
        data = {'path': root, 'entries': [], 'parent': root}

    for e in data['entries']:
        e['is_archive'] = not e['is_dir'] and is_archive(e['name'])

    return render_template('filemanager.html',
                           current_path=data['path'],
                           parent_path=data['parent'],
                           entries=data['entries'])

@filemanager_bp.route('/filemanager/read')
@login_required
def filemanager_read():
    path = request.args.get('path', '')
    content, err = read_file(path)
    if err:
        return jsonify({'error': err})
    return jsonify({'content': content})

@filemanager_bp.route('/filemanager/download')
@login_required
def filemanager_download():
    path = request.args.get('path', '')
    safe = _safe_path(path)
    if not safe or not os.path.isfile(safe):
        flash('File not found.', 'danger')
        return redirect(url_for('filemanager.filemanager_route'))
    return send_file(safe, as_attachment=True)


@filemanager_bp.route('/ftp', methods=['GET', 'POST'])
@login_required
def ftp():
    ftp_installed = check_pureftpd_installed()
    is_admin = session.get('role') == 'admin'
    current_username = session.get('username')
    user_home = f"/home/{current_username}/public_html" if not is_admin else None

    if request.method == 'POST' and ftp_installed:
        action = request.form.get('action')

        if action == 'create':
            username = request.form.get('username')
            password = request.form.get('password')
            raw_directory = request.form.get('directory')

            v, e = validate_input(username, 'username')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('filemanager.ftp'))

            # Process directory depending on role
            if is_admin:
                if not raw_directory.startswith('/var/www/') and not raw_directory.startswith('/home/'):
                    raw_directory = os.path.join('/var/www', raw_directory.lstrip('/'))
                absolute_dir = os.path.abspath(raw_directory)
                if not (absolute_dir.startswith('/var/www/') or absolute_dir.startswith('/home/')):
                    flash("Invalid directory path. Must be within /var/www/ or /home/", "danger")
                    return redirect(url_for('filemanager.ftp'))
            else:
                # Regular user: forcefully restrict to their home directory
                if not raw_directory:
                    absolute_dir = user_home
                else:
                    # Strip leading slashes to prevent absolute path traversal tricks
                    safe_rel = raw_directory.lstrip('/')
                    absolute_dir = os.path.abspath(os.path.join(user_home, safe_rel))
                    
                if not absolute_dir.startswith(user_home):
                    flash("Access denied: You can only create FTP accounts within your directory.", "danger")
                    return redirect(url_for('filemanager.ftp'))
                
                # Prevent them from creating an FTP account with the same name as an admin or someone else
                # We enforce a prefix for non-admin ftp accounts just to be safe, e.g. username_ftpname
                # But to keep it simple, we just pass the username they chose. Pure-FTPd requires unique usernames.

            success, message = create_ftp_user(username, password, absolute_dir)
            flash(message, 'success' if success else 'danger')

        elif action == 'delete':
            username = request.form.get('username')
            v, e = validate_input(username, 'username')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('filemanager.ftp'))
                
            # If not admin, they can only delete their own FTPs.
            if not is_admin:
                # Get the directory of the FTP user being deleted
                ftp_users = get_ftp_users()
                ftp_user = next((u for u in ftp_users if u['username'] == username), None)
                if not ftp_user or not ftp_user['directory'].startswith(user_home):
                    flash("Access denied: You cannot delete this FTP account.", "danger")
                    return redirect(url_for('filemanager.ftp'))
                    
            success, message = delete_ftp_user(username)
            flash(message, 'success' if success else 'danger')

        elif action == 'password':
            username = request.form.get('username')
            new_password = request.form.get('new_password')
            if not is_admin:
                ftp_users = get_ftp_users()
                ftp_user = next((u for u in ftp_users if u['username'] == username), None)
                if not ftp_user or not ftp_user['directory'].startswith(user_home):
                    flash("Access denied: You cannot modify this FTP account.", "danger")
                    return redirect(url_for('filemanager.ftp'))
            success, message = change_ftp_password(username, new_password)
            flash(message, 'success' if success else 'danger')

        elif action == 'toggle_sftp':
            if not is_admin:
                flash("Access denied: Only admins can toggle SFTP system-wide.", "danger")
                return redirect(url_for('filemanager.ftp'))
            enable = request.form.get('enable') == 'true'
            success, message = toggle_sftp(enable)
            flash(message, 'success' if success else 'danger')

        elif action == 'toggle_user_status':
            username = request.form.get('username')
            if not is_admin:
                ftp_users = get_ftp_users()
                ftp_user = next((u for u in ftp_users if u['username'] == username), None)
                if not ftp_user or not ftp_user['directory'].startswith(user_home):
                    flash("Access denied: You cannot modify this FTP account.", "danger")
                    return redirect(url_for('filemanager.ftp'))
            enable = request.form.get('enable') == 'true'
            success, message = toggle_ftp_user_status(username, enable)
            flash(message, 'success' if success else 'danger')

        return redirect(url_for('filemanager.ftp'))

    sftp_enabled = get_sftp_status()
    users = get_ftp_users() if ftp_installed else None
    
    # Filter users for non-admins
    if users and not is_admin:
        users = [u for u in users if u['directory'].startswith(user_home)]
        
    return render_template('ftp.html', 
                           ftp_installed=ftp_installed, 
                           users=users, 
                           sftp_enabled=sftp_enabled,
                           is_admin=is_admin,
                           user_home=user_home)
