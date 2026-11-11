# Updates:

## 11.11.2026 (BIND9 DNS Server, Nameserver Management, Multi-PHP Manager & Lifecycle Cleanup)

1. **Multi-PHP Manager (`7.0`–`8.5`)**:
   - Added a complete Multi-PHP Manager (`php_mgr.py`, `routes_php.py`, `templates/php.html`) supporting PHP `7.0` through `8.5` across **Hybrid**, **Nginx**, and **Apache** stacks.
   - Supports per-domain and bulk domain PHP switching (or `Inherit System Default`), system default CLI/Nginx/Apache switching, PHP-FPM service controls (start/stop/restart/reload), extension management, and clean uninstallation of non-default unused versions.
   - Added an interactive **PHP INI Editor** for core directives (`memory_limit`, `upload_max_filesize`, `post_max_size`, `max_execution_time`, `max_input_time`, `max_input_vars`, `display_errors`, `date.timezone`) per PHP version and SAPI (`fpm`, `apache2`, `cli`).
2. **Sury PHP Repository & Isolated APT Package Pinning**:
   - Switched PHP repository provisioning in both `scripts/install_cpanel.sh` and `php_mgr.py` to `packages.sury.org/php` over IPv4-first HTTPS with GPG keyring verification and a manual PGP key import fallback in the UI.
   - Added permanent unversioned PHP metapackage pins (`/etc/apt/preferences.d/99-lite-cpanel-php`) alongside scoped per-install/per-upgrade series pinning and `apt-mark manual` preservation for versioned PECL extensions (such as `php*-mongodb`), preventing unintended installations of newer PHP releases (`8.4`/`8.5`) and `php-common` conflict breaks on Ubuntu 24.04.
3. **PHP Version Updater**:
   - Added APT policy update checking (`apt-cache policy`) and streaming `--only-upgrade` package updates for individual PHP versions or all installed versions simultaneously, preserving running PHP-FPM states and CLI `update-alternatives`.
4. **Comprehensive User Deletion Cleanup (`user_cleanup_mgr.py`)**:
   - Deleting a shared user from Manage Users now cleans up all associated system resources in safe order: PM2 processes, Docker Compose stacks/containers/volumes/images, Nginx/Apache virtual hosts, Let's Encrypt certificates, domain logs, MySQL/MariaDB databases & users, MongoDB databases & users, Pure-FTPd/SFTP accounts & jail symlinks, user crontabs, running OS processes, and the Linux user/group and `/home/<user>` directory, with structured summary/warning reporting.
5. **Domain & Next.js App Deletion Lifecycle**:
   - Added virtual host deletion on the **Domains** page (`domains_mgr.py`, `routes_domains.py`, `domains.html`) for both `root` (`admin`) and shared users (for their addon domains), cleaning up Nginx/Apache vhosts, Certbot certs, domain logs, document roots, and `user_resources` tracking.
   - Merged `-le-ssl.conf` detection cleanly under `Apache SSL` (eliminating duplicate `-le-ssl` rows) and enforced strict protection preventing standalone deletion of any account's primary `main_domain`, even by `root`.
   - Upgraded Next.js app deletion (`nextjs_mgr.py`) to stop/remove matching PM2 processes, delete proxy vhosts/SSL/logs, release allocated ports, and safely delete the app directory (preserving `public_html` when attached to a `main_domain`).
6. **Admin-Only Access Hardening**:
   - Enforced `@admin_required` and strict `role == 'admin'` checks on sensitive routes and installers (ModSecurity, MongoDB & Mongo Express install/credentials access, global PM2 installation, Multi-PHP management tabs/API/actions, Hosting Plans, Manage Users, System Settings, Firewall, Backups, and Web Terminal) and updated blueprint-namespaced auth redirects.
7. **BIND9 DNS Server, Root Nameserver Management & Per-User DNS Manager**:
   - Added a complete authoritative BIND9 DNS server module (`dns_mgr.py`, `routes_dns.py`, `templates/dns.html`) with pre-commit `named-checkzone` / `named-checkconf` validation, CNAME exclusivity checks, RFC 1035 multi-chunk TXT formatting, last apex `NS` protection, non-recursive hardening (`recursion no`, `allow-transfer { none; }`), and streaming in-panel BIND9 installation plus `install_cpanel.sh` provisioning.
   - Added **Root Nameserver Management** (`?tab=nameservers` & sidebar **Nameservers (NS)** shortcut for `root`): configure global `NS1`–`NS4` nameservers and glue IPv4s, default IPv4/IPv6, hostmaster email, default TTL, auto-create toggle, automatic glue `A` record sync, one-click bulk zone synchronization, BIND9 service controls, and CSF TCP/UDP Port 53 opening.
   - Added a dedicated **DNS Manager** sidebar menu (`/dns`) accessible to all users: shared hosting users can initialize and manage DNS zones (`A`, `AAAA`, `CNAME`, `MX`, `TXT`, `NS`, `SRV`, `CAA`), SPF/DMARC/CAA quick presets, zone enable/pause, reset, and raw zone previews strictly scoped to their own domains.
   - Wired automatic DNS zone creation and deletion hooks into `domains_mgr.py`, `nextjs_mgr.py`, `docker_mgr.py`, `user_cleanup_mgr.py`, `routes_dashboard.py` service status/restart, and `settings_mgr.py` BIND config editing.

## 29.08.2026 (Multi-User, Hosting Plans, Docker & Modular Architecture Update)

1. **Multi-User Hosting & Quota Enforcement**: Introduced SQLite-backed Hosting Plans (`max_domains`, `max_databases`, `max_mongodb`, `max_nextjs`, `max_docker`), isolated shared user accounts, automated `main_domain` virtual host + home web-root provisioning, auto-created jailed SFTP/FTP accounts, and tenant-scoped views across domains, databases, MongoDB, cron, and traffic stats.
2. **Docker Manager**: Added Docker container controls (`start`, `stop`, `restart`, `remove`, container run), `docker compose` execution, automated port allocation (`9000+`), and Apache/Nginx reverse-proxy mapping with SSL support.
3. **Modular Flask Blueprints**: Refactored panel routing into dedicated modules under `cpanel/app/lib/` (`routes_auth`, `routes_dashboard`, `routes_domains`, `routes_databases`, `routes_nextjs`, `routes_filemanager`, `routes_system`, `routes_wordpress`, `routes_admin`, `routes_docker`).
4. **Next.js, WordPress & SFTP Refinements**: Added domain-derived document roots and automated port allocation (`3000+`) for Next.js apps, NVM Node.js integration, custom document roots with isolated ownership for WordPress, and symlinked per-group SFTP chroot jails.
5. **DNS Study**: Added `dns_management_study.md` evaluating future DNS management integration.

## 23.04.2026 (Security & Framework Optimization Update)

1. **Jailed SFTP (Chroot)**: Implemented ChrootDirectory for FTP users, restricting them to their home directories and enhancing server security.
2. **Private Group Isolation**: Refactored FTP user management to assign unique private groups to each user, ensuring total data privacy and preventing cross-account access.
3. **Global Security Shield**: Added an automated "Security Hardening" feature. This blocks access to all dotfiles (including `.env`) via web requests and disables directory indexing (Autoindex) on both Apache and Nginx.
4. **Framework Permission Fixes**: Enhanced the File Manager to automatically apply secure 770/660 permissions to web roots and specific framework directories (e.g., `/storage`, `/bootstrap/cache` for Laravel), resolving "Permission denied" errors.
5. **Performance & Reliability**:
   - Integrated a background caching worker for the Dashboard to pre-fetch system metrics, reducing load time by up to 90%.
   - Added automatic service restarts (Apache/Nginx/PHP-FPM) triggered by permission changes to ensure changes take effect immediately.
   - Fixed "Connection reset by peer" errors in SFTP by resolving path resolution logic.

21.04.2026 (Major UI & Analytics Update)

1. **Slick Modern UI/UX**: Completely redesigned the dashboard and sidebar with a premium midnight-gradient aesthetic, refined typography (Inter), and glassmorphism-inspired cards.
2. **Web Traffic Analytics**: Integrated **GoAccess** for real-time web traffic monitoring. Added a dedicated Traffic Monitor module providing per-domain hits, unique visitors, bandwidth stats, and full interactive HTML reports.
3. **Process Manager Hardening**: Implemented **Automatic PM2 Resurrection** on system boot and hardened NVM environment paths to ensure Node.js/Next.js applications persist across system restarts.
4. **Advanced System Metrics**: Expanded the server information module to display CPU clock frequency, platform details (Architecture/KVM), and full OS Distro names (e.g., AlmaLinux/Ubuntu).
5. **Security Log Parsing**: Enhanced SSH login audit logs with dual-format timestamp parsing (ISO/Syslog) for clean, human-readable security event monitoring.
6. **Terminal UX**: Upgraded the web terminal with a custom minimalist scrollbar and improved container styling for a pro-developer feel.
7. **Infrastructure Refactoring**: Migrated to a centralized `base.html` architecture to improve panel stability and eliminate template-related Internal Server Errors.

21.04.2026 (Initial Release)

1. Implemented a full **Node.js Process Manager** powered by PM2, allowing live monitoring and control of background applications.
2. Updated **Certbot** installation to the official Snap-based method for improved reliability on Ubuntu 22.04 and 24.04.
3. Enhanced **SSL Generation** with smart webserver detection (Apache vs Nginx) and automated DNS verification for subdomains to prevent NXDOMAIN failures.
4. Improved **System Health** dashboard with a persistent log viewer in Settings that ensures core log tabs are always accessible.
5. Added NVM path auto-detection to ensure `npm` and `node` are always available to the panel even when installed via version managers.
6. Established strict **Project Guidelines** to ensure production-ready standards for future developments.


13.04.2026

1. Script has been updated to 3.0. Major changes has been done to support latest Ubuntu LTS versions and latest webmin versions. Also added support for installing certbot for ssl certificates.
2. Added support for interactive menu to choose options during installation.
3. Improved error handling and logging for better troubleshooting.
4. Auto Installation of MariaDB & phpMyAdmin.
5. updated readme and documentation for better clarity.
6. Added multiple stack options including NGINX and hybrid configurations.
7. Added support for installing CSF firewall.
8. Added support for installing ModSecurity firewall.
9. Added support for installing Fail2Ban.
10. Added support for installing UFW firewall.
11. Added support for installing Nginx.
12. Added support for installing PHP.
13. Added support for installing MariaDB.
14. Added support for installing phpMyAdmin.
15. Added support for installing Certbot.
16. Added support for installing Webmin.
17. Added support for installing FTP.
18. Added support for installing SSH.

24.01.2016

1. Script has been updated to 1.0 with some new changes. Now users will have choice on installing webmin control panel and ssl certificates. Also http and https apache file config has been splited into two files for better management.

27.01.2016

1. Script has been updated to 1.1. Some minor changes has been done and permission checking has been added.

13.11.2025

1. Script has been updated to 2.0. Major changes has been done to support latest Ubuntu LTS versions and latest webmin versions. Also added support for installing certbot for ssl certificates.
2. Added support for interactive menu to choose options during installation.
3. Improved error handling and logging for better troubleshooting.
4. Auto Installation of MariaDB & phpMyAdmin.
5. updated readme and documentation for better clarity.
6. Added multiple stack options including NGINX and hybrid configurations.
