#!/bin/bash
#
# Virtual Host Management Script
# Companion script for LAMP installer
#

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Create virtual host with SSL support
create_vhost_with_ssl() {
    local domain=$1
    local doc_root="/var/www/${domain}"
    local site_file="/etc/apache2/sites-available/${domain}.conf"
    
    # Create HTTP virtual host
    cat > "$site_file" <<EOF
<VirtualHost *:80>
    ServerName $domain
    ServerAlias www.$domain
    DocumentRoot $doc_root
    ErrorLog \${APACHE_LOG_DIR}/${domain}_error.log
    CustomLog \${APACHE_LOG_DIR}/${domain}_access.log combined
    Redirect permanent / https://$domain/
</VirtualHost>

<VirtualHost *:443>
    ServerName $domain
    ServerAlias www.$domain
    ServerAdmin admin@$domain
    DocumentRoot $doc_root
    
    SSLEngine on
    SSLCertificateFile /etc/letsencrypt/live/$domain/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/$domain/privkey.pem
    
    <Directory $doc_root>
        Options Indexes FollowSymLinks MultiViews
        AllowOverride All
        Require all granted
    </Directory>
    
    DirectoryIndex index.php index.html
    ErrorLog \${APACHE_LOG_DIR}/${domain}_ssl_error.log
    CustomLog \${APACHE_LOG_DIR}/${domain}_ssl_access.log combined
</VirtualHost>
EOF
    
    echo -e "${GREEN}SSL-enabled virtual host created for: $domain${NC}"
}

# Remove virtual host
remove_vhost() {
    local domain=$1
    
    echo -e "${YELLOW}Removing virtual host: $domain${NC}"
    
    # Disable site
    a2dissite "$domain" 2>/dev/null
    
    # Remove configuration
    rm -f "/etc/apache2/sites-available/${domain}.conf"
    
    # Ask about document root
    echo -n "Remove document root /var/www/$domain? (y/N): "
    read remove_docs
    if [[ "$remove_docs" =~ ^[Yy]$ ]]; then
        rm -rf "/var/www/$domain"
        echo -e "${GREEN}Document root removed${NC}"
    fi
    
    systemctl reload apache2
    echo -e "${GREEN}Virtual host removed: $domain${NC}"
}

# List all virtual hosts with status
list_vhosts_detailed() {
    echo -e "\n${GREEN}=== Virtual Hosts Status ===${NC}"
    
    for conf in /etc/apache2/sites-available/*.conf; do
        if [ -f "$conf" ]; then
            domain=$(basename "$conf" .conf)
            if [ -f "/etc/apache2/sites-enabled/$domain.conf" ]; then
                status="${GREEN}ENABLED${NC}"
            else
                status="${RED}DISABLED${NC}"
            fi
            
            doc_root="/var/www/$domain"
            if [ -d "$doc_root" ]; then
                doc_status="${GREEN}EXISTS${NC}"
            else
                doc_status="${RED}MISSING${NC}"
            fi
            
            echo -e "$domain - Status: $status - DocRoot: $doc_status"
        fi
    done
}

case "$1" in
    "create-ssl")
        if [ -z "$2" ]; then
            echo "Usage: $0 create-ssl <domain>"
            exit 1
        fi
        create_vhost_with_ssl "$2"
        ;;
    "remove")
        if [ -z "$2" ]; then
            echo "Usage: $0 remove <domain>"
            exit 1
        fi
        remove_vhost "$2"
        ;;
    "list")
        list_vhosts_detailed
        ;;
    *)
        echo "Usage: $0 {create-ssl|remove|list} [domain]"
        exit 1
        ;;
esac