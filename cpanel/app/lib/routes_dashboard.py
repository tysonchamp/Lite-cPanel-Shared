from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session
from auth import login_required
import psutil
import os
import json
import subprocess
import threading
import time
import re
from datetime import datetime
import platform
import socket

dashboard_bp = Blueprint('dashboard', __name__)

# --- Global Cache for Dashboard Speed ---
DASHBOARD_CACHE = {
    'traffic': [],
    'security_events': [],
    'public_ip': "Unknown",
    'cpu_model': "Unknown",
    'last_ip_update': 0,
    'last_traffic_update': 0,
    'last_security_update': 0
}

def get_cpu_model():
    try:
        if os.path.exists('/proc/cpuinfo'):
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if 'model name' in line:
                        return line.split(':')[1].strip()
    except: pass
    return platform.processor() or "Generic Processor"

def get_dashboard_traffic():
    """Heavy function to calculate traffic for all domains."""
    from nextjs_mgr import get_nextjs_apps
    from domains_mgr import get_virtual_hosts
    
    traffic_stats = []
    all_domains = set()
    try:
        for host in get_virtual_hosts('admin', None): all_domains.add(host['domain'])
    except: pass

    for domain in all_domains:
        domain_lower = domain.lower()
        log_candidates = [
            f"/var/log/nginx/{domain_lower}_access.log",
            f"/var/log/apache2/{domain_lower}_access.log",
            f"/var/log/nginx/{domain_lower}.access.log",
            f"/var/log/apache2/{domain_lower}.access.log",
            f"/var/log/nginx/access.log",
        ]
        
        traffic_item = None
        for log_file in log_candidates:
            if os.path.exists(log_file):
                try:
                    cmd = ['/usr/bin/goaccess', log_file, '--log-format=COMBINED', '--no-global-config', '-o', 'json']
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    if res.returncode == 0:
                        data = json.loads(res.stdout)
                        gen = data.get('general', {})
                        traffic_item = {'domain': domain, 'hits': gen.get('total_requests', 0), 'bandwidth': gen.get('bandwidth', 0), 'visitors': gen.get('unique_visitors', 0)}
                        if traffic_item['hits'] > 0: break
                except: pass
        if traffic_item: traffic_stats.append(traffic_item)
    return sorted(traffic_stats, key=lambda x: x['bandwidth'], reverse=True)

def get_dashboard_security():
    all_events = []
    auth_logs = [('/var/log/auth.log', 'sshd'), ('/var/log/secure', 'sshd'), ('/var/log/cpanel_auth.log', 'cpanel_auth')]
    
    for log_path, tag in auth_logs:
        if os.path.exists(log_path):
            try:
                res = subprocess.run(['tail', '-n', '50', log_path], capture_output=True, text=True, timeout=5)
                for line in res.stdout.strip().split('\n'):
                    if not line: continue
                    if (tag == 'sshd' and any(x in line for x in ['Accepted', 'Failed', 'Invalid'])) or \
                       (tag == 'cpanel_auth' and 'Failed login attempt' in line):
                        
                        time_str = "Unknown"
                        iso_match = re.search(r'^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})', line)
                        if iso_match:
                            try:
                                dt = datetime.strptime(f"{iso_match.group(1)} {iso_match.group(2)}", '%Y-%m-%d %H:%M:%S')
                                time_str = dt.strftime('%b %d %H:%M:%S')
                            except: time_str = f"{iso_match.group(1)} {iso_match.group(2)}"
                        else:
                            syslog_match = re.match(r'^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})', line)
                            if syslog_match:
                                time_str = syslog_match.group(1)
                            else:
                                time_str = " ".join(line.split()[:3])

                        m = re.search(r'sshd\[\d+\]: (.*)', line)
                        if not m and 'cpanel_auth' in tag:
                            m = re.search(r'cpanel_auth: (.*)', line)
                        msg = m.group(1) if m else line
                        
                        all_events.append({'time': time_str, 'msg': msg})
            except: pass
    return sorted(all_events, key=lambda x: x['time'], reverse=True)[:10]

def dashboard_background_worker():
    DASHBOARD_CACHE['cpu_model'] = get_cpu_model()
    
    while True:
        try:
            if time.time() - DASHBOARD_CACHE['last_ip_update'] > 86400:
                try:
                    import urllib.request
                    DASHBOARD_CACHE['public_ip'] = urllib.request.urlopen('https://ident.me', timeout=5).read().decode('utf-8').strip()
                    DASHBOARD_CACHE['last_ip_update'] = time.time()
                except: pass

            if time.time() - DASHBOARD_CACHE['last_traffic_update'] > 600:
                DASHBOARD_CACHE['traffic'] = get_dashboard_traffic()
                DASHBOARD_CACHE['last_traffic_update'] = time.time()

            if time.time() - DASHBOARD_CACHE['last_security_update'] > 120:
                DASHBOARD_CACHE['security_events'] = get_dashboard_security()
                DASHBOARD_CACHE['last_security_update'] = time.time()
                
        except Exception as e:
            print(f"Background worker error: {e}")
        time.sleep(30)

# Start background thread immediately when blueprint is loaded
threading.Thread(target=dashboard_background_worker, daemon=True).start()

@dashboard_bp.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('auth.login'))
    return redirect(url_for('dashboard.dashboard'))

@dashboard_bp.route('/dashboard')
@login_required
def dashboard():
    cpu_usage = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    stats = {
        'cpu': cpu_usage,
        'ram_total': round(ram.total / (1024**3), 2),
        'ram_used': round(ram.used / (1024**3), 2),
        'ram_percent': ram.percent,
        'disk_total': round(disk.total / (1024**3), 2),
        'disk_used': round(disk.used / (1024**3), 2),
        'disk_percent': disk.percent,
        'user_ip': request.headers.get('CF-Connecting-IP') or \
                   request.headers.get('X-Real-IP') or \
                   request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip() or "Unknown"
    }
    
    hostname = platform.node()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
    except: ip_address = "Unknown"
        
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime_delta = datetime.now() - boot_time
    uptime_str = f"{uptime_delta.days}d {uptime_delta.seconds // 3600}h {(uptime_delta.seconds % 3600) // 60}m"

    public_ip = DASHBOARD_CACHE.get('public_ip', 'Unknown')
    traffic_stats = DASHBOARD_CACHE.get('traffic', [])
    
    # Filter traffic stats if not admin
    if session.get('role') != 'admin':
        from nextjs_mgr import get_nextjs_apps
        from domains_mgr import get_virtual_hosts
        user_domains = set()
        for app in get_nextjs_apps(session.get('role'), session.get('username')): user_domains.add(app['domain'])
        for host in get_virtual_hosts(session.get('role'), session.get('username')): user_domains.add(host['domain'])
        traffic_stats = [t for t in traffic_stats if t['domain'] in user_domains]

    ssh_logins = DASHBOARD_CACHE.get('security_events', [])

    ports = []
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status == 'LISTEN': ports.append(conn.laddr.port)
        ports = sorted(list(set(ports)))
    except: pass

    server_info = {
        'hostname': hostname,
        'ip_address': ip_address,
        'public_ip': public_ip,
        'processor': DASHBOARD_CACHE.get('cpu_model', 'Unknown'),
        'cpu_cores': psutil.cpu_count(logical=True),
        'cpu_freq': f"{psutil.cpu_freq().current:.0f} MHz" if psutil.cpu_freq() else "N/A",
        'os': "Linux",
        'kernel': platform.release(),
        'platform': f"{platform.machine()} {platform.system()}",
        'uptime': uptime_str,
        'server_time': datetime.now().strftime("%a %b %d %H:%M:%S"),
        'listening_ports': ports,
        'ssh_logins': ssh_logins,
        'last_backup': "Check Backups Page"
    }

    return render_template('dashboard.html', server_info=server_info, stats=stats, traffic_stats=traffic_stats)

@dashboard_bp.route('/traffic')
@login_required
def traffic_monitor():
    traffic_stats = []
    from nextjs_mgr import get_nextjs_apps
    from domains_mgr import get_virtual_hosts
    
    def get_domain_traffic_helper(domain, log_file):
        if not os.path.exists(log_file): return None
        try:
            goaccess_path = '/usr/bin/goaccess'
            if not os.path.exists(goaccess_path): goaccess_path = 'goaccess'
            
            cmd = [goaccess_path, log_file, '--log-format=COMBINED', '--no-global-config', '-o', 'json']
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                cmd = [goaccess_path, log_file, '--log-format=VCOMMON', '--no-global-config', '-o', 'json']
                res = subprocess.run(cmd, capture_output=True, text=True)
            
            if res.returncode == 0:
                data = json.loads(res.stdout)
                general = data.get('general', {})
                return {
                    'domain': domain,
                    'hits': general.get('total_requests', 0),
                    'bandwidth': general.get('bandwidth', 0),
                    'visitors': general.get('unique_visitors', 0)
                }
        except: pass
        return None

    role = session.get('role')
    username = session.get('username')
    
    all_domains = set()
    for app in get_nextjs_apps(role, username): all_domains.add(app['domain'])
    for host in get_virtual_hosts(role, username): all_domains.add(host['domain'])

    for domain in all_domains:
        domain_lower = domain.lower()
        log_candidates = [
            f"/var/log/nginx/{domain_lower}_access.log",
            f"/var/log/apache2/{domain_lower}_access.log",
            f"/var/log/nginx/{domain_lower}.access.log",
            f"/var/log/apache2/{domain_lower}.access.log",
            f"/var/log/nginx/access.log"
        ]
        for log_file in log_candidates:
            if os.path.exists(log_file):
                stats = get_domain_traffic_helper(domain, log_file)
                if stats and stats['hits'] > 0:
                    traffic_stats.append(stats)
                    break

    traffic_stats = sorted(traffic_stats, key=lambda x: x['bandwidth'], reverse=True)
    return render_template('traffic.html', traffic_stats=traffic_stats)

@dashboard_bp.route('/traffic/report/<domain>')
@login_required
def traffic_report(domain):
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    domain_lower = domain.lower()
    log_candidates = [
        f"/var/log/nginx/{domain_lower}_access.log",
        f"/var/log/apache2/{domain_lower}_access.log",
        f"/var/log/nginx/{domain_lower}.access.log",
        f"/var/log/apache2/{domain_lower}.access.log",
        f"/var/log/nginx/{domain}_access.log",
        f"/var/log/apache2/{domain}_access.log"
    ]
    source_file = next((p for p in log_candidates if os.path.exists(p)), None)
    
    if not source_file:
        return f"Log file not found for {domain}.", 404
        
    try:
        goaccess_path = '/usr/bin/goaccess'
        if not os.path.exists(goaccess_path): goaccess_path = 'goaccess'
        
        def get_report(source_file, log_fmt):
            if start_date and end_date:
                import glob
                try:
                    s_dt = datetime.strptime(start_date, '%Y-%m-%d')
                    e_dt = datetime.strptime(end_date, '%Y-%m-%d')
                    
                    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    patterns = []
                    curr = s_dt
                    from datetime import timedelta
                    while curr <= e_dt:
                        patterns.append(f"{curr.day:02d}/{months[curr.month]}/{curr.year}")
                        curr += timedelta(days=1)
                    
                    if not patterns: return None, "Invalid date range"
                    
                    all_logs = glob.glob(f"{source_file}*")
                    log_files = []
                    
                    start_ts = s_dt.timestamp() - 86400
                    for f in all_logs:
                        try:
                            if os.path.getmtime(f) >= start_ts:
                                log_files.append(f)
                        except OSError:
                            pass
                            
                    if not log_files:
                        return None, f"No logs modified on or after {start_date}."

                    if s_dt == e_dt:
                        grep_cmd = ['zgrep', '-hi', patterns[0]] + log_files
                    else:
                        regex_pattern = "|".join(patterns)
                        grep_cmd = ['zgrep', '-hiE', regex_pattern] + log_files
                        
                    go_cmd = [goaccess_path, '-', f'--log-format={log_fmt}', '--no-global-config', '-o', 'html']
                    
                    p1 = subprocess.Popen(grep_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    res = subprocess.run(go_cmd, stdin=p1.stdout, capture_output=True)
                    p1.stdout.close()
                    p1.wait()
                    
                    if res.returncode != 0:
                        err_out = res.stderr.decode('utf-8', errors='ignore')
                        if "Parsed 0 lines" in err_out or "No input" in err_out or p1.returncode != 0:
                            return None, f"No traffic data found for the period {start_date} to {end_date}."
                            
                    return res, None
                except Exception as e:
                    return None, str(e)
            else:
                go_cmd = [goaccess_path, source_file, f'--log-format={log_fmt}', '--no-global-config', '-o', 'html']
                res = subprocess.run(go_cmd, capture_output=True)
                return res, None

        res, err = get_report(source_file, 'COMBINED')
        if res and res.returncode != 0:
            res, err = get_report(source_file, 'VCOMMON')

        if res and res.returncode == 0:
            from flask import make_response
            response = make_response(res.stdout)
            response.headers['Content-Type'] = 'text/html'
            return response
        else:
            error_msg = err if err else (res.stderr.decode('utf-8', errors='ignore') if res else "Unknown error")
            return f"GoAccess Error: {error_msg}", 500
    except Exception as e:
        return f"System Error: {str(e)}", 500

@dashboard_bp.route('/api/sysinfo')
@login_required
def api_sysinfo():
    load1, load5, load15 = os.getloadavg()
    res = subprocess.run(['ps', 'aux', '--sort=-%cpu'], capture_output=True, text=True)
    lines = res.stdout.strip().split('\n')
    processes = []
    for line in lines[1:11]:
        parts = line.split(None, 10)
        if len(parts) == 11:
            cmd = parts[10]
            if len(cmd) > 50: cmd = cmd[:47] + '...'
            processes.append({
                'user': parts[0],
                'pid': parts[1],
                'cpu': parts[2],
                'mem': parts[3],
                'name': cmd
            })
            
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    return jsonify({
        'cpu_percent': cpu_percent,
        'ram': {'used': round(mem.used / (1024**3), 2), 'total': round(mem.total / (1024**3), 2), 'percent': mem.percent},
        'disk': {'used': round(disk.used / (1024**3), 2), 'total': round(disk.total / (1024**3), 2), 'percent': disk.percent},
        'load': [round(load1, 2), round(load5, 2), round(load15, 2)],
        'processes': processes
    })

@dashboard_bp.route('/api/services')
@login_required
def api_services():
    from modsec_mgr import get_modsec_status
    
    res = subprocess.run("systemctl list-units --type=service --all | grep -m1 -oE 'php[0-9.]+-fpm\\.service'", shell=True, capture_output=True, text=True)
    php_fpm_id = res.stdout.strip().replace('.service', '') or 'php-fpm'

    services = [
        {'id': 'apache2', 'name': 'Apache Engine'},
        {'id': 'nginx', 'name': 'Nginx Engine'},
        {'id': php_fpm_id, 'name': 'PHP-FPM'}, 
        {'id': 'mariadb', 'name': 'MySQL / MariaDB'},
        {'id': 'mongod', 'name': 'MongoDB'},
        {'id': 'mongo-express', 'name': 'Mongo Express'},
        {'id': 'csf', 'name': 'CSF Firewall'},
        {'id': 'cpanel', 'name': 'cPanel Platform'},
    ]

    from mongodb_mgr import get_mongo_express_status
    for srv in services:
        sys_id = srv['id']
        if sys_id == 'mongo-express':
            me = get_mongo_express_status()
            srv['status'] = 'active' if me == 'active' else ('not_installed' if me == 'not_installed' else 'inactive')
            continue
        chk = subprocess.run(['systemctl', 'is-active', sys_id], capture_output=True, text=True)
        status = chk.stdout.strip()
        srv['status'] = status if status in ['active', 'inactive', 'failed'] else 'not_installed'

    try:
        from modsec_mgr import check_modsec_installed
        modsec_status = get_modsec_status()
        modsec_installed = check_modsec_installed()
        apache_active = any(s['id'] == 'apache2' and s['status'] == 'active' for s in services)
        if not modsec_installed:
            modsec_state = 'not_installed'
        elif modsec_status == 'Off':
            modsec_state = 'inactive'
        elif apache_active:
            modsec_state = 'active'
        else:
            modsec_state = 'inactive'
    except Exception:
        modsec_state = 'unknown'

    services.append({'id': 'modsec', 'name': 'ModSecurity', 'status': modsec_state})
    return jsonify({'services': services})

@dashboard_bp.route('/api/services/restart', methods=['POST'])
@login_required
def api_service_restart():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Permission denied. Admins only.'}), 403

    service_id = request.json.get('service_id') if request.is_json else request.form.get('service_id')
    if not service_id:
        return jsonify({'success': False, 'message': 'Missing service identification.'})

    if service_id == 'mongo-express':
        from mongodb_mgr import restart_mongo_express
        ok, msg = restart_mongo_express()
        return jsonify({'success': ok, 'message': msg})

    if service_id == 'modsec':
        res = subprocess.run(['systemctl', 'restart', 'apache2'], capture_output=True, text=True)
        if res.returncode == 0: return jsonify({'success': True, 'message': 'ModSecurity refreshed successfully.'})
        return jsonify({'success': False, 'message': f'Operation failed: {res.stderr}'})

    if service_id == 'cpanel':
        subprocess.Popen(['bash', '-c', 'sleep 1 && systemctl restart cpanel.service'])
        return jsonify({'success': True, 'message': 'Process initiated in background...'})

    if service_id not in ['apache2', 'nginx', 'mariadb', 'mysql', 'csf'] and not service_id.startswith('php'):
        return jsonify({'success': False, 'message': 'Forbidden infrastructure target.'})

    res = subprocess.run(['systemctl', 'restart', service_id], capture_output=True, text=True)
    if res.returncode == 0: return jsonify({'success': True, 'message': f'{service_id.title()} has been restarted successfully.'})
    else: return jsonify({'success': False, 'message': f'Crash/Timeout: {res.stderr}'})
