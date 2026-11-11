import os
import re
import glob
import json
import time
import queue
import shutil
import tempfile
import threading
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

        ini_files = {}
        for sapi in ('fpm', 'apache2', 'cli'):
            ini_path = f'/etc/php/{ver}/{sapi}/php.ini'
            if os.path.exists(ini_path):
                ini_files[sapi] = ini_path

        result.append({
            'version': ver,
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
        'MIT PGP Keyserver (HTTPS)',
        f'https://pgp.mit.edu/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        False,
    ),
    (
        'Ubuntu Keyserver (HTTP port 80)',
        f'http://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{_ONDREJ_KEY_FPR}',
        True,
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
_SURY_KEYRING_PATH = '/etc/apt/keyrings/sury-php.gpg'
_SURY_LIST_PATH = '/etc/apt/sources.list.d/php.list'

_APT_COMMON_OPTS = [
    '-o', 'DPkg::Lock::Timeout=60',
    '-o', 'Acquire::http::Timeout=25',
    '-o', 'Acquire::https::Timeout=25',
    '-o', 'Acquire::Retries=2',
]

_APT_INSTALL_OPTS = _APT_COMMON_OPTS + [
    '-o', 'Dpkg::Options::=--force-confdef',
    '-o', 'Dpkg::Options::=--force-confold',
]


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
    return res.returncode == 0 and bool(res.stdout.strip())


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


def _curl_fetch(url, timeout=12):
    """Fetch URL content via IPv4-first curl, default stack fallback, and urllib fallback."""
    errors = []
    if shutil.which('curl'):
        for label, extra in (('IPv4', ['-4']), ('default', [])):
            try:
                res = subprocess.run(
                    [
                        'curl', '-fsSL', '--http1.1', '--retry', '1', '--retry-delay', '1',
                        '--connect-timeout', '5', '--max-time', str(timeout),
                    ] + extra + [url],
                    capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout + 5
                )
                if res.returncode == 0 and res.stdout:
                    return True, res.stdout, None
                msg = res.stderr.decode('utf-8', errors='replace').strip() or f"exited with {res.returncode}"
                errors.append(f"curl ({label}): {msg}")
            except subprocess.TimeoutExpired:
                errors.append(f"curl ({label}): timed out after {timeout}s")
            except Exception as e:
                errors.append(f"curl ({label}): {str(e)}")
    else:
        errors.append("curl binary not found")

    ok, data, u_err = _urllib_fetch(url, timeout=min(timeout, 12))
    if ok and data:
        return True, data, None
    if u_err:
        errors.append(u_err)
    return False, b'', '; '.join(errors)


def _gpg_fetch_ondrej_key(timeout=15):
    """Fetch ondrej/php key via gpg HKP port 80 inside an isolated temporary GNUPGHOME."""
    if not shutil.which('gpg'):
        return False, b'', "gpg HKP: gpg binary not found"
    try:
        with tempfile.TemporaryDirectory(prefix='cpanel-gpg-') as gpghome:
            os.chmod(gpghome, 0o700)
            recv = subprocess.run(
                [
                    'gpg', '--batch', '--homedir', gpghome,
                    '--keyserver', 'hkp://keyserver.ubuntu.com:80',
                    '--recv-keys', _ONDREJ_KEY_FPR,
                ],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout
            )
            if recv.returncode != 0:
                msg = recv.stderr.strip() or recv.stdout.strip() or f"exited {recv.returncode}"
                return False, b'', f"gpg HKP: {msg}"
            exp = subprocess.run(
                ['gpg', '--batch', '--homedir', gpghome, '--armor', '--export', _ONDREJ_KEY_FPR],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=10
            )
            if exp.returncode == 0 and exp.stdout:
                block = _extract_pgp_block(exp.stdout)
                if block:
                    return True, block, None
            err = exp.stderr.decode('utf-8', errors='replace').strip() or "empty exported key"
            return False, b'', f"gpg HKP export: {err}"
    except subprocess.TimeoutExpired:
        return False, b'', f"gpg HKP: timed out after {timeout}s"
    except Exception as e:
        return False, b'', f"gpg HKP: {str(e)}"


def _has_valid_repo_source(os_id):
    """Verify an existing Ondrej/Sury repo file is non-empty and references its keyring if applicable."""
    if os_id == 'debian':
        if not os.path.exists(_SURY_LIST_PATH) or not os.path.exists(_SURY_KEYRING_PATH):
            return False
        try:
            with open(_SURY_LIST_PATH, 'r') as f:
                content = f.read()
            if 'packages.sury.org' not in content or os.path.getsize(_SURY_KEYRING_PATH) == 0:
                return False
            with open(_SURY_KEYRING_PATH, 'rb') as kf:
                return not _is_html_payload(kf.read())
        except Exception:
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
            if _ONDREJ_KEYRING_PATH in content:
                if not os.path.exists(_ONDREJ_KEYRING_PATH):
                    continue
                with open(_ONDREJ_KEYRING_PATH, 'rb') as kf:
                    if not _extract_pgp_block(kf.read()):
                        continue
            return True
        except Exception:
            continue
    return False


def _ensure_php_repository_iter(env):
    """
    Generator that configures ondrej/php PPA (Ubuntu) or Sury PHP repo (Debian),
    yielding live progress events and finishing with {'done': True, 'ok': bool, 'error': str | None}.
    """
    os_info = _detect_os_info()
    os_id = os_info['id']
    codename = os_info['codename']

    if os_id == 'debian':
        if _has_valid_repo_source('debian'):
            yield {'done': True, 'ok': True, 'error': None}
            return
        os.makedirs('/etc/apt/keyrings', mode=0o755, exist_ok=True)
        yield {'percent': 21, 'message': 'Downloading Sury PHP GPG key (packages.sury.org)...'}
        ok, key_bytes, err = _curl_fetch(_SURY_KEY_URL, timeout=15)
        if not ok or _is_html_payload(key_bytes):
            detail = err or 'received invalid HTML response instead of GPG keyring'
            yield {'done': True, 'ok': False, 'error': f"Failed to download Sury PHP GPG key: {detail}"}
            return
        with open(_SURY_KEYRING_PATH, 'wb') as f:
            f.write(key_bytes)
        os.chmod(_SURY_KEYRING_PATH, 0o644)
        with open(_SURY_LIST_PATH, 'w') as f:
            f.write(f"deb [signed-by={_SURY_KEYRING_PATH}] https://packages.sury.org/php/ {codename} main\n")
        os.chmod(_SURY_LIST_PATH, 0o644)
        yield {'percent': 26, 'message': 'Configured Sury PHP repository.'}
        yield {'done': True, 'ok': True, 'error': None}
        return

    # Ubuntu / derivatives
    if _has_valid_repo_source('ubuntu'):
        yield {'done': True, 'ok': True, 'error': None}
        return

    os.makedirs('/etc/apt/keyrings', mode=0o755, exist_ok=True)
    attempts = []
    key_block = None

    for idx, (label, url, require_gpg) in enumerate(_ONDREJ_KEY_SOURCES):
        pct = min(24, 21 + idx)
        yield {'percent': pct, 'message': f"Fetching ondrej/php GPG key from {label}..."}
        ok, raw_bytes, fetch_err = _curl_fetch(url, timeout=12)
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
        yield {'percent': 25, 'message': 'Trying gpg HKP fallback (keyserver.ubuntu.com:80)...'}
        g_ok, g_bytes, g_err = _gpg_fetch_ondrej_key(timeout=15)
        if g_ok and g_bytes:
            v_ok, v_err = _verify_ondrej_key(g_bytes, require_gpg=False)
            if v_ok:
                key_block = g_bytes
            else:
                attempts.append(f"gpg HKP: {v_err}")
        else:
            attempts.append(g_err)

    if key_block is not None:
        # Remove any broken/partial ondrej repo files left by interrupted runs
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
    yield {'done': True, 'ok': False, 'error': f"Failed to configure ondrej/php repository: {detail}"}


def _ensure_php_repository(env):
    """
    Configure ondrej/php PPA (Ubuntu) or Sury PHP repo (Debian) directly over HTTPS
    without interactive prompts or keyserver HKP hangs.
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
        apt_err = ''
        for force_v4 in (False, True):
            extra_v4 = ['-o', 'Acquire::ForceIPv4=true'] if force_v4 else []
            update_cmd = ['apt-get', 'update', '-y'] + _APT_COMMON_OPTS + extra_v4
            prog = 28
            rc = 1
            tail = []
            for ev, data in _run_cmd_streaming(update_cmd, env=env, timeout=180):
                if ev == 'line':
                    prog = min(40, prog + 1)
                    yield emit(prog, "Refreshing APT package lists...", detail=data)
                elif ev == 'tick':
                    prog = min(40, prog + 1)
                    yield emit(prog, "Refreshing APT package lists...")
                elif ev == 'done':
                    rc, tail = data
            if rc == 0:
                apt_ok = True
                break
            apt_err = '; '.join(tail[-3:]) or f"apt-get update exited with {rc}"
            if not force_v4:
                yield emit(30, "Retrying APT refresh over IPv4...", detail=apt_err)

        if not apt_ok and not _apt_pkg_exists(f'php{version}-fpm'):
            yield emit(30, f"apt-get update failed: {apt_err}", error=True)
            return

        if not _apt_pkg_exists(f'php{version}-fpm'):
            yield emit(40, f"Package php{version}-fpm is not available for this OS release.", error=True)
            return

        packages = [f'php{version}-fpm', f'php{version}-cli', f'php{version}-common']
        if version == '7.4':
            packages.append('php7.4-json')
        for ext in requested_exts:
            pkg = f'php{version}-{ext}'
            if pkg not in packages and _apt_pkg_exists(pkg):
                packages.append(pkg)

        yield emit(45, f"Installing PHP {version} FPM, CLI, and {len(packages) - 3} extensions...")
        install_cmd = ['apt-get', 'install', '-y'] + _APT_INSTALL_OPTS + packages
        prog = 45
        rc = 1
        tail = []
        last_detail = ''
        for ev, data in _run_cmd_streaming(install_cmd, env=env, timeout=600):
            if ev == 'line':
                last_detail = data
                prog = min(84, prog + 1)
                yield emit(prog, f"Installing PHP {version} packages...", detail=data)
            elif ev == 'tick':
                prog = min(84, prog + 1)
                yield emit(prog, f"Installing PHP {version} packages...", detail=last_detail)
            elif ev == 'done':
                rc, tail = data

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
            ['dpkg-query', '-W', '-f=${Package}\n', f'php{version}*'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
        )
        pkg_re = re.compile(rf'^php{re.escape(version)}(?:-[a-z0-9.+-]+)?$')
        pkgs = [p.strip() for p in dpkg_res.stdout.splitlines() if pkg_re.fullmatch(p.strip())]

        if pkgs:
            env = _non_interactive_env()
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
        res = subprocess.run(
            ['apt-get', 'install', '-y'] + _APT_INSTALL_OPTS + [pkg],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180
        )
        if res.returncode != 0:
            return False, f"Failed to install {pkg}: {res.stderr.strip() or res.stdout.strip()}"

        if _get_service_status(f'php{version}-fpm') == 'active':
            subprocess.run(['systemctl', 'reload', f'php{version}-fpm'], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        return True, f"Extension {pkg} installed successfully."
    except subprocess.TimeoutExpired:
        return False, f"Timed out installing {pkg}."
    except Exception as e:
        return False, f"Error installing {pkg}: {str(e)}"


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

