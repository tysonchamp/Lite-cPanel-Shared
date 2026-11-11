import os
import re
import glob
import json
import time
import queue
import shutil
import tempfile
import threading
import contextlib
import subprocess
import urllib.request
from domains_mgr import get_virtual_hosts

SUPPORTED_PHP_VERSIONS = ['7.4', '8.0', '8.1', '8.2', '8.3', '8.4', '8.5']

DEFAULT_CORE_EXTENSIONS = [
    'mysql', 'curl', 'gd', 'mbstring', 'xml', 'zip', 'bcmath', 'intl', 'gmp'
]

INSTALLABLE_EXTENSIONS = [
    'mysql', 'curl', 'gd', 'mbstring', 'xml', 'zip', 'bcmath', 'intl', 'gmp',
    'imagick', 'redis', 'memcached', 'mongodb', 'soap', 'sqlite3', 'pgsql',
    'ldap', 'imap', 'opcache'
]

INI_DIRECTIVES = [
    {
        'key': 'memory_limit',
        'label': 'Memory Limit',
        'placeholder': '256M',
        'help': 'Maximum amount of memory a script may consume (e.g. 128M, 512M, 1G, or -1).',
        'pattern': re.compile(r'^(\d+[KMG]?|-1)$', re.I),
    },
    {
        'key': 'upload_max_filesize',
        'label': 'Upload Max Filesize',
        'placeholder': '64M',
        'help': 'Maximum allowed size for uploaded files (e.g. 64M, 128M, 1G).',
        'pattern': re.compile(r'^\d+[KMG]?$', re.I),
    },
    {
        'key': 'post_max_size',
        'label': 'Post Max Size',
        'placeholder': '64M',
        'help': 'Maximum size of POST data accepted (should be >= upload_max_filesize).',
        'pattern': re.compile(r'^\d+[KMG]?$', re.I),
    },
    {
        'key': 'max_execution_time',
        'label': 'Max Execution Time (sec)',
        'placeholder': '300',
        'help': 'Maximum time in seconds a script is allowed to run before being terminated.',
        'pattern': re.compile(r'^\d+$'),
    },
    {
        'key': 'max_input_time',
        'label': 'Max Input Time (sec)',
        'placeholder': '60',
        'help': 'Maximum time in seconds a script is allowed to parse input data (-1 for unlimited).',
        'pattern': re.compile(r'^(-1|\d+)$'),
    },
    {
        'key': 'max_input_vars',
        'label': 'Max Input Vars',
        'placeholder': '3000',
        'help': 'Maximum number of input variables accepted per request.',
        'pattern': re.compile(r'^\d+$'),
    },
    {
        'key': 'display_errors',
        'label': 'Display Errors',
        'placeholder': 'Off',
        'help': 'Whether to print errors to the output (On or Off).',
        'pattern': re.compile(r'^(On|Off)$', re.I),
        'options': ['Off', 'On'],
    },
    {
        'key': 'date.timezone',
        'label': 'Timezone (date.timezone)',
        'placeholder': 'UTC',
        'help': 'Default timezone used by all date/time functions (e.g. UTC, America/New_York).',
        'pattern': re.compile(r'^[A-Za-z0-9_/+.-]{1,64}$'),
    },
]

_VERSION_RE = re.compile(r'^\d+\.\d+$')
_PHP_BLOCK_RE = re.compile(
    r'\n?[ \t]*# LITE_CPANEL_PHP_START[^\n]*\n.*?# LITE_CPANEL_PHP_END[ \t]*\n?',
    re.DOTALL
)
_GENERIC_FPM_FILESMATCH_RE = re.compile(
    r'\n?[ \t]*<FilesMatch\s+["\']?\\\.php\$["\']?\s*>.*?SetHandler\s+["\']proxy:unix:(?:/var)?/run/php/php\d+\.\d+-fpm\.sock[^"\']*["\'].*?</FilesMatch>[ \t]*\n?',
    re.DOTALL | re.IGNORECASE
)
_NGINX_FASTCGI_SOCK_RE = re.compile(
    r'(fastcgi_pass\s+unix:)(?:/var)?/run/php/php\d+\.\d+-fpm\.sock(\s*;)'
)


def _version_sort_key(ver):
    try:
        return tuple(int(x) for x in ver.split('.'))
    except Exception:
        return (0, 0)


def is_valid_php_version(version):
    return isinstance(version, str) and bool(_VERSION_RE.fullmatch(version))


def get_webserver_type():
    """Return configured webserver stack type ('apache', 'nginx', 'hybrid', or 'unknown')."""
    cfg = '/var/lib/lite-cpanel/.stack_config'
    if os.path.exists(cfg):
        try:
            with open(cfg, 'r') as f:
                for line in f:
                    if line.startswith('WEBSERVER_TYPE='):
                        return line.split('=', 1)[1].strip().lower()
        except Exception:
            pass
    has_nginx = os.path.exists('/etc/nginx/sites-available')
    has_apache = os.path.exists('/etc/apache2/sites-available')
    if has_nginx and has_apache:
        return 'hybrid'
    if has_nginx:
        return 'nginx'
    if has_apache:
        return 'apache'
    return 'unknown'


def get_default_php_version():
    """Detect the system default PHP version (CLI / Apache fallback)."""
    php_bin = shutil.which('php') or '/usr/bin/php'
    if os.path.exists(php_bin):
        try:
            res = subprocess.run(
                [php_bin, '-r', 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;'],
                capture_output=True, text=True, timeout=5
            )
            ver = res.stdout.strip()
            if is_valid_php_version(ver):
                return ver
        except Exception:
            pass
        try:
            real = os.path.realpath(php_bin)
            m = re.search(r'php(\d+\.\d+)$', real)
            if m:
                return m.group(1)
        except Exception:
            pass

    for conf in sorted(glob.glob('/etc/apache2/conf-enabled/php*-fpm.conf')):
        m = re.search(r'php(\d+\.\d+)-fpm\.conf$', conf)
        if m:
            return m.group(1)

    for mod in sorted(glob.glob('/etc/apache2/mods-enabled/php*.load')):
        m = re.search(r'php(\d+\.\d+)\.load$', mod)
        if m:
            return m.group(1)

    return None


def _get_fpm_socket_path(version):
    """Read listen socket from php-fpm pool config, falling back to /run/php/php{ver}-fpm.sock."""
    pool_conf = f'/etc/php/{version}/fpm/pool.d/www.conf'
    if os.path.exists(pool_conf):
        try:
            with open(pool_conf, 'r') as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith(';'):
                        continue
                    if stripped.startswith('listen') and '=' in stripped:
                        val = stripped.split('=', 1)[1].strip()
                        if val.startswith('/'):
                            return val
        except Exception:
            pass
    return f'/run/php/php{version}-fpm.sock'


def _get_service_status(service_name):
    try:
        res = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True, timeout=5
        )
        status = res.stdout.strip()
        if status in ('active', 'inactive', 'failed', 'activating'):
            return status
    except Exception:
        pass
    return 'inactive'


def _extract_upstream_version(pkg_ver):
    """Extract clean PHP version (e.g. '7.4.33') from a Debian/Ubuntu package version string."""
    if not pkg_ver:
        return ''
    ver = pkg_ver.strip()
    if not ver or ver == '(none)':
        return ''
    if ':' in ver:
        ver = ver.split(':', 1)[1]
    ver = ver.split('-', 1)[0].split('+', 1)[0].split('~', 1)[0]
    return ver


def _get_all_installed_php_dpkg_versions():
    """Return {pkg_name: installed_deb_version} for all installed phpX.Y* packages."""
    installed = {}
    if not shutil.which('dpkg-query'):
        return installed
    try:
        res = subprocess.run(
            ['dpkg-query', '-W', '-f=${Package}\t${db:Status-Abbrev}\t${Version}\n', 'php[0-9]*.[0-9]*'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10
        )
        for line in res.stdout.splitlines():
            parts = line.split('\t')
            if len(parts) != 3:
                continue
            pkg, status, ver = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if status.startswith('ii') and re.fullmatch(r'php\d+\.\d+(?:-[a-z0-9.+-]+)?', pkg):
                installed[pkg] = ver
    except Exception:
        pass
    return installed


def get_installed_php_versions():
    """Return metadata dicts for all PHP versions detected on the system."""
    versions = set()

    if os.path.exists('/etc/php'):
        for entry in os.listdir('/etc/php'):
            if is_valid_php_version(entry) and os.path.isdir(os.path.join('/etc/php', entry)):
                versions.add(entry)

    for bin_path in glob.glob('/usr/bin/php[0-9]*.[0-9]*'):
        m = re.search(r'/php(\d+\.\d+)$', bin_path)
        if m:
            versions.add(m.group(1))

    for fpm_path in glob.glob('/usr/sbin/php-fpm[0-9]*.[0-9]*'):
        m = re.search(r'/php-fpm(\d+\.\d+)$', fpm_path)
        if m:
            versions.add(m.group(1))

    default_ver = get_default_php_version()
    dpkg_pkgs = _get_all_installed_php_dpkg_versions() if versions else {}
    result = []
    for ver in sorted(versions, key=_version_sort_key):
        cli_bin = f'/usr/bin/php{ver}'
        has_cli = os.path.exists(cli_bin)
        has_fpm = (
            os.path.exists(f'/usr/sbin/php-fpm{ver}') or
            os.path.isdir(f'/etc/php/{ver}/fpm')
        )
        if not has_cli and not has_fpm:
            continue

        fpm_service = f'php{ver}-fpm'
        fpm_status = _get_service_status(fpm_service) if has_fpm else 'not_installed'
        socket_path = _get_fpm_socket_path(ver) if has_fpm else None

        pkg_ver = (
            dpkg_pkgs.get(f'php{ver}-fpm')
            or dpkg_pkgs.get(f'php{ver}-cli')
            or dpkg_pkgs.get(f'php{ver}-common')
            or ''
        )
        full_ver = _extract_upstream_version(pkg_ver)
        if not full_ver and has_cli:
            try:
                vr = subprocess.run(
                    [cli_bin, '-r', 'echo PHP_VERSION;'],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
                )
                if vr.returncode == 0 and vr.stdout.strip():
                    full_ver = vr.stdout.strip().split('-', 1)[0]
            except Exception:
                pass

        ini_files = {}
        for sapi in ('fpm', 'apache2', 'cli'):
            ini_path = f'/etc/php/{ver}/{sapi}/php.ini'
            if os.path.exists(ini_path):
                ini_files[sapi] = ini_path

        result.append({
            'version': ver,
            'full_version': full_ver or ver,
            'pkg_version': pkg_ver,
            'has_cli': has_cli,
            'has_fpm': has_fpm,
            'fpm_service': fpm_service,
            'fpm_status': fpm_status,
            'socket_path': socket_path,
            'is_default': (ver == default_ver),
            'ini_files': ini_files,
        })

    return result


def get_php_extensions(version):
    """List loaded PHP modules for a specific installed PHP version."""
    if not is_valid_php_version(version):
        return []
    php_bin = f'/usr/bin/php{version}'
    if not os.path.exists(php_bin):
        return []
    try:
        res = subprocess.run([php_bin, '-m'], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return []
        modules = []
        for line in res.stdout.splitlines():
            s = line.strip()
            if not s or s.startswith('['):
                continue
            modules.append(s)
        return sorted(set(modules), key=str.lower)
    except Exception:
        return []


def _is_non_php_proxy_vhost(config_paths):
    """Return True if any vhost config is marked as a Next.js or Docker reverse proxy app."""
    for path in config_paths.values():
        try:
            with open(path, 'r') as f:
                content = f.read()
                if '# NEXTJS_APP' in content or '# DOCKER_APP' in content:
                    return True
        except Exception:
            pass
    return False


def _detect_apache_php_version(apache_conf_path):
    if not apache_conf_path or not os.path.exists(apache_conf_path):
        return None
    try:
        with open(apache_conf_path, 'r') as f:
            content = f.read()
        m = re.search(r'# LITE_CPANEL_PHP_START\s*\((\d+\.\d+)\)', content)
        if m:
            return m.group(1)
        m = re.search(r'SetHandler\s+["\']?proxy:unix:(?:/var)?/run/php/php(\d+\.\d+)-fpm\.sock', content)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _detect_nginx_php_version(nginx_conf_path):
    if not nginx_conf_path or not os.path.exists(nginx_conf_path):
        return None
    try:
        with open(nginx_conf_path, 'r') as f:
            content = f.read()
        m = re.search(r'fastcgi_pass\s+unix:(?:/var)?/run/php/php(\d+\.\d+)-fpm\.sock', content)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_php_domains(role='admin', username=None):
    """
    Return PHP-capable virtual hosts filtered by ownership (when role != 'admin')
    along with each domain's active/inherited PHP version.
    """
    vhosts = get_virtual_hosts(role, username)
    default_php = get_default_php_version()
    php_domains = []

    for v in vhosts:
        domain = v.get('domain', '')
        if not domain or domain.endswith('-le-ssl'):
            continue
        config_paths = v.get('config_paths', {})
        if _is_non_php_proxy_vhost(config_paths):
            continue

        apache_conf = config_paths.get('Apache')
        nginx_conf = config_paths.get('Nginx')

        detected = _detect_apache_php_version(apache_conf)
        handler = 'apache_fpm' if detected else None

        if not detected:
            nginx_ver = _detect_nginx_php_version(nginx_conf)
            if nginx_ver:
                detected = nginx_ver
                handler = 'nginx_fpm'

        is_inherited = False
        if not detected:
            detected = default_php or 'Unknown'
            is_inherited = True
            handler = 'system_default'

        php_domains.append({
            'domain': domain,
            'servers': v.get('servers', []),
            'enabled': v.get('enabled', False),
            'has_ssl': v.get('has_ssl', False),
            'doc_root': v.get('doc_root', f'/var/www/{domain}'),
            'config_paths': config_paths,
            'php_version': detected,
            'is_inherited': is_inherited,
            'handler': handler,
        })

    return sorted(php_domains, key=lambda d: d['domain'].lower())


def _update_apache_vhost_php(conf_path, version, socket_path=None):
    """Apply or remove per-vhost PHP-FPM handler in an Apache VirtualHost config."""
    with open(conf_path, 'r') as f:
        content = f.read()

    updated = _PHP_BLOCK_RE.sub('\n', content)
    updated = _GENERIC_FPM_FILESMATCH_RE.sub('\n', updated)

    if version != 'inherit':
        snippet = (
            f"    # LITE_CPANEL_PHP_START ({version})\n"
            f"    <FilesMatch \\.php$>\n"
            f"        SetHandler \"proxy:unix:{socket_path}|fcgi://localhost\"\n"
            f"    </FilesMatch>\n"
            f"    # LITE_CPANEL_PHP_END\n"
        )
        updated, count = re.subn(
            r'([ \t]*</VirtualHost>)',
            lambda m: f"\n{snippet}{m.group(1)}",
            updated,
            flags=re.IGNORECASE
        )
        if count == 0:
            return False, f"No <VirtualHost> block found in {conf_path}."

    # Collapse excessive blank lines introduced by repeated replacements
    updated = re.sub(r'\n{3,}', '\n\n', updated)
    with open(conf_path, 'w') as f:
        f.write(updated)
    return True, None


def _update_nginx_vhost_php(conf_path, socket_path):
    """Update fastcgi_pass socket in an Nginx VirtualHost config."""
    with open(conf_path, 'r') as f:
        content = f.read()

    if 'proxy_pass http://127.0.0.1:8080' in content and 'fastcgi_pass' not in content:
        # Hybrid frontend proxies directly to Apache backend
        return True, None

    if _NGINX_FASTCGI_SOCK_RE.search(content):
        updated = _NGINX_FASTCGI_SOCK_RE.sub(rf'\g<1>{socket_path}\g<2>', content)
        with open(conf_path, 'w') as f:
            f.write(updated)
        return True, None

    if 'location ~ \\.php' not in content and 'proxy_pass' not in content:
        php_block = (
            "    location ~ \\.php$ {\n"
            f"        fastcgi_pass unix:{socket_path};\n"
            "        fastcgi_index index.php;\n"
            "        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;\n"
            "        include fastcgi_params;\n"
            "    }\n"
        )
        updated, count = re.subn(
            r'(\n[ \t]*location\s+/\s*\{)',
            lambda m: f"\n{php_block}{m.group(1)}",
            content,
            count=1
        )
        if count > 0:
            with open(conf_path, 'w') as f:
                f.write(updated)
            return True, None

    return False, f"No PHP FastCGI block found in {conf_path}."


def set_domain_php_version(domain, version, role='admin', username=None):
    """Set (or reset to 'inherit') the PHP version for a single virtual host."""
    installed = {v['version']: v for v in get_installed_php_versions()}
    default_php = get_default_php_version()

    if version != 'inherit':
        if not is_valid_php_version(version) or version not in installed:
            return False, f"PHP {version} is not installed."
        if not installed[version]['has_fpm']:
            return False, f"PHP {version} FPM is not installed. Please install php{version}-fpm first."

    domains = get_php_domains(role, username)
    target = next((d for d in domains if d['domain'] == domain), None)
    if not target:
        return False, "Domain not found or you do not have permission to manage it."

    target_ver = default_php if version == 'inherit' else version
    socket_path = None
    if version != 'inherit':
        socket_path = installed[version]['socket_path']
        if installed[version]['fpm_status'] != 'active':
            subprocess.run(['systemctl', 'enable', '--now', f'php{version}-fpm'], capture_output=True)
    elif target_ver and target_ver in installed and installed[target_ver]['has_fpm']:
        socket_path = installed[target_ver]['socket_path']

    backups = {}
    apache_touched = False
    nginx_touched = False

    try:
        apache_paths = []
        primary_apache = f'/etc/apache2/sites-available/{domain}.conf'
        ssl_apache = f'/etc/apache2/sites-available/{domain}-le-ssl.conf'
        if os.path.exists(primary_apache):
            apache_paths.append(primary_apache)
        if os.path.exists(ssl_apache):
            apache_paths.append(ssl_apache)

        nginx_path = f'/etc/nginx/sites-available/{domain}'
        has_nginx = os.path.exists(nginx_path)

        if not apache_paths and not has_nginx:
            return False, f"No web server virtual host configuration found for {domain}."

        if apache_paths:
            if version != 'inherit':
                subprocess.run(['a2enmod', 'proxy', 'proxy_fcgi', 'setenvif'], capture_output=True)
            for p in apache_paths:
                with open(p, 'r') as f:
                    backups[p] = f.read()
                ok, err = _update_apache_vhost_php(p, version, socket_path)
                if not ok:
                    raise RuntimeError(err)
                apache_touched = True

        if has_nginx:
            with open(nginx_path, 'r') as f:
                nginx_orig = f.read()
            is_hybrid_proxy = ('proxy_pass http://127.0.0.1:8080' in nginx_orig and 'fastcgi_pass' not in nginx_orig)
            if not is_hybrid_proxy:
                if not socket_path:
                    raise RuntimeError(f"PHP-FPM socket unavailable for PHP {target_ver or version}.")
                backups[nginx_path] = nginx_orig
                ok, err = _update_nginx_vhost_php(nginx_path, socket_path)
                if not ok:
                    raise RuntimeError(err)
                nginx_touched = True

        if apache_touched:
            test_a = subprocess.run(['apache2ctl', 'configtest'], capture_output=True, text=True)
            if test_a.returncode != 0:
                raise RuntimeError(f"Apache config test failed: {test_a.stderr.strip() or test_a.stdout.strip()}")
            subprocess.run(['systemctl', 'reload', 'apache2'], check=True, capture_output=True, text=True)

        if nginx_touched:
            test_n = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
            if test_n.returncode != 0:
                raise RuntimeError(f"Nginx config test failed: {test_n.stderr.strip() or test_n.stdout.strip()}")
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True, text=True)

        label = f"System Default ({default_php})" if version == 'inherit' and default_php else (
            "System Default" if version == 'inherit' else f"PHP {version}"
        )
        return True, f"Updated {domain} to use {label}."
    except Exception as e:
        for path, orig in backups.items():
            try:
                with open(path, 'w') as f:
                    f.write(orig)
            except Exception:
                pass
        return False, str(e)


def set_default_php_version(version):
    """Switch the global/CLI default PHP version (admin-only)."""
    installed = {v['version']: v for v in get_installed_php_versions()}
    if not is_valid_php_version(version) or version not in installed:
        return False, f"PHP {version} is not installed."
    if not installed[version]['has_cli']:
        return False, f"PHP {version} CLI binary not found."

    try:
        for tool in ('php', 'phar', 'phar.phar', 'phpize', 'php-config'):
            target_bin = f'/usr/bin/{tool}{version}'
            if os.path.exists(target_bin):
                subprocess.run(['update-alternatives', '--set', tool, target_bin], capture_output=True)

        if installed[version]['has_fpm']:
            subprocess.run(['systemctl', 'enable', '--now', f'php{version}-fpm'], capture_output=True)

        if os.path.exists('/etc/apache2'):
            fpm_conf = f'/etc/apache2/conf-available/php{version}-fpm.conf'
            mod_php_load = f'/etc/apache2/mods-available/php{version}.load'
            enabled_fpm_confs = glob.glob('/etc/apache2/conf-enabled/php*-fpm.conf')

            if os.path.exists(fpm_conf):
                subprocess.run(['a2enmod', 'proxy', 'proxy_fcgi', 'setenvif'], capture_output=True, stdin=subprocess.DEVNULL)
                for m in glob.glob('/etc/apache2/mods-enabled/php*.load'):
                    mname = os.path.basename(m).replace('.load', '')
                    subprocess.run(['a2dismod', mname], capture_output=True, stdin=subprocess.DEVNULL)
                for c in enabled_fpm_confs:
                    cname = os.path.basename(c).replace('.conf', '')
                    if cname != f'php{version}-fpm':
                        subprocess.run(['a2disconf', cname], capture_output=True, stdin=subprocess.DEVNULL)
                subprocess.run(['a2enconf', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL)
            elif os.path.exists(mod_php_load):
                for c in enabled_fpm_confs:
                    cname = os.path.basename(c).replace('.conf', '')
                    subprocess.run(['a2disconf', cname], capture_output=True, stdin=subprocess.DEVNULL)
                for m in glob.glob('/etc/apache2/mods-enabled/php*.load'):
                    mname = os.path.basename(m).replace('.load', '')
                    if mname != f'php{version}':
                        subprocess.run(['a2dismod', mname], capture_output=True, stdin=subprocess.DEVNULL)
                subprocess.run(['a2enmod', f'php{version}'], capture_output=True, stdin=subprocess.DEVNULL)

            test_a = subprocess.run(['apache2ctl', 'configtest'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if test_a.returncode == 0:
                subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True, stdin=subprocess.DEVNULL)

        default_nginx = '/etc/nginx/sites-available/default'
        if os.path.exists(default_nginx) and installed[version]['has_fpm']:
            socket_path = installed[version]['socket_path']
            with open(default_nginx, 'r') as f:
                orig = f.read()
            if _NGINX_FASTCGI_SOCK_RE.search(orig):
                updated = _NGINX_FASTCGI_SOCK_RE.sub(rf'\g<1>{socket_path}\g<2>', orig)
                with open(default_nginx, 'w') as f:
                    f.write(updated)
                test_n = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
                if test_n.returncode == 0:
                    subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
                else:
                    with open(default_nginx, 'w') as f:
                        f.write(orig)

        return True, f"System default PHP switched to PHP {version}."
    except Exception as e:
        return False, f"Failed to switch default PHP: {str(e)}"


def manage_php_fpm_service(version, action):
    """Start, stop, restart, or reload php{version}-fpm (admin-only)."""
    if action not in ('start', 'stop', 'restart', 'reload'):
        return False, "Invalid service action."
    installed = {v['version']: v for v in get_installed_php_versions()}
    if not is_valid_php_version(version) or version not in installed or not installed[version]['has_fpm']:
        return False, f"PHP {version} FPM is not installed."

    svc = f"php{version}-fpm"
    try:
        res = subprocess.run(['systemctl', action, svc], capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            return True, f"{svc} {action}ed successfully."
        return False, f"Failed to {action} {svc}: {res.stderr.strip()}"
    except Exception as e:
        return False, f"Error managing {svc}: {str(e)}"


_ONDREJ_KEY_FPR = '14AA40EC0831756756D7F66C4F4EA0AAE5267A6C'
_ONDREJ_KEY_SOURCES = [
    (
        'PGPKeys EU (HTTPS)',
        f'https://pgpkeys.eu/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        False,
    ),
    (
        'PGPKeys UK (HTTPS)',
        f'https://pgpkeys.uk/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        False,
    ),
    (
        'Ubuntu Keyserver (HTTPS)',
        f'https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        False,
    ),
    (
        'OpenPGP Keyserver (HTTPS)',
        f'https://keys.openpgp.org/vks/v1/by-fingerprint/{_ONDREJ_KEY_FPR}',
        False,
    ),
    (
        'PGPKeys EU (HTTP port 80)',
        f'http://pgpkeys.eu/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        True,
    ),
    (
        'Ubuntu Keyserver (HTTP port 80)',
        f'http://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        True,
    ),
    (
        'MIT PGP Keyserver (HTTPS)',
        f'https://pgp.mit.edu/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        False,
    ),
]
_ONDREJ_KEY_URL = _ONDREJ_KEY_SOURCES[0][1]
_ONDREJ_KEYRING_PATH = '/etc/apt/keyrings/ondrej-php.asc'
_ONDREJ_LIST_PATH = '/etc/apt/sources.list.d/ondrej-php.list'

_PGP_BLOCK_RE = re.compile(
    rb'-----BEGIN PGP PUBLIC KEY BLOCK-----\s*[\r\n]+.+?-----END PGP PUBLIC KEY BLOCK-----',
    re.DOTALL,
)

_SURY_KEY_URL = 'https://packages.sury.org/php/apt.gpg'
_SURY_KEYRING_PATH = '/usr/share/keyrings/deb.sury.org-keyring.gpg'
_SURY_ASC_KEYRING_PATH = '/usr/share/keyrings/deb.sury.org-keyring.asc'
_SURY_LIST_PATH = '/etc/apt/sources.list.d/php.list'

_PHP_PREFS_PATH = '/etc/apt/preferences.d/99-lite-cpanel-php'
_PHP_SERIES_ALL = (
    '5.6', '7.0', '7.1', '7.2', '7.3', '7.4',
    '8.0', '8.1', '8.2', '8.3', '8.4', '8.5', '8.6', '8.7',
)
_UNVERSIONED_PHP_METAPACKAGES = (
    'php', 'php-fpm', 'php-cli', 'php-cgi', 'php-phpdbg',
    'libapache2-mod-php', 'libphp-embed', 'php-all-dev', 'php-*-all-dev',
    'php-mysql', 'php-gd', 'php-curl', 'php-gmp', 'php-mbstring',
    'php-xml', 'php-zip', 'php-bcmath', 'php-intl', 'php-json',
    'php-mongodb', 'php-imagick', 'php-redis', 'php-memcached',
    'php-soap', 'php-sqlite3', 'php-pgsql', 'php-ldap', 'php-imap',
    'php-opcache', 'php-bz2', 'php-readline', 'php-pear',
)

_APT_COMMON_OPTS = [
    '-o', 'DPkg::Lock::Timeout=60',
    '-o', 'Acquire::http::Timeout=25',
    '-o', 'Acquire::https::Timeout=25',
    '-o', 'Acquire::Retries=2',
]

_APT_INSTALL_OPTS = _APT_COMMON_OPTS + [
    '--no-install-recommends',
    '-o', 'Dpkg::Options::=--force-confdef',
    '-o', 'Dpkg::Options::=--force-confold',
]


_PHP_METAPKG_PIN_CLAUSES = (
    'Pin: origin packages.sury.org',
    'Pin: release o=deb.sury.org',
    'Pin: release o=LP-PPA-ondrej-php',
)


def _metapkg_pin_blocks():
    """Return APT preference blocks pinning unversioned Sury/Ondrej PHP metapackages to -1."""
    pkg_list = ' '.join(_UNVERSIONED_PHP_METAPACKAGES)
    return [
        f"Package: {pkg_list}\n{clause}\nPin-Priority: -1\n"
        for clause in _PHP_METAPKG_PIN_CLAUSES
    ]


def _ensure_php_metapkg_pins():
    """
    Pin unversioned Sury/Ondrej PHP metapackages (-1) so apt upgrade / installs
    never pull unrelated PHP versions (such as php8.4 or php8.5) unexpectedly.
    """
    content = '\n'.join(_metapkg_pin_blocks())
    try:
        os.makedirs(os.path.dirname(_PHP_PREFS_PATH), mode=0o755, exist_ok=True)
        if os.path.exists(_PHP_PREFS_PATH):
            with open(_PHP_PREFS_PATH, 'r') as f:
                if f.read() == content:
                    return
        with open(_PHP_PREFS_PATH, 'w') as f:
            f.write(content)
        os.chmod(_PHP_PREFS_PATH, 0o644)
    except OSError:
        pass


def _get_pkg_installed_version(pkg_name):
    """Return installed Debian version string for pkg_name, or None if not installed."""
    if not shutil.which('dpkg-query'):
        return None
    try:
        res = subprocess.run(
            ['dpkg-query', '-W', '-f=${db:Status-Abbrev}\t${Version}\n', pkg_name],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
        )
        if res.returncode != 0:
            return None
        line = res.stdout.strip()
        if '\t' in line:
            status, ver = line.split('\t', 1)
            if status.strip().startswith('ii') and ver.strip():
                return ver.strip()
    except Exception:
        pass
    return None


def _dpkg_compare_versions(ver_a, op, ver_b):
    """Compare two Debian/Ubuntu version strings using dpkg --compare-versions."""
    if not ver_a or not ver_b:
        return False
    if not shutil.which('dpkg'):
        return ver_a != ver_b if op == 'gt' else False
    try:
        res = subprocess.run(
            ['dpkg', '--compare-versions', ver_a, op, ver_b],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=5
        )
        return res.returncode == 0
    except Exception:
        return False


@contextlib.contextmanager
def _scoped_apt_isolation_prefs(target_version):
    """
    Create a temporary APT preferences file ensuring APT only installs/upgrades
    `target_version` and cannot pull unrequested PHP series (e.g. 8.4 / 8.5).
    Yields extra ['-o', 'Dir::Etc::preferences=...'] flags for apt-get.
    """
    _ensure_php_metapkg_pins()
    installed_set = {v['version'] for v in get_installed_php_versions()}
    blocks = []

    for other in _PHP_SERIES_ALL:
        if other == target_version:
            continue
        if other not in installed_set:
            blocks.append(
                f"Package: php{other} php{other}-* libapache2-mod-php{other} libphp{other}-embed\n"
                "Pin: release *\n"
                "Pin-Priority: -1\n"
            )
        else:
            missing_sapis = [
                p for p in (
                    f'php{other}-fpm',
                    f'libapache2-mod-php{other}',
                    f'libphp{other}-embed',
                    f'php{other}-cgi',
                    f'php{other}-phpdbg',
                )
                if not _get_pkg_installed_version(p)
            ]
            if missing_sapis:
                blocks.append(
                    f"Package: {' '.join(missing_sapis)}\n"
                    "Pin: release *\n"
                    "Pin-Priority: -1\n"
                )

    blocks.extend(_metapkg_pin_blocks())

    # Note: Do NOT pin php-common below Sury's version; Ubuntu 24.04's stock php-common (2:93*)
    # explicitly `Breaks: php5.6-common .. php8.2-common`, so php-common must be free to upgrade
    # to Sury's 2:101+ while unversioned metapackages (e.g. php-mongodb) stay pinned at -1.

    pref_path = None
    try:
        with tempfile.NamedTemporaryFile('w', prefix='lite_cpanel_php_pin_', suffix='.pref', delete=False) as tf:
            tf.write('\n'.join(blocks))
            pref_path = tf.name
        yield ['-o', f'Dir::Etc::preferences={pref_path}']
    finally:
        if pref_path and os.path.exists(pref_path):
            try:
                os.remove(pref_path)
            except OSError:
                pass


def _non_interactive_env():
    env = os.environ.copy()
    env['DEBIAN_FRONTEND'] = 'noninteractive'
    env['NEEDRESTART_MODE'] = 'a'
    env['NEEDRESTART_SUSPEND'] = '1'
    env['APT_LISTCHANGES_FRONTEND'] = 'none'
    env['UCF_FORCE_CONFFOLD'] = '1'
    return env


def _apt_pkg_exists(pkg_name):
    res = subprocess.run(
        ['apt-cache', 'show', pkg_name],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15
    )
    return res.returncode == 0 and 'Version:' in res.stdout


def _detect_os_info():
    """Return {'id': ..., 'id_like': ..., 'codename': ...} from /etc/os-release."""
    info = {}
    if os.path.exists('/etc/os-release'):
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    line = line.strip()
                    if '=' not in line or line.startswith('#'):
                        continue
                    k, v = line.split('=', 1)
                    info[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass

    os_id = info.get('ID', 'ubuntu').lower()
    id_like = info.get('ID_LIKE', '').lower()
    codename = info.get('UBUNTU_CODENAME') or info.get('VERSION_CODENAME') or ''
    if not codename and shutil.which('lsb_release'):
        try:
            codename = subprocess.run(
                ['lsb_release', '-sc'],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10
            ).stdout.strip()
        except Exception:
            codename = ''
    if not codename:
        codename = 'bookworm' if os_id == 'debian' else 'noble'

    return {'id': os_id, 'id_like': id_like, 'codename': codename}


def _is_html_payload(raw_bytes):
    """Return True if raw bytes appear to be an HTML document rather than a GPG keyring."""
    if not raw_bytes:
        return False
    head = raw_bytes[:512].lower()
    return b'<html' in head or b'<!doctype' in head


def _extract_pgp_block(raw_bytes):
    """Extract clean ASCII-armored PGP public key block from response bytes, or None."""
    if not raw_bytes:
        return None
    match = _PGP_BLOCK_RE.search(raw_bytes)
    if not match:
        return None
    block = match.group(0).replace(b'\r\n', b'\n').replace(b'\r', b'\n').strip() + b'\n'
    if len(block) < 128:
        return None
    return block


def _verify_ondrej_key(key_bytes, require_gpg=False):
    """Verify PGP key block structure and fingerprint against Launchpad PPA signing key."""
    if not _extract_pgp_block(key_bytes):
        return False, "Missing valid ASCII-armored PGP public key block"
    if not shutil.which('gpg'):
        if require_gpg:
            return False, "gpg binary required to verify plain HTTP key download"
        return True, None
    try:
        res = subprocess.run(
            ['gpg', '--batch', '--with-colons', '--show-keys', '-'],
            input=key_bytes,
            capture_output=True,
            timeout=10,
        )
        if res.returncode != 0:
            err = res.stderr.decode('utf-8', errors='replace').strip() or f"gpg exited {res.returncode}"
            return False, f"Invalid PGP key payload: {err}"
        out = res.stdout.decode('utf-8', errors='replace').upper()
        for line in out.splitlines():
            if line.startswith('FPR:') and _ONDREJ_KEY_FPR in line:
                return True, None
        return False, f"Key fingerprint mismatch (expected {_ONDREJ_KEY_FPR})"
    except Exception as e:
        if require_gpg:
            return False, f"Failed to verify GPG key fingerprint: {str(e)}"
        return True, None


def _urllib_fetch(url, timeout=12):
    """Fetch URL content via stdlib urllib.request as a curl fallback."""
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Lite-cPanel-MultiPHP/1.0'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            if status and status >= 400:
                return False, b'', f"urllib HTTP {status}"
            data = resp.read()
            if data:
                return True, data, None
            return False, b'', "urllib returned empty response"
    except Exception as e:
        return False, b'', f"urllib: {str(e)}"


def _curl_fetch(url, timeout=10):
    """Fetch URL content via IPv4-first curl, IPv6 fallback, and urllib fallback."""
    errors = []
    skip_urllib = False
    if shutil.which('curl'):
        for label, extra in (('IPv4', ['-4']), ('IPv6', ['-6'])):
            try:
                res = subprocess.run(
                    [
                        'curl', '-fsSL', '--http1.1',
                        '--connect-timeout', '4', '--max-time', str(timeout),
                    ] + extra + [url],
                    capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout + 3
                )
                if res.returncode == 0 and res.stdout:
                    return True, res.stdout, None
                msg = res.stderr.decode('utf-8', errors='replace').strip() or f"exited with {res.returncode}"
                errors.append(f"curl ({label}): {msg}")
                # HTTP 404 or IPv4 connect timeout need not waste another cycle in urllib
                if label == 'IPv4' and res.returncode == 22:
                    skip_urllib = True
                    break
                if label == 'IPv4' and res.returncode == 28:
                    skip_urllib = True
            except subprocess.TimeoutExpired:
                errors.append(f"curl ({label}): timed out after {timeout}s")
                if label == 'IPv4':
                    skip_urllib = True
            except Exception as e:
                errors.append(f"curl ({label}): {str(e)}")
    else:
        errors.append("curl binary not found")

    if not skip_urllib:
        ok, data, u_err = _urllib_fetch(url, timeout=min(timeout, 6))
        if ok and data:
            return True, data, None
        if u_err:
            errors.append(u_err)
    return False, b'', '; '.join(errors)


def _gpg_fetch_ondrej_key(timeout=10):
    """Fetch ondrej/php key via gpg HKPS/HKP mirrors inside an isolated temporary GNUPGHOME."""
    if not shutil.which('gpg'):
        return False, b'', "gpg HKP: gpg binary not found"
    keyservers = (
        'hkps://pgpkeys.eu',
        'hkp://pgpkeys.eu:80',
        'hkps://pgpkeys.uk',
        'hkp://keyserver.ubuntu.com:80',
    )
    errs = []
    try:
        with tempfile.TemporaryDirectory(prefix='cpanel-gpg-') as gpghome:
            os.chmod(gpghome, 0o700)
            for ks in keyservers:
                try:
                    recv = subprocess.run(
                        [
                            'gpg', '--batch', '--homedir', gpghome,
                            '--keyserver', ks,
                            '--keyserver-options', 'timeout=6',
                            '--recv-keys', _ONDREJ_KEY_FPR,
                        ],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout
                    )
                    if recv.returncode != 0:
                        msg = recv.stderr.strip() or recv.stdout.strip() or f"exited {recv.returncode}"
                        errs.append(f"{ks}: {msg}")
                        continue
                    exp = subprocess.run(
                        ['gpg', '--batch', '--homedir', gpghome, '--armor', '--export', _ONDREJ_KEY_FPR],
                        capture_output=True, stdin=subprocess.DEVNULL, timeout=10
                    )
                    if exp.returncode == 0 and exp.stdout:
                        block = _extract_pgp_block(exp.stdout)
                        if block:
                            return True, block, None
                    err = exp.stderr.decode('utf-8', errors='replace').strip() or "empty exported key"
                    errs.append(f"{ks} export: {err}")
                except subprocess.TimeoutExpired:
                    errs.append(f"{ks}: timed out after {timeout}s")
                except Exception as e:
                    errs.append(f"{ks}: {str(e)}")
    except Exception as e:
        errs.append(str(e))
    return False, b'', f"gpg HKP: {'; '.join(errs)}"


def _find_local_ondrej_key():
    """Scan local APT keyring directories for the Launchpad ondrej/php signing key."""
    if not shutil.which('gpg'):
        return None
    patterns = (
        '/etc/apt/keyrings/*.asc',
        '/etc/apt/keyrings/*.gpg',
        '/etc/apt/trusted.gpg.d/*.asc',
        '/etc/apt/trusted.gpg.d/*.gpg',
        '/usr/share/keyrings/*.asc',
        '/usr/share/keyrings/*.gpg',
    )
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))

    for path in candidates:
        try:
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                continue
            show = subprocess.run(
                ['gpg', '--batch', '--with-colons', '--show-keys', path],
                capture_output=True, timeout=5
            )
            if show.returncode != 0:
                continue
            if _ONDREJ_KEY_FPR not in show.stdout.decode('utf-8', errors='replace').upper():
                continue
            exp = subprocess.run(
                ['gpg', '--batch', '--no-default-keyring', '--keyring', path, '--armor', '--export', _ONDREJ_KEY_FPR],
                capture_output=True, timeout=5
            )
            if exp.returncode == 0 and exp.stdout:
                block = _extract_pgp_block(exp.stdout)
                if block:
                    return block
        except Exception:
            continue
    return None


def _write_ondrej_repo_files(key_block, codename):
    """Persist verified ondrej/php keyring and APT source list cleanly."""
    os.makedirs('/etc/apt/keyrings', mode=0o755, exist_ok=True)
    for stale in glob.glob('/etc/apt/sources.list.d/*ondrej*php*'):
        try:
            os.remove(stale)
        except OSError:
            pass
    with open(_ONDREJ_KEYRING_PATH, 'wb') as f:
        f.write(key_block)
    os.chmod(_ONDREJ_KEYRING_PATH, 0o644)
    with open(_ONDREJ_LIST_PATH, 'w') as f:
        f.write(f"deb [signed-by={_ONDREJ_KEYRING_PATH}] https://ppa.launchpadcontent.net/ondrej/php/ubuntu {codename} main\n")
    os.chmod(_ONDREJ_LIST_PATH, 0o644)
    _ensure_php_metapkg_pins()


def import_ondrej_key_text(raw_text):
    """Validate and install an admin-supplied ondrej/php ASCII-armored public GPG key."""
    if not raw_text or not raw_text.strip():
        return False, "Please paste the PGP public key block."
    os_info = _detect_os_info()
    if os_info['id'] == 'debian':
        return False, "Manual PPA key import is only needed on Ubuntu-based systems."
    block = _extract_pgp_block(raw_text.encode('utf-8', errors='replace'))
    if not block:
        return False, "Invalid input: no ASCII-armored PGP PUBLIC KEY BLOCK found."
    ok, err = _verify_ondrej_key(block, require_gpg=True)
    if not ok:
        return False, f"Key verification failed: {err}"
    try:
        _write_ondrej_repo_files(block, os_info['codename'])
        return True, "Verified and saved ondrej/php repository key. You can now click Install PHP."
    except Exception as e:
        return False, f"Failed to write repository key files: {str(e)}"


def _extract_signed_by_path(content, is_deb822=False):
    """Extract the signed-by keyring path from an APT source file if specified."""
    if is_deb822:
        match = re.search(r'(?mi)^Signed-By:\s*(\S+)', content)
        return match.group(1).strip() if match else None
    match = re.search(r'signed-by=([^\s\]]+)', content, re.I)
    return match.group(1).strip() if match else None


def _is_valid_keyring_file(path):
    """Verify a keyring file exists, is non-trivial, and does not contain HTML."""
    if not path or not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) < 32:
            return False
        with open(path, 'rb') as kf:
            data = kf.read()
        if _is_html_payload(data):
            return False
        if path.endswith('.asc'):
            return _extract_pgp_block(data) is not None
        return True
    except Exception:
        return False


def _configure_sury_repo(codename):
    """
    Fetch Sury PHP GPG key directly over HTTPS and dearmor it into
    /usr/share/keyrings/deb.sury.org-keyring.gpg for Ubuntu & Debian.
    """
    ok, key_bytes, err = _curl_fetch(_SURY_KEY_URL, timeout=15)
    if not ok or _is_html_payload(key_bytes):
        detail = err or 'received invalid HTML response instead of GPG keyring'
        return False, detail

    keyring_path = _SURY_KEYRING_PATH
    try:
        os.makedirs(os.path.dirname(_SURY_KEYRING_PATH), mode=0o755, exist_ok=True)
        if shutil.which('gpg'):
            dearmor = subprocess.run(
                ['gpg', '--batch', '--dearmor', '--yes'],
                input=key_bytes,
                capture_output=True,
                timeout=15,
            )
            if dearmor.returncode != 0 or not dearmor.stdout:
                d_err = dearmor.stderr.decode('utf-8', errors='replace').strip() or f"gpg exited {dearmor.returncode}"
                return False, f"Failed to dearmor Sury PHP key: {d_err}"
            out_bytes = dearmor.stdout
        else:
            if b'BEGIN PGP PUBLIC KEY BLOCK' in key_bytes:
                block = _extract_pgp_block(key_bytes)
                if not block:
                    return False, "Downloaded Sury PHP key lacked a valid PGP key block"
                keyring_path = _SURY_ASC_KEYRING_PATH
                out_bytes = block
            else:
                out_bytes = key_bytes

        # Remove stale/partial ondrej repo files so APT doesn't hit duplicate or broken sources
        for stale in glob.glob('/etc/apt/sources.list.d/*ondrej*php*'):
            try:
                os.remove(stale)
            except OSError:
                pass

        with open(keyring_path, 'wb') as f:
            f.write(out_bytes)
        os.chmod(keyring_path, 0o644)
        with open(_SURY_LIST_PATH, 'w') as f:
            f.write(f"deb [signed-by={keyring_path}] https://packages.sury.org/php/ {codename} main\n")
        os.chmod(_SURY_LIST_PATH, 0o644)
        _ensure_php_metapkg_pins()
        return True, None
    except Exception as e:
        return False, str(e)


def _has_valid_repo_source(os_id):
    """Verify an existing Sury PHP or Ondrej PPA repo file is valid and has its keyring."""
    # Check Sury repo entries (works on both Ubuntu and Debian)
    sury_candidates = glob.glob('/etc/apt/sources.list.d/*.list') + glob.glob('/etc/apt/sources.list.d/*.sources')
    if _SURY_LIST_PATH not in sury_candidates and os.path.exists(_SURY_LIST_PATH):
        sury_candidates.insert(0, _SURY_LIST_PATH)

    for path in sury_candidates:
        try:
            with open(path, 'r') as f:
                content = f.read()
            active_lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith('#')]
            active_text = '\n'.join(active_lines)
            if 'packages.sury.org/php' not in active_text.lower():
                continue
            signed_by = _extract_signed_by_path(active_text, is_deb822=path.endswith('.sources'))
            if signed_by:
                if _is_valid_keyring_file(signed_by):
                    return True
                continue
            if _is_valid_keyring_file(_SURY_KEYRING_PATH) or _is_valid_keyring_file(_SURY_ASC_KEYRING_PATH):
                return True
        except Exception:
            continue

    if os_id == 'debian':
        return False

    for path in glob.glob('/etc/apt/sources.list.d/*ondrej*php*'):
        try:
            with open(path, 'r') as f:
                content = f.read()
            lowered = content.lower()
            if 'ondrej' not in lowered or 'php' not in lowered:
                continue
            if path.endswith('.sources') and ('uris:' not in lowered or 'signed-by:' not in lowered):
                continue
            signed_by = _extract_signed_by_path(content, is_deb822=path.endswith('.sources'))
            if signed_by and not _is_valid_keyring_file(signed_by):
                continue
            if _ONDREJ_KEYRING_PATH in content and not _is_valid_keyring_file(_ONDREJ_KEYRING_PATH):
                continue
            return True
        except Exception:
            continue
    return False


def _ensure_php_repository_iter(env):
    """
    Generator that configures Sury PHP repo (packages.sury.org, primary for Ubuntu & Debian)
    with Launchpad ondrej/php PPA fallback on Ubuntu, yielding live progress events and
    finishing with {'done': True, 'ok': bool, 'error': str | None}.
    """
    os_info = _detect_os_info()
    os_id = os_info['id']
    codename = os_info['codename']

    if _has_valid_repo_source(os_id):
        yield {'done': True, 'ok': True, 'error': None}
        return

    attempts = []

    yield {'percent': 21, 'message': 'Configuring Sury PHP repository (packages.sury.org)...'}
    s_ok, s_err = _configure_sury_repo(codename)
    if s_ok:
        yield {'percent': 26, 'message': f'Configured Sury PHP repository ({codename}).'}
        yield {'done': True, 'ok': True, 'error': None}
        return
    if os_id == 'debian':
        yield {'done': True, 'ok': False, 'error': f"Failed to configure Sury PHP repository: {s_err}"}
        return
    attempts.append(f"Sury HTTPS (packages.sury.org): {s_err}")

    # Ubuntu / derivatives fallback: ondrej/php PPA
    os.makedirs('/etc/apt/keyrings', mode=0o755, exist_ok=True)
    key_block = _find_local_ondrej_key()

    if key_block is None:
        for idx, (label, url, require_gpg) in enumerate(_ONDREJ_KEY_SOURCES):
            pct = min(24, 22 + idx)
            yield {'percent': pct, 'message': f"Fetching ondrej/php GPG key from {label}..."}
            ok, raw_bytes, fetch_err = _curl_fetch(url, timeout=10)
            if not ok:
                attempts.append(f"{label}: {fetch_err}")
                continue
            candidate = _extract_pgp_block(raw_bytes)
            if not candidate:
                attempts.append(f"{label}: response did not contain a valid PGP public key block")
                continue
            v_ok, v_err = _verify_ondrej_key(candidate, require_gpg=require_gpg)
            if not v_ok:
                attempts.append(f"{label}: {v_err}")
                continue
            key_block = candidate
            break

    if key_block is None:
        yield {'percent': 25, 'message': 'Trying gpg HKP/HKPS keyserver fallbacks...'}
        g_ok, g_bytes, g_err = _gpg_fetch_ondrej_key(timeout=10)
        if g_ok and g_bytes:
            v_ok, v_err = _verify_ondrej_key(g_bytes, require_gpg=False)
            if v_ok:
                key_block = g_bytes
            else:
                attempts.append(f"gpg HKP: {v_err}")
        else:
            attempts.append(g_err)

    if key_block is not None:
        _write_ondrej_repo_files(key_block, codename)
        yield {'percent': 26, 'message': 'Configured ondrej/php repository.'}
        yield {'done': True, 'ok': True, 'error': None}
        return

    # Fallback: non-interactive add-apt-repository with --no-update and strict 30s timeout
    if shutil.which('add-apt-repository'):
        yield {'percent': 25, 'message': 'Trying add-apt-repository fallback for ondrej/php...'}
        try:
            res = subprocess.run(
                ['add-apt-repository', '-y', '--no-update', 'ppa:ondrej/php'],
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30
            )
            if res.returncode == 0:
                yield {'percent': 26, 'message': 'Configured ondrej/php repository.'}
                yield {'done': True, 'ok': True, 'error': None}
                return
            msg = res.stderr.strip() or res.stdout.strip() or f"exited {res.returncode}"
            attempts.append(f"add-apt-repository: {msg}")
        except subprocess.TimeoutExpired:
            attempts.append("add-apt-repository: timed out after 30s")
        except Exception as e:
            attempts.append(f"add-apt-repository: {str(e)}")
    else:
        attempts.append("add-apt-repository: binary not found")

    detail = ' | '.join(attempts) if attempts else 'Unknown error'
    yield {'done': True, 'ok': False, 'error': f"Failed to configure PHP repository: {detail}"}


def _ensure_php_repository(env):
    """
    Configure Sury PHP repo (packages.sury.org, primary for Ubuntu & Debian)
    with Launchpad ondrej/php PPA fallback on Ubuntu over HTTPS without
    interactive prompts or keyserver HKP hangs.
    """
    last = {'ok': False, 'error': 'Unknown repository configuration error'}
    for event in _ensure_php_repository_iter(env):
        if event.get('done'):
            last = event
    return bool(last.get('ok')), last.get('error')


def _run_cmd_streaming(cmd, env=None, timeout=600):
    """
    Execute a command non-interactively and yield live events:
    ('line', text), ('tick', None), or ('done', (returncode, tail_lines)).
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        errors='replace',
    )
    q = queue.Queue()

    def _reader():
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if line:
                    q.put(('line', line))
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            q.put(('eof', None))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    tail = []
    deadline = time.monotonic() + timeout

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                raise subprocess.TimeoutExpired(cmd, timeout)

            try:
                kind, payload = q.get(timeout=min(2.0, remaining))
            except queue.Empty:
                yield ('tick', None)
                continue

            if kind == 'eof':
                break

            tail.append(payload)
            if len(tail) > 25:
                tail.pop(0)
            yield ('line', payload)

        rc = proc.wait(timeout=15)
        t.join(timeout=2)
        yield ('done', (rc, tail))
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def _restore_default_php_alternatives(version):
    """Restore update-alternatives symlinks to `version` after installing another PHP version."""
    for tool in ('php', 'phar', 'phar.phar', 'phpize', 'php-config'):
        target_bin = f'/usr/bin/{tool}{version}'
        if os.path.exists(target_bin):
            subprocess.run(
                ['update-alternatives', '--set', tool, target_bin],
                capture_output=True, stdin=subprocess.DEVNULL
            )


def _stream_apt_update_with_fallback(env, start_prog=28, retry_prog=30, max_prog=40):
    """
    Stream `apt-get update -y` with an IPv4 fallback on failure.
    Yields intermediate event dicts and ends with {'done': True, 'ok': bool, 'used_ipv4': bool, 'error': str}.
    """
    prog = start_prog
    used_ipv4 = False
    apt_err = ''
    for force_v4 in (False, True):
        if force_v4:
            prog = max(prog, retry_prog)
        extra_v4 = ['-o', 'Acquire::ForceIPv4=true'] if force_v4 else []
        update_cmd = ['apt-get', 'update', '-y'] + _APT_COMMON_OPTS + extra_v4
        rc = 1
        tail = []
        step_timeout = 75 if not force_v4 else 180
        try:
            for ev, data in _run_cmd_streaming(update_cmd, env=env, timeout=step_timeout):
                if ev == 'line':
                    prog = min(max_prog, prog + 1)
                    yield {'progress': prog, 'message': 'Refreshing APT package lists...', 'detail': data}
                elif ev == 'tick':
                    prog = min(max_prog, prog + 1)
                    yield {'progress': prog, 'message': 'Refreshing APT package lists...'}
                elif ev == 'done':
                    rc, tail = data
        except subprocess.TimeoutExpired:
            rc = 124
            tail = [f"apt-get update timed out after {step_timeout}s"]
        if rc == 0:
            used_ipv4 = force_v4
            yield {'done': True, 'ok': True, 'used_ipv4': used_ipv4, 'error': ''}
            return
        apt_err = '; '.join(tail[-3:]) or f"apt-get update exited with {rc}"
        if not force_v4:
            prog = max(prog, retry_prog)
            yield {'progress': prog, 'message': 'Retrying APT refresh over IPv4...', 'detail': apt_err}

    yield {'done': True, 'ok': False, 'used_ipv4': False, 'error': apt_err}


def _preserve_versioned_pecl_before_common_upgrade():
    """
    When Ubuntu 24.04's php-common (2:93*) upgrades to Sury 2:101+ to allow PHP <= 8.2,
    APT removes obsolete unversioned PECL wrappers (like php-mongodb). Mark already-installed
    versioned extension packages (like php8.3-mongodb) manual so apt autoremove preserves them.
    """
    if not shutil.which('apt-mark'):
        return
    php_common_ver = _get_pkg_installed_version('php-common')
    if not php_common_ver or not _dpkg_compare_versions(php_common_ver, 'lt', '2:95~'):
        return
    installed_php_pkgs = _get_all_installed_php_dpkg_versions()
    if not installed_php_pkgs:
        return
    checked_meta = {}
    to_mark = []
    for pkg in sorted(installed_php_pkgs):
        m = re.fullmatch(r'php\d+\.\d+-([a-z0-9.+-]+)', pkg)
        if not m:
            continue
        meta = f'php-{m.group(1)}'
        if meta not in _UNVERSIONED_PHP_METAPACKAGES:
            continue
        if meta not in checked_meta:
            checked_meta[meta] = bool(_get_pkg_installed_version(meta))
        if checked_meta[meta]:
            to_mark.append(pkg)
    if to_mark:
        try:
            subprocess.run(
                ['apt-mark', 'manual'] + to_mark,
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15
            )
        except Exception:
            pass


def install_php_generator(version, extensions=None):
    """Generator for live-streaming a new PHP version installation (admin-only)."""
    def emit(progress, message, error=False, success=False, detail=''):
        payload = {
            'progress': progress,
            'message': message,
            'error': error,
            'success': success,
        }
        if detail:
            payload['detail'] = detail
        return json.dumps(payload) + "\n"

    if version not in SUPPORTED_PHP_VERSIONS:
        yield emit(100, f"Unsupported PHP version: {version}", error=True)
        return

    requested_exts = []
    if extensions:
        for ext in extensions:
            if ext in INSTALLABLE_EXTENSIONS and ext not in requested_exts:
                requested_exts.append(ext)
    else:
        requested_exts = list(DEFAULT_CORE_EXTENSIONS)

    try:
        env = _non_interactive_env()
        prev_default = get_default_php_version()

        yield emit(10, f"Checking package availability for PHP {version}...")

        if not _apt_pkg_exists(f'php{version}-fpm'):
            yield emit(20, "Configuring PHP repository (ondrej/php / Sury)...")
            repo_ok = False
            repo_err = "Unknown repository configuration error"
            for event in _ensure_php_repository_iter(env):
                if event.get('done'):
                    repo_ok = bool(event.get('ok'))
                    repo_err = event.get('error')
                else:
                    yield emit(event.get('percent', 20), event.get('message', ''))
            if not repo_ok:
                yield emit(20, repo_err, error=True)
                return

        yield emit(28, "Refreshing APT package lists...")
        apt_ok = False
        used_ipv4 = False
        apt_err = ''
        for upd_ev in _stream_apt_update_with_fallback(env, start_prog=28, retry_prog=30, max_prog=40):
            if upd_ev.get('done'):
                apt_ok = bool(upd_ev.get('ok'))
                used_ipv4 = bool(upd_ev.get('used_ipv4'))
                apt_err = upd_ev.get('error', '')
            else:
                yield emit(upd_ev['progress'], upd_ev['message'], detail=upd_ev.get('detail', ''))

        if not apt_ok and not _apt_pkg_exists(f'php{version}-fpm'):
            yield emit(30, f"apt-get update failed: {apt_err}", error=True)
            return

        if not _apt_pkg_exists(f'php{version}-fpm'):
            yield emit(40, f"Package php{version}-fpm is not available for this OS release.", error=True)
            return

        packages = [f'php{version}-fpm', f'php{version}-cli', f'php{version}-common']
        if version == '7.4' and _apt_pkg_exists('php7.4-json'):
            packages.append('php7.4-json')
        for ext in requested_exts:
            pkg = f'php{version}-{ext}'
            if pkg not in packages and _apt_pkg_exists(pkg):
                packages.append(pkg)

        yield emit(45, f"Installing PHP {version} FPM, CLI, and {len(packages) - 3} extensions...")
        _preserve_versioned_pecl_before_common_upgrade()
        install_attempts = [True] if used_ipv4 else [False, True]
        prog = 45
        rc = 1
        tail = []
        last_detail = ''
        with _scoped_apt_isolation_prefs(version) as pin_opts:
            for force_v4 in install_attempts:
                extra_v4 = ['-o', 'Acquire::ForceIPv4=true'] if force_v4 else []
                install_cmd = ['apt-get', 'install', '-y'] + _APT_INSTALL_OPTS + pin_opts + extra_v4 + packages
                rc = 1
                tail = []
                step_timeout = 300 if not force_v4 else 600
                try:
                    for ev, data in _run_cmd_streaming(install_cmd, env=env, timeout=step_timeout):
                        if ev == 'line':
                            last_detail = data
                            prog = min(84, prog + 1)
                            yield emit(prog, f"Installing PHP {version} packages...", detail=data)
                        elif ev == 'tick':
                            prog = min(84, prog + 1)
                            yield emit(prog, f"Installing PHP {version} packages...", detail=last_detail)
                        elif ev == 'done':
                            rc, tail = data
                except subprocess.TimeoutExpired:
                    rc = 124
                    tail = [f"apt-get install timed out after {step_timeout}s"]
                if rc == 0:
                    break
                if not force_v4 and len(install_attempts) > 1:
                    yield emit(prog, f"Retrying PHP {version} package install over IPv4...", detail='; '.join(tail[-2:]))

        if rc != 0:
            err_detail = '; '.join(tail[-4:]) or f"apt-get install exited with {rc}"
            yield emit(prog, f"Installation failed: {err_detail}", error=True)
            return

        # Keep existing CLI default PHP intact when adding another PHP version
        if prev_default and prev_default != version and get_default_php_version() != prev_default:
            _restore_default_php_alternatives(prev_default)

        yield emit(88, f"Enabling and starting php{version}-fpm service...")
        subprocess.run(['systemctl', 'enable', f'php{version}-fpm'], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        start_res = subprocess.run(['systemctl', 'start', f'php{version}-fpm'], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        if start_res.returncode != 0:
            yield emit(88, f"Installed, but failed to start php{version}-fpm: {start_res.stderr.strip()}", error=True)
            return

        if shutil.which('a2enmod'):
            yield emit(94, "Ensuring Apache proxy_fcgi integration is enabled...")
            subprocess.run(['a2enmod', 'proxy', 'proxy_fcgi', 'setenvif'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
            if _get_service_status('apache2') == 'active':
                subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        if _get_service_status('nginx') == 'active':
            subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        yield emit(100, f"PHP {version} installed and running successfully!", success=True)
    except subprocess.TimeoutExpired:
        yield emit(0, f"Installation timed out while setting up PHP {version}. Please check /var/log/apt/term.log and retry.", error=True)
    except Exception as e:
        yield emit(0, f"Installation error: {str(e)}", error=True)


def uninstall_php_version(version):
    """Uninstall a non-default, unused PHP version (admin-only)."""
    if not is_valid_php_version(version):
        return False, "Invalid PHP version."

    installed = {v['version']: v for v in get_installed_php_versions()}
    if version not in installed:
        return False, f"PHP {version} is not installed."

    default_ver = get_default_php_version()
    if version == default_ver:
        return False, f"Cannot uninstall PHP {version} because it is the system default. Switch default PHP first."

    in_use = [d['domain'] for d in get_php_domains('admin') if d['php_version'] == version]
    if in_use:
        sample = ', '.join(in_use[:5])
        suffix = f" (+{len(in_use) - 5} more)" if len(in_use) > 5 else ""
        return False, f"Cannot uninstall PHP {version}: currently assigned to {sample}{suffix}. Switch those domains first."

    try:
        subprocess.run(['systemctl', 'stop', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        subprocess.run(['systemctl', 'disable', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        if shutil.which('a2disconf'):
            subprocess.run(['a2disconf', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        dpkg_res = subprocess.run(
            [
                'dpkg-query', '-W', '-f=${Package}\t${db:Status-Abbrev}\n',
                f'php{version}*', f'libapache2-mod-php{version}*', f'libphp{version}-embed',
            ],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
        )
        pkg_re = re.compile(rf'^(?:php|libapache2-mod-php|libphp){re.escape(version)}(?:-[a-z0-9.+-]+)?$')
        pkgs = []
        for line in dpkg_res.stdout.splitlines():
            parts = line.split('\t')
            pkg = parts[0].strip()
            status = parts[1].strip() if len(parts) > 1 else 'ii'
            if status.startswith('ii') and pkg_re.fullmatch(pkg) and pkg not in pkgs:
                pkgs.append(pkg)

        if pkgs:
            env = _non_interactive_env()
            sim = subprocess.run(
                ['apt-get', 'purge', '-s', '-y'] + _APT_COMMON_OPTS + pkgs,
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30
            )
            protected = {'phpmyadmin', 'apache2', 'nginx', 'mariadb-server', 'mysql-server'}
            remv_protected = []
            for sim_line in sim.stdout.splitlines():
                if sim_line.startswith('Remv '):
                    remv_pkg = sim_line.split()[1].strip()
                    if remv_pkg in protected and remv_pkg not in remv_protected:
                        remv_protected.append(remv_pkg)
            if remv_protected:
                return False, (
                    f"Cannot safely purge PHP {version}: APT would also remove {', '.join(remv_protected)} "
                    "(held by an unversioned PHP metapackage). Keep default PHP metapackages intact first."
                )

            purge = subprocess.run(
                ['apt-get', 'purge', '-y'] + _APT_COMMON_OPTS + pkgs,
                capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=300
            )
            if purge.returncode != 0:
                return False, f"Failed to purge PHP {version} packages: {purge.stderr.strip()}"
            subprocess.run(
                ['apt-get', 'autoremove', '-y'] + _APT_COMMON_OPTS,
                capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=180
            )

        if _get_service_status('apache2') == 'active':
            subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        return True, f"PHP {version} uninstalled successfully."
    except subprocess.TimeoutExpired:
        return False, f"Uninstall timed out while removing PHP {version} packages."
    except Exception as e:
        return False, f"Uninstall failed: {str(e)}"


def install_php_extension(version, ext):
    """Install a single PHP extension package for an installed PHP version (admin-only)."""
    if not is_valid_php_version(version):
        return False, "Invalid PHP version."
    if ext not in INSTALLABLE_EXTENSIONS:
        return False, f"Extension '{ext}' is not in the allowed extensions list."

    installed = {v['version'] for v in get_installed_php_versions()}
    if version not in installed:
        return False, f"PHP {version} is not installed."

    pkg = f'php{version}-{ext}'
    try:
        env = _non_interactive_env()
        last_err = ''
        with _scoped_apt_isolation_prefs(version) as pin_opts:
            for force_v4 in (False, True):
                extra_v4 = ['-o', 'Acquire::ForceIPv4=true'] if force_v4 else []
                try:
                    res = subprocess.run(
                        ['apt-get', 'install', '-y'] + _APT_INSTALL_OPTS + pin_opts + extra_v4 + [pkg],
                        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                        timeout=90 if not force_v4 else 180
                    )
                    if res.returncode == 0:
                        if _get_service_status(f'php{version}-fpm') == 'active':
                            subprocess.run(['systemctl', 'reload', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
                        return True, f"Extension {pkg} installed successfully."
                    last_err = res.stderr.strip() or res.stdout.strip() or f"apt-get exited with {res.returncode}"
                except subprocess.TimeoutExpired:
                    last_err = f"Timed out installing {pkg}"
        return False, f"Failed to install {pkg}: {last_err}"
    except Exception as e:
        return False, f"Error installing {pkg}: {str(e)}"


def _parse_apt_cache_policy(policy_stdout):
    """Parse `apt-cache policy` output into {pkg: {'inst': str, 'cand': str}}."""
    parsed = {}
    cur_pkg = None
    for raw in policy_stdout.splitlines():
        if raw and not raw[0].isspace() and raw.rstrip().endswith(':'):
            cur_pkg = raw.rstrip()[:-1].strip()
            parsed[cur_pkg] = {'inst': '', 'cand': ''}
        elif cur_pkg:
            s = raw.strip()
            if s.startswith('Installed:'):
                parsed[cur_pkg]['inst'] = s.split(':', 1)[1].strip()
            elif s.startswith('Candidate:'):
                parsed[cur_pkg]['cand'] = s.split(':', 1)[1].strip()
    return parsed


def get_php_updates_status(installed_versions=None):
    """
    Inspect local APT cache to report available patch/security updates per installed
    PHP version without performing network requests.
    """
    if installed_versions is None:
        installed_versions = get_installed_php_versions()
    ver_list = [v['version'] for v in installed_versions if is_valid_php_version(v.get('version', ''))]
    if not ver_list:
        return {}

    dpkg_pkgs = _get_all_installed_php_dpkg_versions()
    pkgs_by_ver = {}
    all_pkgs = []
    for ver in ver_list:
        ver_re = re.compile(rf'^php{re.escape(ver)}(?:-[a-z0-9.+-]+)?$')
        vpkgs = {p: v for p, v in dpkg_pkgs.items() if ver_re.fullmatch(p)}
        pkgs_by_ver[ver] = vpkgs
        all_pkgs.extend(sorted(vpkgs.keys()))

    policy = {}
    if all_pkgs and shutil.which('apt-cache'):
        try:
            res = subprocess.run(
                ['apt-cache', 'policy'] + all_pkgs,
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
            )
            if res.returncode == 0 and res.stdout:
                policy = _parse_apt_cache_policy(res.stdout)
        except Exception:
            policy = {}

    updates = {}
    meta_by_ver = {v['version']: v for v in installed_versions}
    for ver in ver_list:
        vpkgs = pkgs_by_ver.get(ver, {})
        upgradable = []
        for pkg, inst_ver in sorted(vpkgs.items()):
            pinfo = policy.get(pkg, {})
            cand = pinfo.get('cand', '')
            inst = pinfo.get('inst', '') or inst_ver
            if not cand or cand == '(none)' or not inst or inst == '(none)':
                continue
            if _dpkg_compare_versions(cand, 'gt', inst):
                upgradable.append(pkg)

        core_pkg = next(
            (p for p in (f'php{ver}-fpm', f'php{ver}-cli', f'php{ver}-common') if p in vpkgs),
            None
        )
        cur_pkg_ver = vpkgs.get(core_pkg, '') if core_pkg else meta_by_ver.get(ver, {}).get('pkg_version', '')
        cur_full = _extract_upstream_version(cur_pkg_ver) or meta_by_ver.get(ver, {}).get('full_version', ver)

        upgradable_core = next(
            (p for p in (f'php{ver}-fpm', f'php{ver}-cli', f'php{ver}-common') if p in upgradable),
            None
        )
        cand_pkg_ver = cur_pkg_ver
        cand_full = cur_full
        update_label = ''
        if upgradable_core:
            cand_pkg_ver = policy.get(upgradable_core, {}).get('cand', cur_pkg_ver)
            cand_full = _extract_upstream_version(cand_pkg_ver) or cur_full
            if cand_full and cand_full != cur_full:
                update_label = f"{cur_full} → {cand_full}"
            else:
                update_label = f"{cur_full} (patch pkg)"
        elif upgradable:
            cand_pkg_ver = policy.get(upgradable[0], {}).get('cand', '')
            update_label = f"{len(upgradable)} pkg(s)"

        updates[ver] = {
            'has_update': bool(upgradable),
            'upgradable_count': len(upgradable),
            'upgradable_packages': upgradable,
            'current_pkg_version': cur_pkg_ver,
            'current_full_version': cur_full,
            'candidate_pkg_version': cand_pkg_ver,
            'candidate_full_version': cand_full,
            'update_label': update_label,
        }
    return updates


def check_php_updates():
    """Refresh APT repository metadata over IPv4-safe transport and report PHP updates."""
    try:
        env = _non_interactive_env()
        _ensure_php_metapkg_pins()
        apt_ok = False
        apt_err = ''
        for upd_ev in _stream_apt_update_with_fallback(env, start_prog=0, retry_prog=0, max_prog=100):
            if upd_ev.get('done'):
                apt_ok = bool(upd_ev.get('ok'))
                apt_err = upd_ev.get('error', '')
        updates = get_php_updates_status()
        if not apt_ok:
            return False, f"Failed to refresh APT lists: {apt_err}", updates
        avail = [(v, info) for v, info in updates.items() if info.get('has_update')]
        if avail:
            summary = ', '.join(
                f"PHP {v} ({info['update_label']})" if info.get('update_label') else f"PHP {v}"
                for v, info in avail
            )
            return True, f"Updates available for {len(avail)} PHP version(s): {summary}.", updates
        return True, "All installed PHP versions are up to date.", updates
    except Exception as e:
        return False, f"Failed to check PHP updates: {str(e)}", {}


def update_php_generator(version='all'):
    """
    Generator for live-streaming isolated `--only-upgrade` package updates for one
    installed PHP version or 'all' installed versions (admin-only).
    """
    def emit(progress, message, error=False, success=False, detail=''):
        payload = {
            'progress': progress,
            'message': message,
            'error': error,
            'success': success,
        }
        if detail:
            payload['detail'] = detail
        return json.dumps(payload) + "\n"

    installed = get_installed_php_versions()
    installed_map = {v['version']: v for v in installed}

    if version == 'all':
        target_versions = [v['version'] for v in installed]
    elif is_valid_php_version(version) and version in installed_map:
        target_versions = [version]
    else:
        yield emit(100, f"PHP {version} is not installed or is invalid.", error=True)
        return

    if not target_versions:
        yield emit(100, "No installed PHP versions found to update.", error=True)
        return

    try:
        env = _non_interactive_env()
        prev_default = get_default_php_version()
        was_fpm_active = {
            ver: (_get_service_status(f'php{ver}-fpm') == 'active')
            for ver in target_versions
        }

        label = "all installed PHP versions" if version == 'all' else f"PHP {version}"
        yield emit(10, f"Checking updates for {label}...")

        apt_ok = False
        used_ipv4 = False
        apt_err = ''
        for upd_ev in _stream_apt_update_with_fallback(env, start_prog=15, retry_prog=22, max_prog=35):
            if upd_ev.get('done'):
                apt_ok = bool(upd_ev.get('ok'))
                used_ipv4 = bool(upd_ev.get('used_ipv4'))
                apt_err = upd_ev.get('error', '')
            else:
                yield emit(upd_ev['progress'], upd_ev['message'], detail=upd_ev.get('detail', ''))

        target_meta = [installed_map[v] for v in target_versions]
        updates_map = get_php_updates_status(target_meta)
        to_update = [
            (ver, updates_map[ver])
            for ver in target_versions
            if updates_map.get(ver, {}).get('has_update') and updates_map[ver].get('upgradable_packages')
        ]

        if not apt_ok and not to_update:
            yield emit(35, f"Failed to check PHP updates: {apt_err}", error=True)
            return

        if not to_update:
            if version == 'all':
                yield emit(100, "All installed PHP versions are already up to date.", success=True)
            else:
                cur_full = installed_map[version].get('full_version', version)
                yield emit(100, f"PHP {version} is already up to date ({cur_full}).", success=True)
            return

        yield emit(42, f"Found updates for {len(to_update)} PHP version(s)...")
        prog = 42
        total_targets = len(to_update)
        updated_vers = []

        for idx, (ver, uinfo) in enumerate(to_update):
            pkgs = uinfo['upgradable_packages']
            cap = 45 + int(40 * (idx + 1) / total_targets)
            yield emit(prog, f"Updating PHP {ver} ({uinfo.get('update_label') or f'{len(pkgs)} pkg(s)'})...")

            install_attempts = [True] if used_ipv4 else [False, True]
            rc = 1
            tail = []
            last_detail = ''
            with _scoped_apt_isolation_prefs(ver) as pin_opts:
                for force_v4 in install_attempts:
                    extra_v4 = ['-o', 'Acquire::ForceIPv4=true'] if force_v4 else []
                    upgrade_cmd = (
                        ['apt-get', 'install', '-y', '--only-upgrade']
                        + _APT_INSTALL_OPTS
                        + pin_opts
                        + extra_v4
                        + pkgs
                    )
                    rc = 1
                    tail = []
                    step_timeout = 300 if not force_v4 else 600
                    try:
                        for ev, data in _run_cmd_streaming(upgrade_cmd, env=env, timeout=step_timeout):
                            if ev == 'line':
                                last_detail = data
                                prog = min(cap, max(prog, 45) + 1) if prog < cap else cap
                                yield emit(prog, f"Updating PHP {ver} packages...", detail=data)
                            elif ev == 'tick':
                                prog = min(cap, max(prog, 45) + 1) if prog < cap else cap
                                yield emit(prog, f"Updating PHP {ver} packages...", detail=last_detail)
                            elif ev == 'done':
                                rc, tail = data
                    except subprocess.TimeoutExpired:
                        rc = 124
                        tail = [f"apt-get upgrade timed out after {step_timeout}s"]
                    if rc == 0:
                        break
                    if not force_v4 and len(install_attempts) > 1:
                        yield emit(prog, f"Retrying PHP {ver} update over IPv4...", detail='; '.join(tail[-2:]))

            if rc != 0:
                err_detail = '; '.join(tail[-4:]) or f"apt-get exited with {rc}"
                yield emit(prog, f"Update failed for PHP {ver}: {err_detail}", error=True)
                return
            updated_vers.append(ver)

        # Preserve pre-existing system default PHP symlinks
        if prev_default and get_default_php_version() != prev_default:
            _restore_default_php_alternatives(prev_default)

        yield emit(90, "Restarting updated PHP-FPM services...")
        for ver in updated_vers:
            if was_fpm_active.get(ver):
                res = subprocess.run(
                    ['systemctl', 'restart', f'php{ver}-fpm'],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
                )
                if res.returncode != 0:
                    yield emit(90, f"Packages updated, but failed to restart php{ver}-fpm: {res.stderr.strip()}", error=True)
                    return

        if _get_service_status('apache2') == 'active':
            subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        if _get_service_status('nginx') == 'active':
            subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

        after_map = {v['version']: v for v in get_installed_php_versions()}
        summary_parts = []
        for ver, uinfo in to_update:
            old_v = uinfo.get('current_full_version', ver)
            new_v = after_map.get(ver, {}).get('full_version', uinfo.get('candidate_full_version', ver))
            summary_parts.append(f"PHP {ver} ({old_v} → {new_v})" if old_v != new_v else f"PHP {ver} ({new_v})")

        yield emit(100, f"Updated {', '.join(summary_parts)} successfully!", success=True)
    except subprocess.TimeoutExpired:
        yield emit(0, "PHP update timed out. Please check /var/log/apt/term.log and retry.", error=True)
    except Exception as e:
        yield emit(0, f"PHP update error: {str(e)}", error=True)


def get_php_ini_settings(version, sapi='fpm'):
    """Read common PHP directives from /etc/php/{version}/{sapi}/php.ini."""
    if not is_valid_php_version(version) or sapi not in ('fpm', 'apache2', 'cli'):
        return None, {}
    ini_path = f'/etc/php/{version}/{sapi}/php.ini'
    if not os.path.exists(ini_path):
        return None, {}

    values = {d['key']: '' for d in INI_DIRECTIVES}
    keys = set(values.keys())

    try:
        with open(ini_path, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith(';') or stripped.startswith('['):
                    continue
                if '=' in stripped:
                    k, v = stripped.split('=', 1)
                    k = k.strip()
                    if k in keys:
                        # Strip inline comment unless inside quotes
                        v = v.strip()
                        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                            v = v[1:-1]
                        elif ';' in v:
                            v = v.split(';', 1)[0].strip()
                        values[k] = v
        return ini_path, values
    except Exception:
        return ini_path, values


def save_php_ini_settings(version, sapi, submitted):
    """Validate and save common PHP INI directives and reload the matching PHP service."""
    if not is_valid_php_version(version) or sapi not in ('fpm', 'apache2', 'cli'):
        return False, "Invalid PHP version or SAPI."

    installed = {v['version'] for v in get_installed_php_versions()}
    if version not in installed:
        return False, f"PHP {version} is not installed."

    ini_path = f'/etc/php/{version}/{sapi}/php.ini'
    if not os.path.exists(ini_path):
        return False, f"INI file not found: {ini_path}"

    updates = {}
    for d in INI_DIRECTIVES:
        key = d['key']
        if key not in submitted:
            continue
        val = submitted[key].strip()
        if not val:
            continue
        if not d['pattern'].fullmatch(val):
            return False, f"Invalid value for {d['label']}: '{val}'."
        if key == 'display_errors':
            val = 'On' if val.lower() == 'on' else 'Off'
        updates[key] = val

    if not updates:
        return False, "No settings provided to update."

    try:
        with open(ini_path, 'r') as f:
            lines = f.readlines()

        remaining = dict(updates)
        new_lines = []
        for line in lines:
            stripped = line.strip()
            replaced = False
            if stripped and not stripped.startswith(';') and not stripped.startswith('[') and '=' in stripped:
                k = stripped.split('=', 1)[0].strip()
                if k in remaining:
                    new_lines.append(f"{k} = {remaining.pop(k)}\n")
                    replaced = True
            if not replaced:
                new_lines.append(line)

        if remaining:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines[-1] += '\n'
            new_lines.append("\n; Added by Lite cPanel Multi-PHP Manager\n")
            for k, v in remaining.items():
                new_lines.append(f"{k} = {v}\n")

        with open(ini_path, 'w') as f:
            f.writelines(new_lines)

        if sapi == 'fpm' and _get_service_status(f'php{version}-fpm') == 'active':
            subprocess.run(['systemctl', 'reload', f'php{version}-fpm'], capture_output=True)
        elif sapi == 'apache2' and _get_service_status('apache2') == 'active':
            subprocess.run(['systemctl', 'reload', 'apache2'], capture_output=True)

        return True, f"PHP {version} ({sapi.upper()}) configuration saved and service reloaded."
    except Exception as e:
        return False, f"Failed to save php.ini: {str(e)}"

