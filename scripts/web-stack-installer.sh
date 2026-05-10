#!/bin/bash
#
# Enhanced Web Stack Installer with Apache/NGINX/Hybrid Support
# Version 2.0
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/var/lib/lite-cpanel"
CONFIG_FILE="$DATA_DIR/.stack_config"
PASSWORDS_FILE="$DATA_DIR/.passwords"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-25
}

is_stack_installed() {
    [ -f "$CONFIG_FILE" ] && [ -f "$PASSWORDS_FILE" ]
}

get_webserver_type() {
    [ -f "$CONFIG_FILE" ] && grep "WEBSERVER_TYPE" "$CONFIG_FILE" | cut -d'=' -f2 || echo "none"
}

get_php_version() {
    php -r "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;" 2>/dev/null || echo "8.1"
}

configure_nginx_phpmyadmin() {
    # Add phpMyAdmin configuration to NGINX default site
    cat >> /etc/nginx/sites-available/default <<EOF

    location /phpmyadmin {
        root /usr/share/;
        index index.php index.html index.htm;
        location ~ ^/phpmyadmin/(.+\.php)$ {
            try_files \$uri =404;
            root /usr/share/;
            fastcgi_pass unix:/var/run/php/php$(get_php_version)-fpm.sock;
            fastcgi_index index.php;
            fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
            include fastcgi_params;
        }
        location ~* ^/phpmyadmin/(.+\.(jpg|jpeg|gif|css|png|js|ico|html|xml|txt))$ {
            root /usr/share/;
        }
    }
EOF
}

install_database() {
    MYSQL_ROOT_PASSWORD=$(generate_password)
    PHPMYADMIN_PASSWORD=$(generate_password)
    
    export DEBIAN_FRONTEND=noninteractive
    debconf-set-selections <<< "mariadb-server mysql-server/root_password password $MYSQL_ROOT_PASSWORD"
    debconf-set-selections <<< "mariadb-server mysql-server/root_password_again password $MYSQL_ROOT_PASSWORD"
    apt-get install mariadb-server -y
    
    mysql -u root <<EOF || mysql -u root -p"$MYSQL_ROOT_PASSWORD" <<EOF
ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('$MYSQL_ROOT_PASSWORD');

CREATE USER IF NOT EXISTS 'admin'@'localhost' IDENTIFIED BY '$PHPMYADMIN_PASSWORD';
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost' WITH GRANT OPTION;

DELETE FROM mysql.user WHERE User='';
DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DROP DATABASE IF EXISTS test;
DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
FLUSH PRIVILEGES;
EOF

    # Create dedicated phpMyAdmin SSO user
    PMA_SSO_PASSWORD=$(generate_password)
    mysql -u root -p"$MYSQL_ROOT_PASSWORD" <<EOF
CREATE USER IF NOT EXISTS 'pma_sso'@'localhost' IDENTIFIED BY '$PMA_SSO_PASSWORD';
GRANT ALL PRIVILEGES ON *.* TO 'pma_sso'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
EOF

    if [ -z "$INSTALL_MONGODB" ]; then
        echo -n "Do you want to install MongoDB? (y/n): "
        read user_mongo
        if [ "$user_mongo" = "y" ]; then
            install_mongodb
        fi
    elif [ "$INSTALL_MONGODB" = "y" ]; then
        install_mongodb
    fi
    
    cat > "$PASSWORDS_FILE" <<EOF
MySQL Root Password: $MYSQL_ROOT_PASSWORD
phpMyAdmin Password: $PHPMYADMIN_PASSWORD
MySQL SSO User: pma_sso
MySQL SSO Password: $PMA_SSO_PASSWORD
Generated: $(date)
EOF
    chmod 600 "$PASSWORDS_FILE"
    
    echo -e "${GREEN}Database setup complete!${NC}"
    echo -e "${YELLOW}MySQL Root Password: $MYSQL_ROOT_PASSWORD${NC}"
}

install_mongodb() {
    apt-get install gnupg curl -y
    curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.2 multiverse" | tee /etc/apt/sources.list.d/mongodb-org-8.2.list
    apt-get update && apt-get install -y mongodb-org php-pear php-mongodb
    systemctl enable --now mongod
}

install_apache_stack() {
    echo -e "${GREEN}Installing Apache LAMP Stack...${NC}"
    
    apt-get update && apt-get upgrade -y
    apt-get install apache2 php php-gd php-common php-curl php-gmp php-mysql php-mongodb libapache2-mod-php openssl sendmail -y
    
    # Install Certbot via Snap
    if ! command -v snap &> /dev/null; then apt-get install snapd -y; fi
    snap install --classic certbot
    ln -sf /snap/bin/certbot /usr/bin/certbot
    
    install_database
    
    debconf-set-selections <<< "phpmyadmin phpmyadmin/dbconfig-install boolean true"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/app-password-confirm password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/admin-pass password $(grep 'MySQL Root Password:' "$PASSWORDS_FILE" | cut -d' ' -f4)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/app-pass password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2"
    apt-get install phpmyadmin -y
    
    a2enmod rewrite ssl
    a2dismod autoindex -f
    
    echo "WEBSERVER_TYPE=apache" > "$CONFIG_FILE"
    echo "STACK_INSTALLED=true" >> "$CONFIG_FILE"
    echo "INSTALL_DATE=$(date)" >> "$CONFIG_FILE"
    
    echo -e "${GREEN}Apache LAMP Stack installed!${NC}"
}

install_nginx_stack() {
    echo -e "${GREEN}Installing NGINX Stack...${NC}"
    
    apt-get update && apt-get upgrade -y
    apt-get install nginx php-fpm php-gd php-common php-curl php-gmp php-mysql php-mongodb openssl sendmail -y
    
    # Install Certbot via Snap
    if ! command -v snap &> /dev/null; then apt-get install snapd -y; fi
    snap install --classic certbot
    ln -sf /snap/bin/certbot /usr/bin/certbot
    
    install_database
    
    # Configure phpMyAdmin for NGINX
    debconf-set-selections <<< "phpmyadmin phpmyadmin/dbconfig-install boolean true"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/app-password-confirm password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/admin-pass password $(grep 'MySQL Root Password:' "$PASSWORDS_FILE" | cut -d' ' -f4)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/app-pass password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/reconfigure-webserver multiselect "
    apt-get install phpmyadmin -y
    
    # Configure NGINX for phpMyAdmin
    configure_nginx_phpmyadmin
    
    # Create security snippets
    mkdir -p /etc/nginx/snippets
    echo 'location ~ /\.(?!well-known) {
    deny all;
    access_log off;
    log_not_found off;
}' > /etc/nginx/snippets/block-dotfiles.conf
    
    PHP_VERSION=$(get_php_version)
    systemctl enable nginx php${PHP_VERSION}-fpm
    systemctl start nginx php${PHP_VERSION}-fpm
    
    echo "WEBSERVER_TYPE=nginx" > "$CONFIG_FILE"
    echo "STACK_INSTALLED=true" >> "$CONFIG_FILE"
    echo "INSTALL_DATE=$(date)" >> "$CONFIG_FILE"
    
    echo -e "${GREEN}NGINX Stack installed!${NC}"
}

install_hybrid_stack() {
    echo -e "${GREEN}Installing NGINX + Apache + PHP-FPM Stack...${NC}"
    
    apt-get update && apt-get upgrade -y
    apt-get install nginx apache2 php-fpm php-gd php-common php-curl php-gmp php-mysql php-mongodb openssl sendmail -y
    
    # Install Certbot via Snap
    if ! command -v snap &> /dev/null; then apt-get install snapd -y; fi
    snap install --classic certbot
    ln -sf /snap/bin/certbot /usr/bin/certbot
    
    install_database
    
    sed -i 's/Listen 80/Listen 8080/' /etc/apache2/ports.conf
    sed -i 's/:80>/:8080>/' /etc/apache2/sites-available/000-default.conf
    
    cat > /etc/nginx/sites-available/default <<EOF
server {
    listen 80 default_server;
    server_name _;
    
    location /phpmyadmin {
        proxy_pass http://127.0.0.1:8080/phpmyadmin;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
    
    location ~ \.php$ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
    
    location / {
        try_files \$uri \$uri/ @apache;
    }
    
    location @apache {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
EOF
    
    # Create security snippets
    mkdir -p /etc/nginx/snippets
    echo 'location ~ /\.(?!well-known) {
    deny all;
    access_log off;
    log_not_found off;
}' > /etc/nginx/snippets/block-dotfiles.conf

    # Configure phpMyAdmin for Apache (backend)
    debconf-set-selections <<< "phpmyadmin phpmyadmin/dbconfig-install boolean true"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/app-password-confirm password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/admin-pass password $(grep 'MySQL Root Password:' "$PASSWORDS_FILE" | cut -d' ' -f4)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/app-pass password $(grep 'phpMyAdmin Password:' "$PASSWORDS_FILE" | cut -d' ' -f3)"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2"
    apt-get install phpmyadmin -y
    
    PHP_VERSION=$(get_php_version)
    # 2. Enable proxy_fcgi and setenvif so Apache can pass requests to FPM
    a2enmod proxy_fcgi setenvif
    a2enmod rewrite
    a2dismod autoindex -f
    # 3. Enable the specific FPM configuration for Apache
    # This sets the SetHandler "proxy:unix:..." directive
    a2enconf php${PHP_VERSION}-fpm
    
    systemctl enable nginx apache2 php${PHP_VERSION}-fpm
    systemctl restart apache2 nginx php${PHP_VERSION}-fpm
    
    echo "WEBSERVER_TYPE=hybrid" > "$CONFIG_FILE"
    echo "STACK_INSTALLED=true" >> "$CONFIG_FILE"
    echo "INSTALL_DATE=$(date)" >> "$CONFIG_FILE"
    
    echo -e "${GREEN}Hybrid Stack installed!${NC}"
}

create_virtual_host() {
    local domain=$1
    local doc_root=${2:-"/var/www/${domain}"}
    local webserver=$(get_webserver_type)
    
    mkdir -p "$doc_root"
    
    cat > "$doc_root/index.php" <<EOF
<?php
echo "<h1>Welcome to $domain</h1>";
echo "<p>Virtual host active - PHP " . phpversion() . "</p>";
?>
EOF
    
    chown -R www-data:www-data "$doc_root"
    
    case $webserver in
        "apache")
            create_apache_vhost "$domain" "$doc_root"
            ;;
        "nginx")
            create_nginx_vhost "$domain" "$doc_root"
            ;;
        "hybrid")
            create_hybrid_vhost "$domain" "$doc_root"
            ;;
    esac
    
    echo -e "${GREEN}Created: $domain${NC}"
}

create_apache_vhost() {
    local domain=$1
    local doc_root=$2
    local site_file="/etc/apache2/sites-available/${domain}.conf"
    
    cat > "$site_file" <<EOF
<VirtualHost *:80>
    ServerName $domain
    ServerAlias www.$domain
    DocumentRoot $doc_root
    <Directory $doc_root>
        AllowOverride All
        Require all granted
    </Directory>
    ErrorLog \${APACHE_LOG_DIR}/${domain}_error.log
    CustomLog \${APACHE_LOG_DIR}/${domain}_access.log combined
</VirtualHost>
EOF
    
    a2ensite "$domain"
    systemctl reload apache2
}

create_nginx_vhost() {
    local domain=$1
    local doc_root=$2
    local site_file="/etc/nginx/sites-available/${domain}"
    
    cat > "$site_file" <<EOF
server {
    listen 80;
    server_name $domain www.$domain;
    root $doc_root;
    index index.php index.html;

    access_log /var/log/nginx/${domain}_access.log;
    error_log  /var/log/nginx/${domain}_error.log;
    
    location ~ \.php$ {
        fastcgi_pass unix:/var/run/php/php$(get_php_version)-fpm.sock;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        include fastcgi_params;
    }
    
    location / {
        try_files \$uri \$uri/ =404;
    }
}
EOF
    
    ln -sf "$site_file" "/etc/nginx/sites-enabled/"
    systemctl reload nginx
}

create_hybrid_vhost() {
    local domain=$1
    local doc_root=$2
    
    cat > "/etc/apache2/sites-available/${domain}.conf" <<EOF
<VirtualHost *:8080>
    ServerName $domain
    ServerAlias www.$domain
    DocumentRoot $doc_root
    <Directory $doc_root>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>
    ErrorLog \${APACHE_LOG_DIR}/${domain}_error.log
    CustomLog \${APACHE_LOG_DIR}/${domain}_access.log combined
</VirtualHost>
EOF
    
    cat > "/etc/nginx/sites-available/${domain}" <<EOF
server {
    listen 80;
    server_name $domain www.$domain;

    access_log /var/log/nginx/${domain}_access.log;
    error_log  /var/log/nginx/${domain}_error.log;
    
    # Block .env and other hidden files
    include snippets/block-dotfiles.conf;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
    
    a2ensite "$domain"
    ln -sf "/etc/nginx/sites-available/${domain}" "/etc/nginx/sites-enabled/"
    systemctl reload apache2 nginx
}

list_virtual_hosts() {
    local webserver=$(get_webserver_type)
    echo -e "\n${GREEN}Virtual Hosts ($webserver):${NC}"
    
    case $webserver in
        "apache")
            for site in /etc/apache2/sites-enabled/*.conf; do
                [ -f "$site" ] && echo "- $(basename "$site" .conf)"
            done
            ;;
        "nginx")
            for site in /etc/nginx/sites-enabled/*; do
                [ -f "$site" ] && [ "$(basename "$site")" != "default" ] && echo "- $(basename "$site")"
            done
            ;;
        "hybrid")
            for site in /etc/nginx/sites-enabled/*; do
                [ -f "$site" ] && [ "$(basename "$site")" != "default" ] && echo "- $(basename "$site")"
            done
            ;;
    esac
}

setup_ssl() {
    local webserver=$(get_webserver_type)
    echo -n "Enter domain for SSL certificate: "
    read domain
    if [ -n "$domain" ]; then
        case $webserver in
            "apache") certbot --apache -d "$domain" -d "www.$domain" ;;
            "nginx"|"hybrid") certbot --nginx -d "$domain" -d "www.$domain" ;;
        esac
    fi
}

show_passwords() {
    if [ -f "$PASSWORDS_FILE" ]; then
        echo -e "\n${GREEN}Saved Passwords:${NC}"
        cat "$PASSWORDS_FILE"
    else
        echo -e "${RED}No passwords file found${NC}"
    fi
}

install_webmin() {
    echo -e "${GREEN}Installing Webmin...${NC}"
    bash "$SCRIPT_DIR/webmin.sh"
}

manage_cron_jobs() {
    while true; do
        echo -e "\n${GREEN}=== Cron Job Management ===${NC}"
        echo "1. List Cron Jobs"
        echo "2. Add Cron Job"
        echo "3. Backup Cron Jobs"
        echo "4. Edit Cron Jobs (Manual)"
        echo "5. Back to Main Menu"
        echo -n "Choose option: "
        read cron_choice
        
        case $cron_choice in
            1)
                echo -e "\n${GREEN}Current Cron Jobs:${NC}"
                crontab -l 2>/dev/null || echo "No cron jobs found."
                ;;
            2)
                echo -e "\n${GREEN}Add New Cron Job${NC}"
                echo "Enter schedule (e.g., '* * * * *'):"
                read schedule
                echo "Enter command to run:"
                read command
                
                if [ -n "$schedule" ] && [ -n "$command" ]; then
                    (crontab -l 2>/dev/null; echo "$schedule $command") | crontab -
                    echo -e "${GREEN}Cron job added!${NC}"
                else
                    echo -e "${RED}Invalid input. Operation cancelled.${NC}"
                fi
                ;;
            3)
                BACKUP_DIR="$SCRIPT_DIR/backups"
                mkdir -p "$BACKUP_DIR"
                BACKUP_FILE="$BACKUP_DIR/crontab_backup_$(date +%Y%m%d_%H%M%S).txt"
                if crontab -l > "$BACKUP_FILE" 2>/dev/null; then
                    echo -e "${GREEN}Backup saved to: $BACKUP_FILE${NC}"
                else
                    echo -e "${RED}Failed to backup (no cron jobs?)${NC}"
                fi
                ;;
            4)
                crontab -e
                ;;
            5)
                return
                ;;
            *)
                echo -e "${RED}Invalid option${NC}"
                ;;
        esac
    done
}

show_menu() {
    echo -e "\n${GREEN}=== Web Stack Installer v2.0 ===${NC}"
    if ! is_stack_installed; then
        echo "1. Install Apache LAMP Stack"
        echo "2. Install NGINX Stack"
        echo "3. Install NGINX + Apache + PHP-FPM"
        echo "4. Exit"
    else
        echo "1. Add Virtual Host"
        echo "2. List Virtual Hosts"
        echo "3. Install Webmin"
        echo "4. Setup SSL Certificate"
        echo "5. Show Passwords"
        echo "6. Manage Cron Jobs"
        echo "7. Exit"
    fi
    echo -n "Choose option: "
}

main() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Please run as root (use sudo)${NC}"
        exit 1
    fi
    
    if [ -n "$1" ]; then
        if ! is_stack_installed; then
            echo -e "${GREEN}First time installation - choose stack type:${NC}"
            echo "1. Apache LAMP"
            echo "2. NGINX"
            echo "3. NGINX + Apache + PHP-FPM"
            echo -n "Choose: "
            read stack_choice
            case $stack_choice in
                1) install_apache_stack ;;
                2) install_nginx_stack ;;
                3) install_hybrid_stack ;;
                *) echo -e "${RED}Invalid choice${NC}"; exit 1 ;;
            esac
        fi
        create_virtual_host "$1" "$2"
        exit 0
    fi
    
    while true; do
        if [ -n "$AUTO_STACK_CHOICE" ] && ! is_stack_installed; then
            choice="$AUTO_STACK_CHOICE"
        else
            show_menu
            read choice
        fi
        
        if ! is_stack_installed; then
            case $choice in
                1) install_apache_stack ;;
                2) install_nginx_stack ;;
                3) install_hybrid_stack ;;
                4) echo -e "${GREEN}Goodbye!${NC}"; exit 0 ;;
                *) echo -e "${RED}Invalid option${NC}" ;;
            esac
            
            # If auto-installing, exit immediately after the stack is installed
            if [ -n "$AUTO_STACK_CHOICE" ]; then
                exit 0
            fi
        else
            case $choice in
                1)
                    echo -n "Enter domain name: "
                    read domain
                    [ -n "$domain" ] && create_virtual_host "$domain"
                    ;;
                2) list_virtual_hosts ;;
                3) install_webmin ;;
                4) setup_ssl ;;
                5) show_passwords ;;
                6) manage_cron_jobs ;;
                7) echo -e "${GREEN}Goodbye!${NC}"; exit 0 ;;
                *) echo -e "${RED}Invalid option${NC}" ;;
            esac
        fi
    done
}

main "$@"
