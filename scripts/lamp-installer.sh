#!/bin/bash
#
# Enhanced Ubuntu LAMP Stack Installer with Multiple Virtual Hosts Support
# Version 2.0
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/var/lib/lite-cpanel"
CONFIG_FILE="$DATA_DIR/.lamp_config"
PASSWORDS_FILE="$DATA_DIR/.passwords"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Generate strong password
generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-25
}

# Check if LAMP is already installed
is_lamp_installed() {
    [ -f "$CONFIG_FILE" ] && [ -f "$PASSWORDS_FILE" ]
}

# Install LAMP stack
install_lamp_stack() {
    echo -e "${GREEN}Installing LAMP Stack...${NC}"
    
    # Generate passwords
    MYSQL_ROOT_PASSWORD=$(generate_password)
    PHPMYADMIN_PASSWORD=$(generate_password)
    
    # Update system
    apt-get update && apt-get upgrade -y
    
    # Install Apache
    apt-get install apache2 -y
    
    # Install PHP
    apt-get install php php-gd php-common php-curl php-gmp php-mysql php-mongodb libapache2-mod-php -y
    
    # Install MariaDB with automated setup
    export DEBIAN_FRONTEND=noninteractive
    debconf-set-selections <<< "mariadb-server mysql-server/root_password password $MYSQL_ROOT_PASSWORD"
    debconf-set-selections <<< "mariadb-server mysql-server/root_password_again password $MYSQL_ROOT_PASSWORD"
    apt-get install mariadb-server -y
    
    # Secure MariaDB installation
    mysql -u root <<EOF || mysql -u root -p"$MYSQL_ROOT_PASSWORD" <<EOF
ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('$MYSQL_ROOT_PASSWORD');
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

    # Install phpMyAdmin
    debconf-set-selections <<< "phpmyadmin phpmyadmin/dbconfig-install boolean true"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/app-password-confirm password $PHPMYADMIN_PASSWORD"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/admin-pass password $MYSQL_ROOT_PASSWORD"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/mysql/app-pass password $PHPMYADMIN_PASSWORD"
    debconf-set-selections <<< "phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2"
    apt-get install phpmyadmin -y
    
    # Install additional packages
    apt-get install openssl sendmail -y
    
    # Install Certbot via Snap (Official recommendation)
    if ! command -v snap &> /dev/null; then
        apt-get install snapd -y
    fi
    snap install --classic certbot
    ln -sf /snap/bin/certbot /usr/bin/certbot
    
    # Install MongoDB
    if [ -z "$INSTALL_MONGODB" ]; then
        echo -n "Do you want to install MongoDB? (y/n): "
        read user_mongo
        if [ "$user_mongo" = "y" ]; then
            install_mongodb
        fi
    elif [ "$INSTALL_MONGODB" = "y" ]; then
        install_mongodb
    fi

    # Install Node.js
    if [ -z "$INSTALL_NODEJS" ]; then
        echo -n "Do you want to install Node.js via NVM? (y/n): "
        read user_node
        if [ "$user_node" = "y" ]; then
            install_nodejs
        fi
    elif [ "$INSTALL_NODEJS" = "y" ]; then
        install_nodejs
    fi
    
    # Enable Apache modules
    a2enmod rewrite
    a2enmod ssl
    a2dismod autoindex -f
    
    # Save passwords
    cat > "$PASSWORDS_FILE" <<EOF
MySQL Root Password: $MYSQL_ROOT_PASSWORD
phpMyAdmin Password: $PHPMYADMIN_PASSWORD
MySQL SSO User: pma_sso
MySQL SSO Password: $PMA_SSO_PASSWORD
Generated on: $(date)
EOF
    chmod 600 "$PASSWORDS_FILE"
    
    # Mark as installed
    echo "LAMP_INSTALLED=true" > "$CONFIG_FILE"
    echo "INSTALL_DATE=$(date)" >> "$CONFIG_FILE"
    
    echo -e "${GREEN}LAMP Stack installed successfully!${NC}"
    echo -e "${YELLOW}MySQL Root Password: $MYSQL_ROOT_PASSWORD${NC}"
    echo -e "${YELLOW}Passwords saved in: $PASSWORDS_FILE${NC}"
}

# Install MongoDB
install_mongodb() {
    echo -e "${GREEN}Installing MongoDB...${NC}"
    apt-get install gnupg curl -y
    curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | \
        gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.2 multiverse" | tee /etc/apt/sources.list.d/mongodb-org-8.2.list
    apt-get update
    apt-get install -y mongodb-org php-pear php-mongodb
    systemctl enable mongod
    systemctl start mongod
}

# Install Node.js
install_nodejs() {
    echo -e "${GREEN}Installing Node.js via NVM...${NC}"
    export NVM_DIR="$HOME/.nvm"
    
    # Ensure NVM is installed
    if [ ! -s "$NVM_DIR/nvm.sh" ]; then
        curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
    fi
    
    \\. "$NVM_DIR/nvm.sh"
    
    # Ensure Node 24 is installed
    if [[ ! "$(node -v 2>/dev/null)" == v24* ]]; then
        nvm install 24
    fi
    nvm use 24
}

# Create virtual host
create_virtual_host() {
    local domain=$1
    local site_file="/etc/apache2/sites-available/${domain}.conf"
    local doc_root="/var/www/${domain}"
    
    echo -e "${GREEN}Creating virtual host for: $domain${NC}"
    
    # Create document root
    mkdir -p "$doc_root"
    
    # Create virtual host configuration
    cat > "$site_file" <<EOF
<VirtualHost *:80>
    ServerName $domain
    ServerAlias www.$domain
    ServerAdmin admin@$domain
    DocumentRoot $doc_root
    
    <Directory $doc_root>
        Options Indexes FollowSymLinks MultiViews
        AllowOverride All
        Require all granted
    </Directory>
    
    DirectoryIndex index.php index.html
    ErrorLog \${APACHE_LOG_DIR}/${domain}_error.log
    CustomLog \${APACHE_LOG_DIR}/${domain}_access.log combined
</VirtualHost>
EOF
    
    # Create sample index file
    cat > "$doc_root/index.php" <<EOF
<?php
echo "<h1>Welcome to $domain</h1>";
echo "<p>Your virtual host is working!</p>";
echo "<p>PHP Version: " . phpversion() . "</p>";
phpinfo();
?>
EOF
    
    # Set permissions
    chown -R www-data:www-data "$doc_root"
    chmod -R 755 "$doc_root"
    
    # Enable site
    a2ensite "$domain"
    systemctl reload apache2
    
    echo -e "${GREEN}Virtual host created: $domain${NC}"
    echo -e "${YELLOW}Document root: $doc_root${NC}"
}

# Main menu
show_menu() {
    echo -e "\n${GREEN}=== Ubuntu LAMP Installer v2.0 ===${NC}"
    echo "1. Install LAMP Stack (first time)"
    echo "2. Add Virtual Host"
    echo "3. List Virtual Hosts"
    echo "4. Install Webmin"
    echo "5. Setup SSL Certificate"
    echo "6. Show Passwords"
    echo "7. Exit"
    echo -n "Choose option: "
}

# List virtual hosts
list_virtual_hosts() {
    echo -e "\n${GREEN}Active Virtual Hosts:${NC}"
    for site in /etc/apache2/sites-enabled/*.conf; do
        if [ -f "$site" ]; then
            domain=$(basename "$site" .conf)
            echo "- $domain"
        fi
    done
}

# Setup SSL
setup_ssl() {
    echo -n "Enter domain for SSL certificate: "
    read domain
    if [ -n "$domain" ]; then
        certbot --apache -d "$domain" -d "www.$domain"
    fi
}

# Show saved passwords
show_passwords() {
    if [ -f "$PASSWORDS_FILE" ]; then
        echo -e "\n${GREEN}Saved Passwords:${NC}"
        cat "$PASSWORDS_FILE"
    else
        echo -e "${RED}No passwords file found${NC}"
    fi
}

# Install Webmin
install_webmin() {
    echo -e "${GREEN}Installing Webmin...${NC}"
    bash "$SCRIPT_DIR/webmin.sh"
}

# Main execution
main() {
    # Check if running as root
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Please run as root (use sudo)${NC}"
        exit 1
    fi
    
    # If domain provided as argument, handle accordingly
    if [ -n "$1" ]; then
        if ! is_lamp_installed; then
            echo -e "${GREEN}First time installation detected${NC}"
            install_lamp_stack
        fi
        create_virtual_host "$1"
        exit 0
    fi
    
    # Interactive menu
    while true; do
        show_menu
        read choice
        
        case $choice in
            1)
                if is_lamp_installed; then
                    echo -e "${YELLOW}LAMP Stack already installed${NC}"
                else
                    install_lamp_stack
                fi
                ;;
            2)
                if ! is_lamp_installed; then
                    echo -e "${RED}Please install LAMP Stack first${NC}"
                    continue
                fi
                echo -n "Enter domain name: "
                read domain
                if [ -n "$domain" ]; then
                    create_virtual_host "$domain"
                fi
                ;;
            3)
                list_virtual_hosts
                ;;
            4)
                install_webmin
                ;;
            5)
                setup_ssl
                ;;
            6)
                show_passwords
                ;;
            7)
                echo -e "${GREEN}Goodbye!${NC}"
                exit 0
                ;;
            *)
                echo -e "${RED}Invalid option${NC}"
                ;;
        esac
    done
}

main "$@"