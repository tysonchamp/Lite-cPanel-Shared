"""BIND9 DNS server management, root nameserver settings, and per-tenant zone/record CRUD."""

from datetime import datetime
import ipaddress
import json
import logging
import os
import pwd
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.request

from hosting_mgr import _get_conn, get_user_data
from security_mgr import validate_input

BIND_DIR = "/etc/bind"
LITE_DNS_DIR = "/etc/bind/lite-cpanel"
ZONES_DIR = "/etc/bind/lite-cpanel/zones"
NAMED_CONF_LOCAL = "/etc/bind/named.conf.local"
NAMED_CONF_OPTIONS = "/etc/bind/named.conf.options"
NAMED_CONF_LITE = "/etc/bind/lite-cpanel/named.conf.lite-cpanel"
CSF_CONF_PATH = "/etc/csf/csf.conf"

SUPPORTED_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA")

_DEFAULT_REFRESH = 3600
_DEFAULT_RETRY = 1800
_DEFAULT_EXPIRE = 1209600
_DEFAULT_MIN_TTL = 3600
_DEFAULT_RECORD_TTL = 14400
_MIN_TTL = 60
_MAX_TTL = 604800

_LABEL_WITH_UNDERSCORE_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")
_CAA_QUOTED_RE = re.compile(
    r'^(\d{1,3})\s+(issue|issuewild|iodef|contactemail|contactphone)\s+"([^"\r\n\\]*)"$'
)
_CAA_BARE_RE = re.compile(
    r'^(\d{1,3})\s+(issue|issuewild|iodef|contactemail|contactphone)\s+([^\s"\r\n\\]+)$'
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_APT_INSTALL_OPTS = [
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]

_PUBLIC_IP_CACHE = {"ip": None}


def _dns_conn():
    """Open hosting.db with foreign key enforcement enabled."""
    conn = _get_conn()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    return conn


def _non_interactive_env():
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["APT_LISTCHANGES_FRONTEND"] = "none"
    env["NEEDRESTART_MODE"] = "a"
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
    return env


def normalize_domain(domain):
    """Normalize a domain name (lowercase, stripped of whitespace and trailing dot)."""
    if not isinstance(domain, str):
        return ""
    return domain.strip().rstrip(".").lower()


def validate_zone_domain(domain):
    """Validate that `domain` is a safe domain name for a DNS zone."""
    norm = normalize_domain(domain)
    if not norm or "/" in norm or "\\" in norm or ".." in norm or len(norm) > 253:
        return False, "", "Invalid domain name."
    ok, err = validate_input(norm, "domain")
    if not ok or "." not in norm:
        return False, "", err or "Domain must be a valid hostname (e.g., example.com)."
    return True, norm, ""


def _validate_owner_name(name):
    """Validate a relative DNS owner name (@, *, *.sub, _dmarc, subdomain.label)."""
    if not isinstance(name, str):
        return False, "", "Record host name is required."
    raw = name.strip()
    if not raw:
        return False, "", "Record host name is required."
    if any(ch.isspace() or ch in ';\\"\'()$@\x00' for ch in raw if not (raw == "@" and ch == "@")):
        if raw != "@":
            return False, "", "Record host name contains invalid characters."
    if raw == "@":
        return True, "@", ""
    if len(raw) > 253:
        return False, "", "Record host name is too long."
    if raw.endswith("."):
        return False, "", "Enter a relative host name without trailing dot (e.g., '@', 'www', '_dmarc')."

    labels = raw.split(".")
    for idx, label in enumerate(labels):
        if label == "*":
            if idx != 0:
                return False, "", "Wildcard '*' is only allowed as the leftmost label."
            continue
        if not _LABEL_WITH_UNDERSCORE_RE.fullmatch(label):
            return False, "", f"Invalid DNS label '{label}'."
    return True, raw.lower(), ""


def _validate_hostname_target(target, allow_at=False, allow_dot=False):
    """Validate and normalize a DNS hostname/FQDN target value."""
    if not isinstance(target, str):
        return False, "", "Target hostname is required."
    raw = target.strip()
    if not raw or any(ch.isspace() or ch in ';\\"\'()\x00' for ch in raw):
        return False, "", "Target hostname is invalid."
    if allow_at and raw == "@":
        return True, "@", ""
    if allow_dot and raw == ".":
        return True, ".", ""

    had_trailing_dot = raw.endswith(".")
    bare = raw.rstrip(".")
    if not bare or ".." in bare or len(bare) > 253:
        return False, "", "Invalid target hostname."

    # Reject raw IPv4 addresses where hostname is expected
    try:
        ipaddress.IPv4Address(bare)
        return False, "", "Target must be a hostname, not an IPv4 address."
    except ValueError:
        pass

    labels = bare.split(".")
    for label in labels:
        if not _LABEL_WITH_UNDERSCORE_RE.fullmatch(label):
            return False, "", f"Invalid hostname label '{label}'."

    # Single relative label (e.g. 'www' or 'mail') stays relative unless user supplied trailing dot;
    # multi-label hostnames (e.g. 'ghs.google.com') are normalized to FQDN with trailing dot.
    if had_trailing_dot or "." in bare:
        return True, f"{bare.lower()}.", ""
    return True, bare.lower(), ""


def _format_soa_email(email_or_rname, fallback_domain="localhost"):
    """Format an email or SOA RNAME into valid BIND SOA RNAME ending in a dot."""
    raw = (email_or_rname or "").strip()
    if not raw:
        return f"hostmaster.{normalize_domain(fallback_domain) or 'localhost'}."
    if "@" in raw:
        local, host = raw.split("@", 1)
        local_escaped = local.strip().replace(".", "\\.")
        host_clean = normalize_domain(host) or normalize_domain(fallback_domain) or "localhost"
        return f"{local_escaped}.{host_clean}."
    clean = raw.rstrip(".")
    return f"{clean}."


def _format_txt_rdata(text):
    """Format TXT value as safe RFC 1035 quoted 255-byte chunks for BIND9."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    chunks = []
    current = []
    cur_len = 0
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        # Keep escape sequences together inside the same quoted chunk
        if ch == "\\" and i + 1 < len(escaped):
            token = escaped[i : i + 2]
            i += 2
        else:
            token = ch
            i += 1
        tok_bytes = len(token.encode("utf-8"))
        if cur_len + tok_bytes > 250 and current:
            chunks.append(f'"{"".join(current)}"')
            current = [token]
            cur_len = tok_bytes
        else:
            current.append(token)
            cur_len += tok_bytes
    if current or not chunks:
        chunks.append(f'"{"".join(current)}"')
    return " ".join(chunks)


def validate_dns_record(rtype, name, value, ttl=14400, priority=None, weight=None, port=None):
    """
    Validate and normalize a DNS record across supported record types.
    Returns (ok, normalized_dict, error_message).
    """
    if not isinstance(rtype, str):
        return False, None, "Record type is required."
    rtype_u = rtype.strip().upper()
    if rtype_u not in SUPPORTED_RECORD_TYPES:
        return False, None, f"Unsupported record type '{rtype}'. Supported: {', '.join(SUPPORTED_RECORD_TYPES)}."

    ok_name, norm_name, err_name = _validate_owner_name(name)
    if not ok_name:
        return False, None, err_name

    try:
        ttl_val = _DEFAULT_RECORD_TTL if ttl in (None, "") else int(ttl)
    except (TypeError, ValueError):
        return False, None, "TTL must be an integer."
    if ttl_val < _MIN_TTL or ttl_val > _MAX_TTL:
        return False, None, f"TTL must be between {_MIN_TTL} and {_MAX_TTL} seconds."

    raw_val = value.strip() if isinstance(value, str) else ""
    if not raw_val or any(ch in "\r\n\x00" for ch in raw_val):
        return False, None, "Record value is required and cannot contain line breaks."

    prio_val = None
    weight_val = None
    port_val = None
    norm_val = raw_val

    if rtype_u == "A":
        try:
            norm_val = str(ipaddress.IPv4Address(raw_val))
        except ValueError:
            return False, None, "A record value must be a valid IPv4 address (e.g., 203.0.113.10)."

    elif rtype_u == "AAAA":
        try:
            norm_val = str(ipaddress.IPv6Address(raw_val))
        except ValueError:
            return False, None, "AAAA record value must be a valid IPv6 address (e.g., 2001:db8::1)."

    elif rtype_u == "CNAME":
        if norm_name == "@":
            return False, None, "Zone apex (@) cannot be a CNAME because SOA and NS records exist at apex."
        ok_t, norm_val, err_t = _validate_hostname_target(raw_val, allow_at=True)
        if not ok_t:
            return False, None, f"Invalid CNAME target: {err_t}"

    elif rtype_u == "NS":
        ok_t, norm_val, err_t = _validate_hostname_target(raw_val, allow_at=False)
        if not ok_t or "." not in norm_val.rstrip("."):
            return False, None, "NS record value must be a valid FQDN nameserver (e.g., ns1.example.com)."

    elif rtype_u == "MX":
        try:
            prio_val = 10 if priority in (None, "") else int(priority)
        except (TypeError, ValueError):
            return False, None, "MX priority must be an integer (0–65535)."
        if prio_val < 0 or prio_val > 65535:
            return False, None, "MX priority must be between 0 and 65535."
        ok_t, norm_val, err_t = _validate_hostname_target(raw_val, allow_at=True)
        if not ok_t:
            return False, None, f"Invalid MX mail server target: {err_t}"

    elif rtype_u == "TXT":
        # Strip wrapping quotes only if user pasted verbatim quoted single string
        txt = raw_val
        if len(txt) >= 2 and txt.startswith('"') and txt.endswith('"') and '"' not in txt[1:-1]:
            txt = txt[1:-1]
        if not txt or len(txt) > 4096:
            return False, None, "TXT value must be between 1 and 4096 characters."
        norm_val = txt

    elif rtype_u == "SRV":
        for label_name, raw_num, min_v, default_v in (
            ("Priority", priority, 0, 10),
            ("Weight", weight, 0, 5),
            ("Port", port, 0, None),
        ):
            if raw_num in (None, "") and default_v is None:
                return False, None, "SRV record requires Priority, Weight, and Port."
            try:
                num = default_v if raw_num in (None, "") else int(raw_num)
            except (TypeError, ValueError):
                return False, None, f"SRV {label_name} must be an integer."
            if num < min_v or num > 65535:
                return False, None, f"SRV {label_name} must be between {min_v} and 65535."
            if label_name == "Priority":
                prio_val = num
            elif label_name == "Weight":
                weight_val = num
            else:
                port_val = num
        ok_t, norm_val, err_t = _validate_hostname_target(raw_val, allow_at=False, allow_dot=True)
        if not ok_t:
            return False, None, f"Invalid SRV target: {err_t}"

    elif rtype_u == "CAA":
        m = _CAA_QUOTED_RE.fullmatch(raw_val) or _CAA_BARE_RE.fullmatch(raw_val)
        if not m:
            return False, None, 'CAA value must match: <flags> <issue|issuewild|iodef> "<value>" (e.g., 0 issue "letsencrypt.org").'
        flags = int(m.group(1))
        if flags < 0 or flags > 255:
            return False, None, "CAA flags must be between 0 and 255."
        tag = m.group(2)
        tag_val = m.group(3)
        norm_val = f'{flags} {tag} "{tag_val}"'

    return True, {
        "name": norm_name,
        "type": rtype_u,
        "value": norm_val,
        "ttl": ttl_val,
        "priority": prio_val,
        "weight": weight_val,
        "port": port_val,
    }, ""


def detect_server_public_ip(force_refresh=False):
    """Detect server public IPv4 (with local interface fallback)."""
    if _PUBLIC_IP_CACHE["ip"] and not force_refresh:
        return _PUBLIC_IP_CACHE["ip"]

    for url in ("https://api.ipify.org", "https://ident.me"):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                candidate = resp.read().decode("utf-8", errors="ignore").strip()
                ip_str = str(ipaddress.IPv4Address(candidate))
                _PUBLIC_IP_CACHE["ip"] = ip_str
                return ip_str
        except Exception:
            pass

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        candidate = sock.getsockname()[0]
        sock.close()
        ip_str = str(ipaddress.IPv4Address(candidate))
        _PUBLIC_IP_CACHE["ip"] = ip_str
        return ip_str
    except Exception:
        return "127.0.0.1"


def get_dns_settings():
    """Fetch global nameserver and DNS default configuration from hosting.db."""
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM dns_settings")
    raw = {row["key"]: row["value"] for row in cur.fetchall()}
    conn.close()

    hostname = (socket.gethostname() or "localhost").strip().lower()
    host_domain = hostname
    if hostname.count(".") >= 2:
        host_domain = hostname.split(".", 1)[1]

    default_ip = raw.get("default_ip", "").strip() or detect_server_public_ip()
    ns1 = raw.get("ns1", "").strip()
    ns2 = raw.get("ns2", "").strip()
    ns1_ip = raw.get("ns1_ip", "").strip() or default_ip
    ns2_ip = raw.get("ns2_ip", "").strip() or default_ip

    try:
        default_ttl = int(raw.get("default_ttl", _DEFAULT_RECORD_TTL))
    except (TypeError, ValueError):
        default_ttl = _DEFAULT_RECORD_TTL

    return {
        "ns1": ns1,
        "ns2": ns2,
        "ns3": raw.get("ns3", "").strip(),
        "ns4": raw.get("ns4", "").strip(),
        "ns1_ip": ns1_ip if ns1 else raw.get("ns1_ip", "").strip(),
        "ns2_ip": ns2_ip if ns2 else raw.get("ns2_ip", "").strip(),
        "ns3_ip": raw.get("ns3_ip", "").strip(),
        "ns4_ip": raw.get("ns4_ip", "").strip(),
        "default_ip": default_ip,
        "default_ipv6": raw.get("default_ipv6", "").strip(),
        "hostmaster_email": raw.get("hostmaster_email", "").strip() or f"hostmaster@{host_domain}",
        "default_ttl": default_ttl,
        "auto_create_zone": raw.get("auto_create_zone", "1") == "1",
        "nameservers_configured": bool(ns1 and ns2),
    }


def _active_nameservers_for_domain(domain, settings=None):
    """Return ordered list of nameserver FQDNs for `domain`."""
    settings = settings or get_dns_settings()
    configured = []
    for key in ("ns1", "ns2", "ns3", "ns4"):
        ns = normalize_domain(settings.get(key, ""))
        if ns:
            fqdn = f"{ns}."
            if fqdn not in configured:
                configured.append(fqdn)
    if configured:
        return configured
    d = normalize_domain(domain)
    return [f"ns1.{d}.", f"ns2.{d}."]


def _glue_pairs_for_domain(domain, settings=None):
    """Return list of (sub_label, ip_str) glue A records needed inside `domain`."""
    settings = settings or get_dns_settings()
    d = normalize_domain(domain)
    suffix = f".{d}"
    default_ip = settings.get("default_ip") or detect_server_public_ip()
    pairs = []
    for key, ip_key in (("ns1", "ns1_ip"), ("ns2", "ns2_ip"), ("ns3", "ns3_ip"), ("ns4", "ns4_ip")):
        ns = normalize_domain(settings.get(key, ""))
        if ns and ns.endswith(suffix):
            sub = ns[: -len(suffix)]
            ip_val = (settings.get(ip_key) or "").strip() or default_ip
            if sub and ip_val:
                pairs.append((sub, ip_val))
    if not settings.get("ns1") and not settings.get("ns2"):
        pairs.extend([("ns1", default_ip), ("ns2", default_ip)])
    return pairs


def save_dns_settings(payload, apply_to_existing=False, sync_glue=True):
    """Validate and persist root Nameserver & global DNS settings."""
    if not isinstance(payload, dict):
        return False, "Invalid settings payload."

    ns1 = normalize_domain(payload.get("ns1", ""))
    ns2 = normalize_domain(payload.get("ns2", ""))
    ns3 = normalize_domain(payload.get("ns3", ""))
    ns4 = normalize_domain(payload.get("ns4", ""))

    if not ns1 or not ns2:
        return False, "Primary (NS1) and Secondary (NS2) nameservers are required."
    if ns1 == ns2:
        return False, "Primary (NS1) and Secondary (NS2) nameservers must be distinct hostnames."

    ns_values = []
    for label, val in (("NS1", ns1), ("NS2", ns2), ("NS3", ns3), ("NS4", ns4)):
        if not val:
            continue
        ok, norm_ns, err = validate_zone_domain(val)
        if not ok:
            return False, f"Invalid {label} hostname '{val}': {err}"
        if norm_ns in ns_values:
            return False, f"Duplicate nameserver hostname '{norm_ns}'."
        ns_values.append(norm_ns)

    default_ip_raw = (payload.get("default_ip") or "").strip() or detect_server_public_ip()
    try:
        default_ip = str(ipaddress.IPv4Address(default_ip_raw))
    except ValueError:
        return False, "Default Server IPv4 must be a valid IPv4 address."

    default_ipv6_raw = (payload.get("default_ipv6") or "").strip()
    default_ipv6 = ""
    if default_ipv6_raw:
        try:
            default_ipv6 = str(ipaddress.IPv6Address(default_ipv6_raw))
        except ValueError:
            return False, "Default Server IPv6 must be a valid IPv6 address."

    resolved_ips = {}
    for label, ns_val, ip_key in (
        ("NS1 IP", ns1, "ns1_ip"),
        ("NS2 IP", ns2, "ns2_ip"),
        ("NS3 IP", ns3, "ns3_ip"),
        ("NS4 IP", ns4, "ns4_ip"),
    ):
        raw_ip = (payload.get(ip_key) or "").strip()
        if raw_ip:
            try:
                resolved_ips[ip_key] = str(ipaddress.IPv4Address(raw_ip))
            except ValueError:
                return False, f"{label} must be a valid IPv4 address."
        else:
            resolved_ips[ip_key] = default_ip if ns_val else ""

    hostmaster_email = (payload.get("hostmaster_email") or "").strip()
    if not hostmaster_email:
        base_dom = ns1.split(".", 1)[1] if "." in ns1 else ns1
        hostmaster_email = f"hostmaster@{base_dom}"
    elif "@" in hostmaster_email:
        if not _EMAIL_RE.fullmatch(hostmaster_email):
            return False, "Hostmaster email address is invalid."
    else:
        ok_hm, hm_dom, _ = validate_zone_domain(hostmaster_email)
        if not ok_hm:
            return False, "Hostmaster contact must be a valid email address."
        hostmaster_email = hm_dom

    try:
        default_ttl = int(payload.get("default_ttl") or _DEFAULT_RECORD_TTL)
    except (TypeError, ValueError):
        return False, "Default TTL must be an integer."
    if default_ttl < _MIN_TTL or default_ttl > _MAX_TTL:
        return False, f"Default TTL must be between {_MIN_TTL} and {_MAX_TTL} seconds."

    auto_create_zone = "1" if payload.get("auto_create_zone") else "0"

    kv_pairs = {
        "ns1": ns1,
        "ns2": ns2,
        "ns3": ns3,
        "ns4": ns4,
        "ns1_ip": resolved_ips["ns1_ip"],
        "ns2_ip": resolved_ips["ns2_ip"],
        "ns3_ip": resolved_ips["ns3_ip"],
        "ns4_ip": resolved_ips["ns4_ip"],
        "default_ip": default_ip,
        "default_ipv6": default_ipv6,
        "hostmaster_email": hostmaster_email,
        "default_ttl": str(default_ttl),
        "auto_create_zone": auto_create_zone,
    }

    conn = _dns_conn()
    cur = conn.cursor()
    for k, v in kv_pairs.items():
        cur.execute(
            "INSERT INTO dns_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, v),
        )
    conn.commit()
    conn.close()

    extra_msgs = []
    if sync_glue:
        synced = sync_nameserver_glue_records()
        if synced:
            extra_msgs.append(f"synced glue A records in: {', '.join(synced)}")

    if apply_to_existing:
        ok_apply, apply_msg = apply_nameservers_to_all_zones()
        if not ok_apply:
            return False, f"Settings saved, but zone sync failed: {apply_msg}"
        extra_msgs.append(apply_msg)

    msg = f"Nameserver settings saved ({ns1}, {ns2})."
    if extra_msgs:
        msg += " " + "; ".join(extra_msgs)
    return True, msg


def _next_serial(prev_serial=0):
    today_serial = int(datetime.now().strftime("%Y%m%d") + "01")
    return max(int(prev_serial or 0) + 1, today_serial)


def _fetch_zone_and_records(conn, zone_id_or_domain):
    cur = conn.cursor()
    if isinstance(zone_id_or_domain, int) or (
        isinstance(zone_id_or_domain, str) and zone_id_or_domain.isdigit()
    ):
        cur.execute("SELECT * FROM dns_zones WHERE id = ?", (int(zone_id_or_domain),))
    else:
        cur.execute("SELECT * FROM dns_zones WHERE domain = ?", (normalize_domain(zone_id_or_domain),))
    zone_row = cur.fetchone()
    if not zone_row:
        return None, []
    cur.execute(
        "SELECT * FROM dns_records WHERE zone_id = ? "
        "ORDER BY CASE type "
        "WHEN 'NS' THEN 1 WHEN 'A' THEN 2 WHEN 'AAAA' THEN 3 WHEN 'CNAME' THEN 4 "
        "WHEN 'MX' THEN 5 WHEN 'TXT' THEN 6 WHEN 'SRV' THEN 7 ELSE 8 END, name, id",
        (zone_row["id"],),
    )
    recs = [dict(r) for r in cur.fetchall()]
    return dict(zone_row), recs


def _check_zone_ownership(zone, role="admin", username=None):
    if role == "admin":
        return True
    if role != "user" or not username:
        return False
    if zone.get("owner_username") == username:
        return True
    return zone.get("domain") in get_user_owned_domains(username)


def _safe_zone_file_path(domain):
    ok, norm, err = validate_zone_domain(domain)
    if not ok:
        raise ValueError(err)
    zones_abs = os.path.abspath(ZONES_DIR)
    path = os.path.abspath(os.path.join(zones_abs, f"{norm}.db"))
    if os.path.dirname(path) != zones_abs or os.path.basename(path) != f"{norm}.db":
        raise ValueError(f"Unsafe zone path resolved: {path}")
    return path


def _bind_uid_gid():
    try:
        pw = pwd.getpwnam("bind")
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        return None, None


def _chown_bind_safe(path, mode=0o644):
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    uid, gid = _bind_uid_gid()
    if uid is not None:
        try:
            os.chown(path, 0, gid)
        except OSError:
            pass


def ensure_bind9_base_config():
    """Ensure /etc/bind/lite-cpanel directory, zone include, and safe authoritative options exist."""
    os.makedirs(LITE_DNS_DIR, mode=0o755, exist_ok=True)
    os.makedirs(ZONES_DIR, mode=0o755, exist_ok=True)
    _chown_bind_safe(LITE_DNS_DIR, 0o755)
    _chown_bind_safe(ZONES_DIR, 0o755)

    if not os.path.exists(NAMED_CONF_LITE):
        with open(NAMED_CONF_LITE, "w") as f:
            f.write("// Managed by Lite-cPanel DNS Manager. Do not edit manually.\n")
        _chown_bind_safe(NAMED_CONF_LITE, 0o644)

    include_line = f'include "{os.path.abspath(NAMED_CONF_LITE)}";'
    if os.path.exists(os.path.dirname(NAMED_CONF_LOCAL)):
        existing_local = ""
        if os.path.exists(NAMED_CONF_LOCAL):
            with open(NAMED_CONF_LOCAL, "r") as f:
                existing_local = f.read()
        if os.path.abspath(NAMED_CONF_LITE) not in existing_local:
            prefix = "" if (not existing_local or existing_local.endswith("\n")) else "\n"
            with open(NAMED_CONF_LOCAL, "a") as f:
                f.write(f"{prefix}{include_line}\n")

    _configure_bind9_options_authoritative()


def _configure_bind9_options_authoritative():
    """Set authoritative-safe defaults in named.conf.options once without clobbering existing edits."""
    if not os.path.exists(NAMED_CONF_OPTIONS):
        return
    try:
        with open(NAMED_CONF_OPTIONS, "r") as f:
            content = f.read()
    except OSError:
        return

    if "Lite-cPanel" in content:
        return

    opt_match = re.search(r"options\s*\{(.*)\}\s*;", content, re.DOTALL)
    if not opt_match:
        return

    body = opt_match.group(1)
    additions = []
    if not re.search(r"\brecursion\b", body):
        additions.append("\trecursion no;")
    if not re.search(r"\ballow-query\b", body):
        additions.append("\tallow-query { any; };")
    if not re.search(r"\ballow-transfer\b", body):
        additions.append("\tallow-transfer { none; };")
    if not re.search(r"\blisten-on\s*\{", body):
        additions.append("\tlisten-on { any; };")

    if not additions:
        return

    insert_block = "\n\t// Authoritative DNS defaults (Lite-cPanel)\n" + "\n".join(additions) + "\n"
    new_body = body + insert_block
    new_content = content[: opt_match.start(1)] + new_body + content[opt_match.end(1) :]

    backup_path = f"{NAMED_CONF_OPTIONS}.litecpanel.bak"
    try:
        shutil.copy2(NAMED_CONF_OPTIONS, backup_path)
        with open(NAMED_CONF_OPTIONS, "w") as f:
            f.write(new_content)
        check_cmd = shutil.which("named-checkconf")
        if check_cmd:
            res = subprocess.run([check_cmd], capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                shutil.copy2(backup_path, NAMED_CONF_OPTIONS)
    except Exception:
        if os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, NAMED_CONF_OPTIONS)
            except OSError:
                pass


def render_zone_file(zone, records, settings=None):
    """Render a complete RFC-compliant BIND9 zone file string from DB zone + records."""
    domain = normalize_domain(zone["domain"])
    settings = settings or get_dns_settings()
    ttl_default = int(settings.get("default_ttl") or _DEFAULT_RECORD_TTL)
    primary_ns = (zone.get("primary_ns") or _active_nameservers_for_domain(domain, settings)[0]).rstrip(".") + "."
    rname = _format_soa_email(zone.get("hostmaster_email") or settings.get("hostmaster_email"), domain)

    lines = [
        f"; Zone file for {domain} — Managed by Lite-cPanel",
        f"$ORIGIN {domain}.",
        f"$TTL {ttl_default}",
        f"@\t{ttl_default}\tIN SOA\t{primary_ns} {rname} (",
        f"\t\t{int(zone['serial'])}\t; Serial",
        f"\t\t{int(zone.get('refresh') or _DEFAULT_REFRESH)}\t\t; Refresh",
        f"\t\t{int(zone.get('retry') or _DEFAULT_RETRY)}\t\t; Retry",
        f"\t\t{int(zone.get('expire') or _DEFAULT_EXPIRE)}\t\t; Expire",
        f"\t\t{int(zone.get('minimum_ttl') or _DEFAULT_MIN_TTL)} )\t\t; Minimum TTL",
        "",
    ]

    for rec in records:
        owner = rec["name"]
        ttl = int(rec.get("ttl") or ttl_default)
        rtype = rec["type"].upper()
        val = rec["value"]

        if rtype == "MX":
            prio = int(rec.get("priority") if rec.get("priority") is not None else 10)
            rdata = f"{prio} {val}"
        elif rtype == "SRV":
            prio = int(rec.get("priority") or 0)
            weight = int(rec.get("weight") or 0)
            port = int(rec.get("port") or 0)
            rdata = f"{prio} {weight} {port} {val}"
        elif rtype == "TXT":
            rdata = _format_txt_rdata(val)
        else:
            rdata = val

        lines.append(f"{owner:<20} {ttl:<6} IN {rtype:<6} {rdata}")

    lines.append("")
    return "\n".join(lines)


def validate_zone_file(domain, zone_path):
    """Validate a BIND9 zone file via `named-checkzone` if installed."""
    checker = shutil.which("named-checkzone")
    if not checker:
        return True, ""
    try:
        res = subprocess.run(
            [checker, normalize_domain(domain), zone_path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15
        )
        if res.returncode != 0:
            err = (res.stdout or res.stderr or "").strip()
            return False, f"BIND zone syntax error: {err}"
        return True, (res.stdout or "").strip()
    except Exception as exc:
        return False, f"Zone validation failed: {exc}"


def validate_bind_config():
    """Validate global BIND configuration via `named-checkconf` if installed."""
    checker = shutil.which("named-checkconf")
    if not checker:
        return True, ""
    try:
        res = subprocess.run(
            [checker],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            return False, f"BIND config error: {err}"
        return True, ""
    except Exception as exc:
        return False, f"Config validation failed: {exc}"


def get_bind9_service_name():
    """Return active/installed systemd unit name ('named' or 'bind9') on Ubuntu/Debian."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "named"
    for svc in ("named", "bind9"):
        try:
            res = subprocess.run(
                [systemctl, "status", svc],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
            )
            if "Loaded: loaded" in res.stdout:
                return svc
        except Exception:
            pass
    return "named"


def is_bind9_installed():
    """Return True if BIND9 daemon binaries are present on the system."""
    return bool(shutil.which("named") or os.path.exists("/usr/sbin/named") or shutil.which("named-checkzone"))


def get_bind9_status():
    """Return BIND9 service status ('not_installed', 'active', 'inactive', 'failed', 'unknown')."""
    if not is_bind9_installed():
        return "not_installed"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "unknown"
    svc = get_bind9_service_name()
    try:
        res = subprocess.run(
            [systemctl, "is-active", svc],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5
        )
        status = res.stdout.strip()
        return status if status in ("active", "inactive", "failed") else "inactive"
    except Exception:
        return "unknown"


def reload_bind9():
    """Validate BIND config and reload BIND9 if installed and active."""
    if not is_bind9_installed():
        return True, ""
    ok_conf, err_conf = validate_bind_config()
    if not ok_conf:
        return False, err_conf
    if get_bind9_status() != "active":
        return True, ""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return True, ""
    svc = get_bind9_service_name()
    try:
        res = subprocess.run(
            [systemctl, "reload", svc],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
        )
        if res.returncode != 0:
            # Fallback to rndc reload or restart
            res2 = subprocess.run(
                [systemctl, "restart", svc],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
            )
            if res2.returncode != 0:
                err = (res2.stderr or res.stderr or "").strip()
                return False, f"Failed to reload BIND9: {err}"
        return True, ""
    except Exception as exc:
        return False, f"Failed to reload BIND9: {exc}"


def manage_bind9_service(action):
    """Execute start/stop/restart/reload on the BIND9 service (admin-only)."""
    if action not in ("start", "stop", "restart", "reload"):
        return False, "Invalid service action."
    if not is_bind9_installed():
        return False, "BIND9 is not installed."
    if action in ("start", "restart", "reload"):
        ensure_bind9_base_config()
        rebuild_named_conf_lite()
        ok_conf, err_conf = validate_bind_config()
        if not ok_conf:
            return False, err_conf
    svc = get_bind9_service_name()
    try:
        res = subprocess.run(
            ["systemctl", action, svc],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=25
        )
        if res.returncode != 0:
            return False, f"Failed to {action} BIND9: {(res.stderr or res.stdout).strip()}"
        return True, f"BIND9 DNS server ({svc}) {action}ed successfully."
    except Exception as exc:
        return False, f"BIND9 service error: {exc}"


def _write_zone_and_sync_bind(zone, records, settings=None, reload_service=True):
    """
    Validate rendered zone via named-checkzone, write atomically if BIND dir exists/configured,
    rebuild named.conf.lite-cpanel, and optionally reload BIND9.
    """
    domain = normalize_domain(zone["domain"])
    rendered = render_zone_file(zone, records, settings=settings)

    with tempfile.NamedTemporaryFile("w", prefix=f"zone_{domain}_", suffix=".db", delete=False) as tf:
        tf.write(rendered)
        tmp_path = tf.name

    try:
        ok_chk, err_chk = validate_zone_file(domain, tmp_path)
        if not ok_chk:
            return False, err_chk

        # Only touch system /etc/bind directories when configured or running BIND9
        should_write_fs = os.path.isdir(BIND_DIR) or os.path.isdir(LITE_DNS_DIR) or os.path.isdir(ZONES_DIR)
        if should_write_fs:
            ensure_bind9_base_config()
            zone_path = _safe_zone_file_path(domain)
            shutil.move(tmp_path, zone_path)
            tmp_path = None
            _chown_bind_safe(zone_path, 0o644)
            rebuild_named_conf_lite()
            if reload_service:
                ok_rel, err_rel = reload_bind9()
                if not ok_rel:
                    return False, err_rel
        return True, ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def rebuild_named_conf_lite():
    """Regenerate named.conf.lite-cpanel from all enabled dns_zones whose zone file exists."""
    should_write = os.path.isdir(BIND_DIR) or os.path.isdir(LITE_DNS_DIR) or os.path.isdir(ZONES_DIR)
    if not should_write:
        return

    ensure_bind9_base_config()
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain, enabled FROM dns_zones ORDER BY domain")
    rows = cur.fetchall()
    conn.close()

    blocks = ["// Managed by Lite-cPanel DNS Manager. Do not edit manually."]
    for row in rows:
        if not row["enabled"]:
            continue
        domain = normalize_domain(row["domain"])
        try:
            zpath = _safe_zone_file_path(domain)
        except ValueError:
            continue
        if os.path.exists(zpath):
            blocks.append(
                f'zone "{domain}" {{\n'
                f'    type master;\n'
                f'    file "{zpath}";\n'
                f'    allow-transfer {{ none; }};\n'
                f'}};'
            )

    content = "\n\n".join(blocks) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=LITE_DNS_DIR, prefix=".named_conf_", delete=False) as tf:
        tf.write(content)
        tmp_conf = tf.name
    shutil.move(tmp_conf, NAMED_CONF_LITE)
    _chown_bind_safe(NAMED_CONF_LITE, 0o644)


def _build_default_records(domain, settings=None):
    """Build initial DNS records list for a newly created zone."""
    settings = settings or get_dns_settings()
    d = normalize_domain(domain)
    ttl = int(settings.get("default_ttl") or _DEFAULT_RECORD_TTL)
    server_ip = settings.get("default_ip") or detect_server_public_ip()
    server_ipv6 = (settings.get("default_ipv6") or "").strip()

    records = []
    for ns_fqdn in _active_nameservers_for_domain(d, settings):
        records.append({
            "name": "@", "type": "NS", "value": ns_fqdn, "ttl": ttl,
            "priority": None, "weight": None, "port": None,
        })

    # Add glue A records if nameservers sit inside this domain (prevents named-checkzone errors)
    added_sub_a = set()
    for sub, glue_ip in _glue_pairs_for_domain(d, settings):
        if (sub, glue_ip) in added_sub_a:
            continue
        added_sub_a.add((sub, glue_ip))
        records.append({
            "name": sub, "type": "A", "value": glue_ip, "ttl": ttl,
            "priority": None, "weight": None, "port": None,
        })

    records.extend([
        {"name": "@", "type": "A", "value": server_ip, "ttl": ttl, "priority": None, "weight": None, "port": None},
        {"name": "www", "type": "CNAME", "value": "@", "ttl": ttl, "priority": None, "weight": None, "port": None},
        {"name": "mail", "type": "A", "value": server_ip, "ttl": ttl, "priority": None, "weight": None, "port": None},
        {"name": "@", "type": "MX", "value": f"mail.{d}.", "ttl": ttl, "priority": 10, "weight": None, "port": None},
        {"name": "@", "type": "TXT", "value": "v=spf1 a mx ~all", "ttl": ttl, "priority": None, "weight": None, "port": None},
    ])

    if server_ipv6:
        records.append({
            "name": "@", "type": "AAAA", "value": server_ipv6, "ttl": ttl,
            "priority": None, "weight": None, "port": None,
        })

    return records


def get_user_owned_domains(username):
    """Collect every domain owned by a shared user across plans, vhosts, Next.js, Docker, and DNS."""
    if not username:
        return set()
    owned = set()
    udata = get_user_data(username)
    if udata:
        if udata.get("main_domain"):
            owned.add(normalize_domain(udata["main_domain"]))
        for rkey in ("domains", "nextjs_apps", "docker_apps"):
            for dom in udata.get(rkey, []):
                owned.add(normalize_domain(dom))

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM dns_zones WHERE owner_username = ?", (username,))
    for row in cur.fetchall():
        owned.add(normalize_domain(row["domain"]))
    conn.close()
    return {d for d in owned if d}


def list_dns_zones(role="admin", username=None):
    """Return DNS zones visible to the current role/user along with record counts."""
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT z.*, COUNT(r.id) AS record_count "
        "FROM dns_zones z LEFT JOIN dns_records r ON r.zone_id = z.id "
        "GROUP BY z.id ORDER BY z.domain"
    )
    zones = [dict(r) for r in cur.fetchall()]
    conn.close()

    if role == "admin":
        return zones

    owned = get_user_owned_domains(username) if role == "user" else set()
    return [z for z in zones if z["owner_username"] == username or z["domain"] in owned]


def get_uninitialized_owned_domains(role="admin", username=None):
    """Return domains configured in Domains/Next.js/Docker/hosting that lack a DNS zone yet."""
    candidates = set()
    try:
        from domains_mgr import get_virtual_hosts
        for v in get_virtual_hosts(role, username):
            candidates.add(normalize_domain(v["domain"]))
    except Exception:
        pass
    try:
        from nextjs_mgr import get_nextjs_apps
        for app in get_nextjs_apps(role, username):
            candidates.add(normalize_domain(app["domain"]))
    except Exception:
        pass
    try:
        from docker_mgr import get_docker_apps
        for app in get_docker_apps(role, username):
            candidates.add(normalize_domain(app["domain"]))
    except Exception:
        pass

    if role == "user" and username:
        candidates.update(get_user_owned_domains(username))

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM dns_zones")
    existing = {normalize_domain(r["domain"]) for r in cur.fetchall()}
    conn.close()

    return sorted(d for d in candidates if d and d not in existing)


def get_zone_with_records(domain, role="admin", username=None):
    """Fetch a single zone and its records after verifying RBAC ownership."""
    ok, norm, err = validate_zone_domain(domain)
    if not ok:
        return None, [], err
    conn = _dns_conn()
    zone, records = _fetch_zone_and_records(conn, norm)
    conn.close()
    if not zone:
        return None, [], f"DNS zone for '{norm}' does not exist."
    if not _check_zone_ownership(zone, role, username):
        return None, [], "Access denied: You do not own this DNS zone."
    return zone, records, ""


def create_dns_zone(domain, owner="root", role="admin", username=None, ip_override=None, reload_service=True):
    """Create a new DNS zone with default records and sync BIND9."""
    ok, norm, err = validate_zone_domain(domain)
    if not ok:
        return False, err

    target_owner = (owner or "root").strip() or "root"
    if role == "user":
        if not username:
            return False, "Access denied."
        owned = get_user_owned_domains(username)
        if norm not in owned:
            return False, f"Access denied: You can only initialize DNS zones for your own domains."
        target_owner = username

    settings = get_dns_settings()
    if ip_override:
        try:
            settings = dict(settings)
            settings["default_ip"] = str(ipaddress.IPv4Address(ip_override.strip()))
        except ValueError:
            return False, "IPv4 override must be a valid IPv4 address."

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM dns_zones WHERE domain = ?", (norm,))
    if cur.fetchone():
        conn.close()
        return False, f"DNS zone for '{norm}' already exists."

    serial = _next_serial(0)
    primary_ns = _active_nameservers_for_domain(norm, settings)[0]
    rname = _format_soa_email(settings.get("hostmaster_email"), norm)
    zone_preview = {
        "domain": norm,
        "owner_username": target_owner,
        "serial": serial,
        "refresh": _DEFAULT_REFRESH,
        "retry": _DEFAULT_RETRY,
        "expire": _DEFAULT_EXPIRE,
        "minimum_ttl": _DEFAULT_MIN_TTL,
        "primary_ns": primary_ns,
        "hostmaster_email": settings.get("hostmaster_email") or rname,
        "enabled": 1,
    }
    default_records = _build_default_records(norm, settings)

    ok_bind, err_bind = _write_zone_and_sync_bind(
        zone_preview, default_records, settings=settings, reload_service=reload_service
    )
    if not ok_bind:
        conn.close()
        return False, err_bind

    cur.execute(
        "INSERT INTO dns_zones (domain, owner_username, serial, refresh, retry, expire, minimum_ttl, primary_ns, hostmaster_email, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            norm, target_owner, serial, _DEFAULT_REFRESH, _DEFAULT_RETRY,
            _DEFAULT_EXPIRE, _DEFAULT_MIN_TTL, primary_ns, zone_preview["hostmaster_email"],
        ),
    )
    zone_id = cur.lastrowid
    for rec in default_records:
        cur.execute(
            "INSERT INTO dns_records (zone_id, name, type, value, ttl, priority, weight, port) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                zone_id, rec["name"], rec["type"], rec["value"],
                rec["ttl"], rec["priority"], rec["weight"], rec["port"],
            ),
        )
    conn.commit()
    conn.close()
    return True, f"DNS zone for '{norm}' created with {len(default_records)} default records."


def ensure_zone_for_domain(domain, owner="root"):
    """Best-effort automatic DNS zone creation hook for domain/app provisioning."""
    settings = get_dns_settings()
    if not settings.get("auto_create_zone", True):
        return True, "Auto-zone creation disabled."
    ok, norm, err = validate_zone_domain(domain)
    if not ok:
        return False, err

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM dns_zones WHERE domain = ?", (norm,))
    exists = cur.fetchone()
    conn.close()
    if exists:
        return True, f"DNS zone for '{norm}' already exists."

    return create_dns_zone(norm, owner=owner or "root", role="admin", reload_service=True)


def reset_dns_zone(domain, role="admin", username=None):
    """Reset a zone's records back to the default template."""
    zone, _, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, err

    settings = get_dns_settings()
    norm = zone["domain"]
    new_serial = _next_serial(zone["serial"])
    primary_ns = _active_nameservers_for_domain(norm, settings)[0]
    zone_preview = dict(zone)
    zone_preview.update({
        "serial": new_serial,
        "primary_ns": primary_ns,
        "hostmaster_email": settings.get("hostmaster_email") or zone.get("hostmaster_email"),
    })
    default_records = _build_default_records(norm, settings)

    ok_bind, err_bind = _write_zone_and_sync_bind(
        zone_preview, default_records, settings=settings, reload_service=True
    )
    if not ok_bind:
        return False, err_bind

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM dns_records WHERE zone_id = ?", (zone["id"],))
    for rec in default_records:
        cur.execute(
            "INSERT INTO dns_records (zone_id, name, type, value, ttl, priority, weight, port) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                zone["id"], rec["name"], rec["type"], rec["value"],
                rec["ttl"], rec["priority"], rec["weight"], rec["port"],
            ),
        )
    cur.execute(
        "UPDATE dns_zones SET serial = ?, primary_ns = ?, hostmaster_email = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_serial, primary_ns, zone_preview["hostmaster_email"], zone["id"]),
    )
    conn.commit()
    conn.close()
    return True, f"DNS zone '{norm}' reset to default records."


def toggle_dns_zone(domain, enable, role="admin", username=None):
    """Enable or disable serving a DNS zone in BIND9."""
    zone, _, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, err

    new_enabled = 1 if enable else 0
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE dns_zones SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_enabled, zone["id"]),
    )
    conn.commit()
    conn.close()

    rebuild_named_conf_lite()
    ok_rel, err_rel = reload_bind9()
    if not ok_rel:
        return False, err_rel
    state = "enabled" if new_enabled else "disabled"
    return True, f"DNS zone '{zone['domain']}' {state}."


def delete_dns_zone(domain, role="admin", username=None, reload_bind=True):
    """Delete a DNS zone, its records, its BIND9 zone file, and update named.conf.lite-cpanel."""
    ok, norm, err = validate_zone_domain(domain)
    if not ok:
        return False, err

    conn = _dns_conn()
    zone, _ = _fetch_zone_and_records(conn, norm)
    if not zone:
        conn.close()
        return False, f"DNS zone '{norm}' not found."
    if not _check_zone_ownership(zone, role, username):
        conn.close()
        return False, "Access denied: You do not own this DNS zone."

    cur = conn.cursor()
    cur.execute("DELETE FROM dns_records WHERE zone_id = ?", (zone["id"],))
    cur.execute("DELETE FROM dns_zones WHERE id = ?", (zone["id"],))
    conn.commit()
    conn.close()

    try:
        zpath = _safe_zone_file_path(norm)
        if os.path.exists(zpath):
            os.remove(zpath)
    except Exception as exc:
        logging.warning("Failed deleting BIND zone file for %s: %s", norm, exc)

    rebuild_named_conf_lite()
    if reload_bind:
        ok_rel, err_rel = reload_bind9()
        if not ok_rel:
            return True, f"DNS zone '{norm}' deleted from DB, but BIND9 reload warned: {err_rel}"
    return True, f"DNS zone '{norm}' deleted successfully."


def delete_zones_for_domains(domains):
    """Remove DNS zones for a list of domains and reload BIND9 once (user/app cleanup hook)."""
    removed = []
    warnings = []
    seen = set()
    for dom in domains or []:
        ok, norm, _ = validate_zone_domain(dom)
        if not ok or norm in seen:
            continue
        seen.add(norm)
        conn = _dns_conn()
        zone, _ = _fetch_zone_and_records(conn, norm)
        if not zone:
            conn.close()
            continue
        cur = conn.cursor()
        cur.execute("DELETE FROM dns_records WHERE zone_id = ?", (zone["id"],))
        cur.execute("DELETE FROM dns_zones WHERE id = ?", (zone["id"],))
        conn.commit()
        conn.close()
        try:
            zpath = _safe_zone_file_path(norm)
            if os.path.exists(zpath):
                os.remove(zpath)
        except Exception as exc:
            warnings.append(f"Failed removing DNS zone file for {norm}: {exc}")
        removed.append(norm)

    if removed:
        rebuild_named_conf_lite()
        ok_rel, err_rel = reload_bind9()
        if not ok_rel:
            warnings.append(err_rel)
    return removed, warnings


def _validate_cname_conflicts(records):
    """Ensure CNAME exclusivity rules required by BIND9 named-checkzone."""
    by_name = {}
    for rec in records:
        by_name.setdefault(rec["name"], []).append(rec["type"])
    for owner, types in by_name.items():
        if "CNAME" in types:
            if owner == "@":
                return False, "Zone apex (@) cannot hold a CNAME record."
            if len(types) > 1:
                return False, f"Host '{owner}' cannot have both a CNAME and other DNS records ({', '.join(types)})."
    return True, ""


def add_dns_record(domain, rtype, name, value, ttl=14400, priority=None, weight=None, port=None, role="admin", username=None):
    """Validate and add a DNS record to an existing zone, verifying BIND9 syntax first."""
    zone, records, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, err

    ok, norm_rec, val_err = validate_dns_record(
        rtype, name, value, ttl=ttl, priority=priority, weight=weight, port=port
    )
    if not ok:
        return False, val_err

    # Prevent duplicate identical records
    for existing in records:
        if (
            existing["name"] == norm_rec["name"]
            and existing["type"] == norm_rec["type"]
            and existing["value"] == norm_rec["value"]
            and existing.get("priority") == norm_rec["priority"]
            and existing.get("weight") == norm_rec["weight"]
            and existing.get("port") == norm_rec["port"]
        ):
            return False, "An identical DNS record already exists."

    candidate_records = list(records) + [norm_rec]
    ok_cname, err_cname = _validate_cname_conflicts(candidate_records)
    if not ok_cname:
        return False, err_cname

    new_serial = _next_serial(zone["serial"])
    zone_preview = dict(zone)
    zone_preview["serial"] = new_serial

    ok_bind, err_bind = _write_zone_and_sync_bind(zone_preview, candidate_records, reload_service=True)
    if not ok_bind:
        return False, err_bind

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO dns_records (zone_id, name, type, value, ttl, priority, weight, port) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            zone["id"], norm_rec["name"], norm_rec["type"], norm_rec["value"],
            norm_rec["ttl"], norm_rec["priority"], norm_rec["weight"], norm_rec["port"],
        ),
    )
    cur.execute(
        "UPDATE dns_zones SET serial = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_serial, zone["id"]),
    )
    conn.commit()
    conn.close()
    return True, f"Added {norm_rec['type']} record '{norm_rec['name']}' -> '{norm_rec['value']}'."


def update_dns_record(domain, record_id, rtype, name, value, ttl=14400, priority=None, weight=None, port=None, role="admin", username=None):
    """Validate and update an existing DNS record cleanly."""
    zone, records, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, err

    try:
        rec_id = int(record_id)
    except (TypeError, ValueError):
        return False, "Invalid record ID."

    target_idx = next((idx for idx, r in enumerate(records) if r["id"] == rec_id), None)
    if target_idx is None:
        return False, "DNS record not found in this zone."

    old_rec = records[target_idx]
    ok, norm_rec, val_err = validate_dns_record(
        rtype, name, value, ttl=ttl, priority=priority, weight=weight, port=port
    )
    if not ok:
        return False, val_err

    # Protect last apex NS record from being changed to non-NS
    if old_rec["name"] == "@" and old_rec["type"] == "NS" and (norm_rec["name"] != "@" or norm_rec["type"] != "NS"):
        apex_ns_count = sum(1 for r in records if r["name"] == "@" and r["type"] == "NS")
        if apex_ns_count <= 1:
            return False, "Cannot remove or change the last remaining apex (@) NS record."

    candidate_records = list(records)
    updated_entry = dict(norm_rec)
    updated_entry["id"] = rec_id
    candidate_records[target_idx] = updated_entry

    ok_cname, err_cname = _validate_cname_conflicts(candidate_records)
    if not ok_cname:
        return False, err_cname

    new_serial = _next_serial(zone["serial"])
    zone_preview = dict(zone)
    zone_preview["serial"] = new_serial

    ok_bind, err_bind = _write_zone_and_sync_bind(zone_preview, candidate_records, reload_service=True)
    if not ok_bind:
        return False, err_bind

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE dns_records SET name = ?, type = ?, value = ?, ttl = ?, priority = ?, weight = ?, port = ? "
        "WHERE id = ? AND zone_id = ?",
        (
            norm_rec["name"], norm_rec["type"], norm_rec["value"], norm_rec["ttl"],
            norm_rec["priority"], norm_rec["weight"], norm_rec["port"], rec_id, zone["id"],
        ),
    )
    cur.execute(
        "UPDATE dns_zones SET serial = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_serial, zone["id"]),
    )
    conn.commit()
    conn.close()
    return True, f"Updated {norm_rec['type']} record '{norm_rec['name']}'."


def delete_dns_record(domain, record_id, role="admin", username=None):
    """Delete a DNS record from a zone while protecting the final apex NS record."""
    zone, records, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, err

    try:
        rec_id = int(record_id)
    except (TypeError, ValueError):
        return False, "Invalid record ID."

    target = next((r for r in records if r["id"] == rec_id), None)
    if not target:
        return False, "DNS record not found in this zone."

    if target["name"] == "@" and target["type"] == "NS":
        apex_ns_count = sum(1 for r in records if r["name"] == "@" and r["type"] == "NS")
        if apex_ns_count <= 1:
            return False, "Cannot delete the last remaining apex (@) NS record."

    candidate_records = [r for r in records if r["id"] != rec_id]
    new_serial = _next_serial(zone["serial"])
    zone_preview = dict(zone)
    zone_preview["serial"] = new_serial

    ok_bind, err_bind = _write_zone_and_sync_bind(zone_preview, candidate_records, reload_service=True)
    if not ok_bind:
        return False, err_bind

    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM dns_records WHERE id = ? AND zone_id = ?", (rec_id, zone["id"]))
    cur.execute(
        "UPDATE dns_zones SET serial = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_serial, zone["id"]),
    )
    conn.commit()
    conn.close()
    return True, f"Deleted {target['type']} record '{target['name']}'."


def sync_nameserver_glue_records():
    """Ensure glue A records exist for configured NS hostnames whose parent zone exists locally."""
    settings = get_dns_settings()
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM dns_zones")
    domains = [r["domain"] for r in cur.fetchall()]
    conn.close()

    synced_domains = []
    for dom in domains:
        glue_pairs = _glue_pairs_for_domain(dom, settings)
        if not glue_pairs:
            continue
        conn = _dns_conn()
        zone, records = _fetch_zone_and_records(conn, dom)
        if not zone:
            conn.close()
            continue

        ttl = int(settings.get("default_ttl") or _DEFAULT_RECORD_TTL)
        changed = False
        candidate_records = list(records)
        cur = conn.cursor()

        for sub, glue_ip in glue_pairs:
            existing = [r for r in candidate_records if r["name"] == sub and r["type"] == "A"]
            if existing:
                if existing[0]["value"] != glue_ip:
                    existing[0]["value"] = glue_ip
                    changed = True
            else:
                candidate_records.append({
                    "id": None, "name": sub, "type": "A", "value": glue_ip,
                    "ttl": ttl, "priority": None, "weight": None, "port": None,
                })
                changed = True

        if not changed:
            conn.close()
            continue

        new_serial = _next_serial(zone["serial"])
        zone_preview = dict(zone)
        zone_preview["serial"] = new_serial
        ok_bind, _ = _write_zone_and_sync_bind(zone_preview, candidate_records, settings=settings, reload_service=False)
        if ok_bind:
            for sub, glue_ip in glue_pairs:
                cur.execute("SELECT id FROM dns_records WHERE zone_id = ? AND name = ? AND type = 'A'", (zone["id"], sub))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE dns_records SET value = ? WHERE id = ?", (glue_ip, row["id"]))
                else:
                    cur.execute(
                        "INSERT INTO dns_records (zone_id, name, type, value, ttl) VALUES (?, ?, 'A', ?, ?)",
                        (zone["id"], sub, glue_ip, ttl),
                    )
            cur.execute(
                "UPDATE dns_zones SET serial = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_serial, zone["id"]),
            )
            conn.commit()
            synced_domains.append(dom)
        conn.close()

    if synced_domains:
        reload_bind9()
    return synced_domains


def apply_nameservers_to_all_zones():
    """Apply configured global NS/SOA settings to every existing DNS zone and reload BIND9 once."""
    settings = get_dns_settings()
    if not settings.get("nameservers_configured"):
        return False, "Configure at least NS1 and NS2 before applying to zones."

    ttl = int(settings.get("default_ttl") or _DEFAULT_RECORD_TTL)
    conn = _dns_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM dns_zones ORDER BY domain")
    domains = [r["domain"] for r in cur.fetchall()]
    conn.close()

    if not domains:
        return True, "No existing zones to update."

    updated = 0
    errors = []
    for dom in domains:
        conn = _dns_conn()
        zone, records = _fetch_zone_and_records(conn, dom)
        if not zone:
            conn.close()
            continue

        new_ns_list = _active_nameservers_for_domain(dom, settings)
        primary_ns = new_ns_list[0]
        new_serial = _next_serial(zone["serial"])

        non_apex_ns = [r for r in records if not (r["name"] == "@" and r["type"] == "NS")]
        new_ns_recs = [
            {
                "name": "@", "type": "NS", "value": ns_fqdn, "ttl": ttl,
                "priority": None, "weight": None, "port": None,
            }
            for ns_fqdn in new_ns_list
        ]

        # Ensure glue A records exist if nameservers belong to this domain
        for sub, glue_ip in _glue_pairs_for_domain(dom, settings):
            match = next((r for r in non_apex_ns if r["name"] == sub and r["type"] == "A"), None)
            if match:
                match["value"] = glue_ip
            else:
                non_apex_ns.append({
                    "name": sub, "type": "A", "value": glue_ip, "ttl": ttl,
                    "priority": None, "weight": None, "port": None,
                })

        candidate_records = new_ns_recs + non_apex_ns
        zone_preview = dict(zone)
        zone_preview.update({
            "serial": new_serial,
            "primary_ns": primary_ns,
            "hostmaster_email": settings.get("hostmaster_email"),
        })

        ok_bind, err_bind = _write_zone_and_sync_bind(
            zone_preview, candidate_records, settings=settings, reload_service=False
        )
        if not ok_bind:
            errors.append(f"{dom}: {err_bind}")
            conn.close()
            continue

        cur = conn.cursor()
        cur.execute("DELETE FROM dns_records WHERE zone_id = ? AND name = '@' AND type = 'NS'", (zone["id"],))
        for rec in new_ns_recs:
            cur.execute(
                "INSERT INTO dns_records (zone_id, name, type, value, ttl) VALUES (?, ?, 'NS', ?, ?)",
                (zone["id"], rec["name"], rec["value"], rec["ttl"]),
            )
        for sub, glue_ip in _glue_pairs_for_domain(dom, settings):
            cur.execute("SELECT id FROM dns_records WHERE zone_id = ? AND name = ? AND type = 'A'", (zone["id"], sub))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE dns_records SET value = ? WHERE id = ?", (glue_ip, row["id"]))
            else:
                cur.execute(
                    "INSERT INTO dns_records (zone_id, name, type, value, ttl) VALUES (?, ?, 'A', ?, ?)",
                    (zone["id"], sub, glue_ip, ttl),
                )
        cur.execute(
            "UPDATE dns_zones SET serial = ?, primary_ns = ?, hostmaster_email = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_serial, primary_ns, settings.get("hostmaster_email"), zone["id"]),
        )
        conn.commit()
        conn.close()
        updated += 1

    ok_rel, err_rel = reload_bind9()
    if not ok_rel:
        errors.append(err_rel)
    if errors:
        return False, f"Updated {updated} zone(s) with errors: {'; '.join(errors)}"
    return True, f"Updated nameservers across {updated} DNS zone(s)."


def get_zone_file_preview(domain, role="admin", username=None):
    """Return rendered BIND9 zone file content and validation status for previewing in UI."""
    zone, records, err = get_zone_with_records(domain, role=role, username=username)
    if not zone:
        return False, "", err
    rendered = render_zone_file(zone, records)
    return True, rendered, ""


def is_dns_port_open_in_csf():
    """Check whether port 53 is allowed in TCP_IN and UDP_IN of CSF firewall."""
    if not os.path.exists(CSF_CONF_PATH):
        return None
    try:
        found = {}
        with open(CSF_CONF_PATH, "r") as f:
            for line in f:
                s = line.strip()
                for key in ("TCP_IN", "UDP_IN"):
                    if s.startswith(f"{key} =") or s.startswith(f"{key}="):
                        val = s.split("=", 1)[1].strip().strip('"')
                        found[key] = {p.strip() for p in val.split(",") if p.strip()}
        return "53" in found.get("TCP_IN", set()) and "53" in found.get("UDP_IN", set())
    except Exception:
        return None


def open_dns_port_in_csf():
    """Open TCP/UDP port 53 in CSF firewall if installed."""
    if not os.path.exists(CSF_CONF_PATH):
        return False, "CSF firewall config not found."
    try:
        with open(CSF_CONF_PATH, "r") as f:
            lines = f.readlines()

        target_keys = ("TCP_IN", "TCP_OUT", "UDP_IN", "UDP_OUT", "TCP6_IN", "UDP6_IN")
        new_lines = []
        changed = False
        for line in lines:
            s = line.strip()
            matched_key = next(
                (k for k in target_keys if s.startswith(f"{k} =") or s.startswith(f"{k}=")),
                None,
            )
            if matched_key:
                raw_val = s.split("=", 1)[1].strip().strip('"')
                ports = [p.strip() for p in raw_val.split(",") if p.strip()]
                if "53" not in ports:
                    ports.append("53")
                    changed = True
                new_lines.append(f'{matched_key} = "{",".join(ports)}"\n')
            else:
                new_lines.append(line)

        if changed:
            with open(CSF_CONF_PATH, "w") as f:
                f.writelines(new_lines)
            if shutil.which("csf"):
                subprocess.run(["csf", "-r"], capture_output=True, text=True, timeout=20)
        return True, "Port 53 (TCP/UDP) is open in CSF Firewall."
    except Exception as exc:
        return False, f"Failed opening DNS port in CSF: {exc}"


def install_bind9_generator():
    """Stream NDJSON progress while installing and initializing BIND9 on Ubuntu/Debian."""
    def emit(progress, message, error=False, success=False, detail=""):
        payload = {"progress": progress, "message": message, "error": error, "success": success}
        if detail:
            payload["detail"] = detail
        return json.dumps(payload) + "\n"

    try:
        env = _non_interactive_env()
        yield emit(10, "Updating APT package lists...")
        res_upd = subprocess.run(
            ["apt-get", "update", "-o", "Acquire::ForceIPv4=true"],
            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180
        )
        if res_upd.returncode != 0:
            res_upd = subprocess.run(
                ["apt-get", "update"],
                env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180
            )
            if res_upd.returncode != 0:
                yield emit(15, f"apt-get update failed: {(res_upd.stderr or res_upd.stdout).strip()}", error=True)
                return

        yield emit(35, "Installing BIND9 DNS server packages (bind9, bind9utils, dnsutils)...")
        res_inst = subprocess.run(
            ["apt-get", "install", "-y"] + _APT_INSTALL_OPTS + ["bind9", "bind9utils", "dnsutils"],
            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300
        )
        if res_inst.returncode != 0:
            err = (res_inst.stderr or res_inst.stdout).strip()
            yield emit(40, f"Package installation failed: {err}", error=True)
            return

        yield emit(65, "Configuring /etc/bind/lite-cpanel directory and authoritative DNS options...")
        ensure_bind9_base_config()

        # Render any DNS zones created prior to BIND9 installation
        conn = _dns_conn()
        cur = conn.cursor()
        cur.execute("SELECT domain FROM dns_zones")
        existing_domains = [r["domain"] for r in cur.fetchall()]
        conn.close()

        for dom in existing_domains:
            conn = _dns_conn()
            zone, records = _fetch_zone_and_records(conn, dom)
            conn.close()
            if zone:
                _write_zone_and_sync_bind(zone, records, reload_service=False)

        rebuild_named_conf_lite()
        ok_conf, err_conf = validate_bind_config()
        if not ok_conf:
            yield emit(75, err_conf, error=True)
            return

        yield emit(82, "Enabling and starting BIND9 service...")
        svc = get_bind9_service_name()
        subprocess.run(["systemctl", "enable", svc], capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
        res_start = subprocess.run(
            ["systemctl", "restart", svc],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=25
        )
        if res_start.returncode != 0:
            yield emit(85, f"BIND9 failed to start: {(res_start.stderr or res_start.stdout).strip()}", error=True)
            return

        if os.path.exists(CSF_CONF_PATH):
            yield emit(92, "Opening DNS port 53 (TCP/UDP) in CSF firewall...")
            open_dns_port_in_csf()

        yield emit(100, "BIND9 DNS server installed and running successfully!", success=True)
    except subprocess.TimeoutExpired:
        yield emit(0, "BIND9 installation timed out. Check /var/log/apt/term.log and retry.", error=True)
    except Exception as exc:
        yield emit(0, f"DNS installation error: {exc}", error=True)

