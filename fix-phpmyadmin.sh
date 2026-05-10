#!/bin/bash

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

echo "Removing phpMyAdmin Signon features..."

# Remove config overrides
rm -f /etc/phpmyadmin/conf.d/cpanel_signon.php

# Disable and remove Apache basedir override
if [ -f /etc/apache2/conf-available/cpanel-pma-basedir.conf ]; then
    a2disconf cpanel-pma-basedir 2>/dev/null
    rm -f /etc/apache2/conf-available/cpanel-pma-basedir.conf
    systemctl reload apache2 2>/dev/null
fi

# Remove login bridge script
rm -f /usr/share/phpmyadmin/phpmyadmin_login.php

# Remove token directories
rm -rf /var/lib/cpanel_tokens
rm -rf /var/lib/phpmyadmin/tokens

echo "phpMyAdmin has been reverted to default manual login."
