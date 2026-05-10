from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, admin_required

from hosting_mgr import get_plans, add_plan, delete_plan, get_users, add_user, delete_user

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/admin/plans', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_plans():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name')
            max_domains = request.form.get('max_domains', 0)
            max_databases = request.form.get('max_databases', 0)
            max_nextjs = request.form.get('max_nextjs', 0)
            if not name:
                flash('Plan name is required.', 'danger')
            else:
                ok, msg = add_plan(name, max_domains, max_databases, max_nextjs)
                flash(msg, 'success' if ok else 'danger')
        elif action == 'delete':
            name = request.form.get('name')
            ok, msg = delete_plan(name)
            flash(msg, 'success' if ok else 'danger')
        return redirect(url_for('admin.admin_plans'))
    return render_template('admin_plans.html', plans=get_plans())

@admin_bp.route('/admin/users', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_users():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            username = request.form.get('username')
            password = request.form.get('password')
            plan = request.form.get('plan')
            if not username or not password or not plan:
                flash('All fields are required.', 'danger')
            else:
                ok, msg = add_user(username, password, plan)
                flash(msg, 'success' if ok else 'danger')
        elif action == 'delete':
            username = request.form.get('username')
            ok, msg = delete_user(username)
            flash(msg, 'success' if ok else 'danger')
        return redirect(url_for('admin.admin_users'))
    return render_template('admin_users.html', users=get_users(), plans=get_plans())
