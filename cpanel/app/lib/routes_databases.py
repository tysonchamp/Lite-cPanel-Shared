from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required
from security_mgr import validate_input

from database_mgr import (get_databases, get_database_details, create_database,
                           delete_database, setup_phpmyadmin_signon,
                           change_user_password, update_user_host)

from mongodb_mgr import (check_mongodb_installed, install_mongodb, get_databases as get_mongo_dbs,
                         create_database as create_mongo_db, delete_database as delete_mongo_db,
                         change_user_password as change_mongo_pass,
                         check_mongo_express_installed, install_mongo_express,
                         get_mongo_express_status, restart_mongo_express,
                         get_mongo_express_credentials)

databases_bp = Blueprint('databases', __name__)

@databases_bp.route('/databases', methods=['GET', 'POST'])
@login_required
def databases():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'create':
            db_name = request.form.get('db_name')
            db_user = request.form.get('db_user')
            db_pass = request.form.get('db_pass')
            
            v1, e1 = validate_input(db_name, 'db_name')
            v2, e2 = validate_input(db_user, 'username')
            if not v1 or not v2:
                flash(f"Validation failed: {e1 or e2}", "danger")
                return redirect(url_for('databases.databases'))

            success, message = create_database(db_name, db_user, db_pass, session.get('role'), session.get('username'))
            flash(message, 'success' if success else 'danger')

        elif action == 'delete':
            db_name = request.form.get('db_name')
            v, e = validate_input(db_name, 'db_name')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('databases.databases'))
            success, message = delete_database(db_name)
            flash(message, 'success' if success else 'danger')

        elif action == 'change_password':
            db_user = request.form.get('db_user')
            host    = request.form.get('host')
            new_pw  = request.form.get('new_password')
            v, e = validate_input(db_user, 'username')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('databases.databases'))
            success, message = change_user_password(db_user, host, new_pw)
            flash(message, 'success' if success else 'danger')

        elif action == 'update_host':
            db_name  = request.form.get('db_name')
            db_user  = request.form.get('db_user')
            old_host = request.form.get('old_host')
            new_host = request.form.get('new_host')
            v, e = validate_input(db_name, 'db_name')
            v2, e2 = validate_input(db_user, 'username')
            if not v or not v2:
                flash(f"Validation failed: {e or e2}", "danger")
                return redirect(url_for('databases.databases'))
            success, message = update_user_host(db_name, db_user, old_host, new_host)
            flash(message, 'success' if success else 'danger')

        return redirect(url_for('databases.databases'))

    db_details = get_database_details(session.get('role'), session.get('username'))
    return render_template('databases.html', db_details=db_details)

@databases_bp.route('/mongodb', methods=['GET', 'POST'])
@login_required
def mongodb_route():
    role = session.get('role')
    username = session.get('username')
    is_installed = check_mongodb_installed()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'install':
            if role != 'admin':
                flash("Access denied.", "danger")
                return redirect(url_for('databases.mongodb_route'))
            success, msg = install_mongodb()
            flash(msg, 'success' if success else 'danger')
            return redirect(url_for('databases.mongodb_route'))
            
        if not is_installed:
            flash("MongoDB is not installed.", "danger")
            return redirect(url_for('databases.mongodb_route'))
            
        if action == 'create':
            db_name = request.form.get('db_name')
            db_user = request.form.get('db_user')
            db_pass = request.form.get('db_pass')
            
            v1, e1 = validate_input(db_name, 'db_name')
            v2, e2 = validate_input(db_user, 'username')
            if not v1 or not v2:
                flash(f"Validation failed: {e1 or e2}", "danger")
                return redirect(url_for('databases.mongodb_route'))
                
            success, message = create_mongo_db(db_name, db_user, db_pass, role, username)
            flash(message, 'success' if success else 'danger')
            
        elif action == 'delete':
            db_name = request.form.get('db_name')
            v, e = validate_input(db_name, 'db_name')
            if not v:
                flash(f"Validation failed: {e}", "danger")
                return redirect(url_for('databases.mongodb_route'))
            success, message = delete_mongo_db(db_name, role, username)
            flash(message, 'success' if success else 'danger')
            
        elif action == 'change_password':
            db_name = request.form.get('db_name')
            db_user = request.form.get('db_user')
            new_pass = request.form.get('new_password')
            
            v1, e1 = validate_input(db_name, 'db_name')
            v2, e2 = validate_input(db_user, 'username')
            if not v1 or not v2:
                flash(f"Validation failed: {e1 or e2}", "danger")
                return redirect(url_for('databases.mongodb_route'))
                
            if role == 'user':
                if not db_name.startswith(f"{username}_"):
                    flash("Access denied.", "danger")
                    return redirect(url_for('databases.mongodb_route'))
                    
            success, message = change_mongo_pass(db_name, db_user, new_pass)
            flash(message, 'success' if success else 'danger')
            
        elif action == 'install_express':
            if role != 'admin':
                flash("Access denied.", "danger")
                return redirect(url_for('databases.mongodb_route'))
            success, msg = install_mongo_express()
            flash(msg, 'success' if success else 'danger')
            
        elif action == 'restart_express':
            if role != 'admin':
                flash("Access denied.", "danger")
                return redirect(url_for('databases.mongodb_route'))
            success, msg = restart_mongo_express()
            flash(msg, 'success' if success else 'danger')
            
        return redirect(url_for('databases.mongodb_route'))

    db_details = get_mongo_dbs(role, username) if is_installed else []
    me_installed = check_mongo_express_installed()
    me_status = get_mongo_express_status() if is_installed else 'not_installed'
    me_creds = get_mongo_express_credentials() if me_status == 'active' else {}
    
    return render_template(
        'mongodb.html', 
        is_installed=is_installed, 
        db_details=db_details,
        me_installed=me_installed,
        me_status=me_status,
        me_creds=me_creds
    )

@databases_bp.route('/phpmyadmin-login')
@login_required
def phpmyadmin_login():
    """Simple redirect to phpMyAdmin without auto-login."""
    host = request.host.split(':')[0]
    return redirect(f"http://{host}/phpmyadmin/")
