# DNS Management Implementation Study for Lite-cPanel

This document outlines a complete strategy for integrating a fully functional DNS Management system into Lite-cPanel without making any modifications right now, as requested.

## 1. Core Architecture & Dependencies

To provide DNS services, the server needs a DNS daemon. **BIND9** (named) is the industry standard for hosting panels (including the original cPanel) and is highly recommended due to its reliability and file-based configuration.

*   **System Package**: `bind9` `bind9utils` `bind9-doc` (on Ubuntu/Debian)
*   **Zone File Storage**: A dedicated directory for Lite-cPanel zone files, e.g., `/etc/bind/lite-cpanel/`
*   **Main Config**: Include the Lite-cPanel zones in BIND's local config by appending `include "/etc/bind/lite-cpanel/named.conf.lite-cpanel";` to `/etc/bind/named.conf.local`.

## 2. Database Schema Additions

The `hosting.db` (SQLite) will need new tables to persistently store DNS records and zone configurations.

```sql
CREATE TABLE IF NOT EXISTS dns_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    owner_username TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dns_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL,
    name TEXT NOT NULL,       -- e.g., '@', 'www', 'mail', or 'subdomain'
    type TEXT NOT NULL,       -- 'A', 'AAAA', 'CNAME', 'MX', 'TXT', 'SRV'
    value TEXT NOT NULL,      -- e.g., '192.168.1.1', 'ghs.google.com'
    ttl INTEGER DEFAULT 14400,
    priority INTEGER,         -- specific to MX/SRV records
    FOREIGN KEY(zone_id) REFERENCES dns_zones(id) ON DELETE CASCADE
);
```

## 3. Backend Logic (`dns_mgr.py`)

A new manager file (`cpanel/app/dns_mgr.py`) would be created to handle the core logic:

*   `add_zone(domain, username)`: Creates a new zone in the DB and generates a default zone file.
*   `delete_zone(domain)`: Removes the zone and its file.
*   `add_record(...)`, `edit_record(...)`, `delete_record(...)`: CRUD operations for individual DNS records.
*   `generate_zone_file(domain)`: 
    *   Fetches all records for a domain from the database.
    *   Formats them into standard BIND9 zone file syntax (SOA record, NS records, and the user's A/CNAME/TXT records).
    *   Writes to `/etc/bind/lite-cpanel/zones/domain.com.db`.
*   `reload_bind()`: Executes `sudo systemctl reload bind9` to apply changes instantly.

## 4. Automation & Integration

DNS should be tightly coupled with the existing Domain and User management systems:

*   **When a Virtual Host is created** (in `domains_mgr.py`): Automatically call `dns_mgr.add_zone(domain)` to generate a default zone file containing:
    *   `A` record pointing the main domain (`@`) to the server's public IP.
    *   `CNAME` record pointing `www` to `@`.
    *   `MX` and `TXT` (SPF/DMARC) records if you plan to support email routing later.
*   **When a Domain is deleted**: Automatically delete the DNS zone to keep the server clean.
*   **User Limits**: Similar to databases, users can be restricted by their hosting plan (e.g., maximum allowed DNS zones or records).

## 5. Web Interface (Frontend)

*   **Sidebar Addition**: A new menu item "DNS Manager" (`<i class="fas fa-network-wired"></i>`).
*   **Routing (`lib/routes_dns.py`)**: Endpoints like `/dns`, `/dns/add_record`, `/dns/delete_record`.
*   **Template (`dns_manager.html`)**:
    *   A dropdown at the top for the user to select which of their domains they want to manage.
    *   A table listing all current records (Type, Name, Value, TTL, Priority).
    *   A clean modal or inline form to "Add New Record", with JavaScript validation (e.g., preventing users from entering letters into an A record's IP field).

## 6. Security Considerations

*   **Validation**: Extremely strict backend regex validation is required for DNS records to prevent BIND9 from failing to start/reload due to syntax errors. If one zone file has a syntax error, it can crash the entire BIND daemon.
*   **Permissions**: The `/etc/bind/lite-cpanel/` directory must be writable by the backend daemon (or via root subprocess calls) but secured against unauthorized access.
*   **Ownership Check**: Ensure `testuser` can only query and edit `dns_records` associated with a `zone_id` they own.
