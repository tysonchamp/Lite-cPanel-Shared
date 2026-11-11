"""Flask Blueprint routes for BIND9 DNS Zone Management and Root Nameserver configuration."""

import json
from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

from auth import login_required
from dns_mgr import (
    SUPPORTED_RECORD_TYPES,
    add_dns_record,
    apply_nameservers_to_all_zones,
    create_dns_zone,
    delete_dns_record,
    delete_dns_zone,
    get_bind9_service_name,
    get_bind9_status,
    get_dns_settings,
    get_uninitialized_owned_domains,
    get_zone_file_preview,
    get_zone_with_records,
    install_bind9_generator,
    is_bind9_installed,
    is_dns_port_open_in_csf,
    list_dns_zones,
    manage_bind9_service,
    normalize_domain,
    open_dns_port_in_csf,
    reset_dns_zone,
    save_dns_settings,
    toggle_dns_zone,
    update_dns_record,
)
from hosting_mgr import get_owner_of_resource, get_users

dns_bp = Blueprint("dns", __name__)


def _ndjson_stream_response(is_admin, denied_message, gen_factory):
    """Return a streaming NDJSON Response for admin-only operations or 403 payload."""
    if not is_admin:
        return Response(
            json.dumps({"progress": 100, "message": denied_message, "error": True}) + "\n",
            status=403,
            mimetype="application/x-ndjson",
        )
    return Response(
        stream_with_context(gen_factory()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _dns_redirect(tab="zones", domain=None):
    """Build redirect response to DNS manager preserving active tab and domain."""
    params = {}
    if tab and tab != "zones":
        params["tab"] = tab
    if domain:
        params["domain"] = normalize_domain(domain)
    return redirect(url_for("dns.dns_manager", **params))


def _resolve_domain_owner(domain, users_map):
    """Find the hosting owner of an existing domain/app for admin bulk zone initialization."""
    norm = normalize_domain(domain)
    for res_type in ("domains", "nextjs_apps", "docker_apps"):
        owner = get_owner_of_resource(res_type, norm)
        if owner:
            return owner
    for uname, udata in (users_map or {}).items():
        if normalize_domain(udata.get("main_domain", "")) == norm:
            return uname
    return "root"


def _parse_record_form(selected_domain):
    """Extract normalized DNS record fields from the submitted POST form."""
    return {
        "domain": normalize_domain(request.form.get("domain") or selected_domain),
        "rtype": request.form.get("rtype", ""),
        "name": request.form.get("name", ""),
        "value": request.form.get("value", ""),
        "ttl": request.form.get("ttl", 14400),
        "priority": (request.form.get("priority") or "").strip() or None,
        "weight": (request.form.get("weight") or "").strip() or None,
        "port": (request.form.get("port") or "").strip() or None,
    }


@dns_bp.route("/dns", methods=["GET", "POST"])
@login_required
def dns_manager():
    """Render and handle BIND9 DNS zones, records, and Root Nameserver management."""
    role = session.get("role")
    username = session.get("username")
    is_admin = role == "admin"

    tab = (request.values.get("tab") or "zones").strip().lower()
    if tab not in ("zones", "nameservers") or (tab == "nameservers" and not is_admin):
        tab = "zones"

    selected_domain = normalize_domain(request.values.get("domain") or "")

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "install_bind9":
            return _ndjson_stream_response(
                is_admin,
                "Access denied. Only the root user can install and configure BIND9.",
                install_bind9_generator,
            )

        if action == "service_action":
            if not is_admin:
                flash("Access denied. Only the root user can manage the BIND9 service.", "danger")
                return _dns_redirect("zones", selected_domain)
            svc_action = (request.form.get("service_action") or "").strip().lower()
            ok, msg = manage_bind9_service(svc_action)
            flash(msg, "success" if ok else "danger")
            return _dns_redirect(tab, selected_domain)

        if action == "open_port53":
            if not is_admin:
                flash("Access denied. Only the root user can modify firewall rules.", "danger")
                return _dns_redirect("zones", selected_domain)
            ok, msg = open_dns_port_in_csf()
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("nameservers", selected_domain)

        if action == "save_nameservers":
            if not is_admin:
                flash("Access denied. Root privileges required for nameserver management.", "danger")
                return _dns_redirect("zones", selected_domain)
            payload = {
                "ns1": request.form.get("ns1", ""),
                "ns2": request.form.get("ns2", ""),
                "ns3": request.form.get("ns3", ""),
                "ns4": request.form.get("ns4", ""),
                "ns1_ip": request.form.get("ns1_ip", ""),
                "ns2_ip": request.form.get("ns2_ip", ""),
                "ns3_ip": request.form.get("ns3_ip", ""),
                "ns4_ip": request.form.get("ns4_ip", ""),
                "default_ip": request.form.get("default_ip", ""),
                "default_ipv6": request.form.get("default_ipv6", ""),
                "hostmaster_email": request.form.get("hostmaster_email", ""),
                "default_ttl": request.form.get("default_ttl", 14400),
                "auto_create_zone": request.form.get("auto_create_zone") == "1",
            }
            apply_to_existing = request.form.get("apply_to_existing") == "1"
            sync_glue = request.form.get("sync_glue", "1") == "1"
            ok, msg = save_dns_settings(
                payload, apply_to_existing=apply_to_existing, sync_glue=sync_glue
            )
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("nameservers", selected_domain)

        if action == "apply_nameservers":
            if not is_admin:
                flash("Access denied. Root privileges required for nameserver management.", "danger")
                return _dns_redirect("zones", selected_domain)
            ok, msg = apply_nameservers_to_all_zones()
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("nameservers", selected_domain)

        if action == "create_zone":
            target_domain = normalize_domain(request.form.get("domain") or "")
            owner = (request.form.get("owner") or "root").strip() if is_admin else username
            ip_override = (request.form.get("ip_override") or "").strip() or None
            ok, msg = create_dns_zone(
                target_domain,
                owner=owner,
                role=role,
                username=username,
                ip_override=ip_override,
            )
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", target_domain if ok else selected_domain)

        if action == "init_all_zones":
            missing = get_uninitialized_owned_domains(role=role, username=username)
            if not missing:
                flash("All eligible domains already have DNS zones.", "info")
                return _dns_redirect("zones", selected_domain)
            users_map = get_users() if is_admin else {}
            created_count = 0
            errors = []
            first_created = None
            for dom in missing:
                dom_owner = _resolve_domain_owner(dom, users_map) if is_admin else username
                ok, msg = create_dns_zone(
                    dom, owner=dom_owner, role=role, username=username, reload_service=False
                )
                if ok:
                    created_count += 1
                    first_created = first_created or dom
                else:
                    errors.append(f"{dom}: {msg}")
            if created_count:
                if is_bind9_installed() and get_bind9_status() == "active":
                    manage_bind9_service("reload")
                flash(f"Initialized {created_count} DNS zone(s).", "success")
            if errors:
                flash(f"Some zones failed: {'; '.join(errors[:5])}", "warning")
            return _dns_redirect("zones", first_created or selected_domain)

        if action == "delete_zone":
            target_domain = normalize_domain(request.form.get("domain") or "")
            ok, msg = delete_dns_zone(
                target_domain, role=role, username=username, reload_bind=True
            )
            flash(msg, "success" if ok else "danger")
            next_dom = None if (ok and target_domain == selected_domain) else selected_domain
            return _dns_redirect("zones", next_dom)

        if action == "toggle_zone":
            target_domain = normalize_domain(request.form.get("domain") or "")
            enable = request.form.get("enable") == "1"
            ok, msg = toggle_dns_zone(target_domain, enable, role=role, username=username)
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", target_domain)

        if action == "reset_zone":
            target_domain = normalize_domain(request.form.get("domain") or "")
            ok, msg = reset_dns_zone(target_domain, role=role, username=username)
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", target_domain)

        if action == "add_record":
            rec = _parse_record_form(selected_domain)
            ok, msg = add_dns_record(
                rec["domain"],
                rec["rtype"],
                rec["name"],
                rec["value"],
                ttl=rec["ttl"],
                priority=rec["priority"],
                weight=rec["weight"],
                port=rec["port"],
                role=role,
                username=username,
            )
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", rec["domain"])

        if action == "edit_record":
            rec = _parse_record_form(selected_domain)
            record_id = request.form.get("record_id")
            ok, msg = update_dns_record(
                rec["domain"],
                record_id,
                rec["rtype"],
                rec["name"],
                rec["value"],
                ttl=rec["ttl"],
                priority=rec["priority"],
                weight=rec["weight"],
                port=rec["port"],
                role=role,
                username=username,
            )
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", rec["domain"])

        if action == "delete_record":
            target_domain = normalize_domain(request.form.get("domain") or selected_domain)
            record_id = request.form.get("record_id")
            ok, msg = delete_dns_record(
                target_domain, record_id, role=role, username=username
            )
            flash(msg, "success" if ok else "danger")
            return _dns_redirect("zones", target_domain)

        flash("Unknown DNS action requested.", "warning")
        return _dns_redirect(tab, selected_domain)

    zones = list_dns_zones(role=role, username=username)
    uninitialized_domains = get_uninitialized_owned_domains(role=role, username=username)
    settings = get_dns_settings()
    bind9_installed = is_bind9_installed()
    bind9_status = get_bind9_status()
    csf_port53_open = is_dns_port_open_in_csf() if is_admin else None

    active_zone = None
    records = []
    if selected_domain:
        active_zone, records, zone_err = get_zone_with_records(
            selected_domain, role=role, username=username
        )
        if not active_zone and zone_err:
            flash(zone_err, "warning")
    elif zones:
        active_zone, records, _ = get_zone_with_records(
            zones[0]["domain"], role=role, username=username
        )

    hosting_users = sorted((get_users() or {}).keys()) if is_admin else []

    return render_template(
        "dns.html",
        is_admin=is_admin,
        active_tab=tab,
        zones=zones,
        active_zone=active_zone,
        records=records,
        uninitialized_domains=uninitialized_domains,
        settings=settings,
        server_ip=settings.get("default_ip", ""),
        bind9_installed=bind9_installed,
        bind9_status=bind9_status,
        bind9_service_name=get_bind9_service_name(),
        csf_port53_open=csf_port53_open,
        record_types=SUPPORTED_RECORD_TYPES,
        hosting_users=hosting_users,
    )


@dns_bp.route("/api/dns/zone-file/<domain>", methods=["GET"])
@login_required
def api_zone_file(domain):
    """Return rendered BIND9 zone file preview after enforcing zone ownership."""
    role = session.get("role")
    username = session.get("username")
    ok, content, err = get_zone_file_preview(domain, role=role, username=username)
    if not ok:
        status_code = 403 if "Access denied" in (err or "") else 404
        return jsonify({"success": False, "message": err}), status_code
    return jsonify({"success": True, "domain": normalize_domain(domain), "content": content})
