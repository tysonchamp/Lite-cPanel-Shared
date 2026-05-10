#!/bin/bash
# Main Installer for Ubuntu Web Stack and cPanel
# This script orchestrates the installation of Web Stack + cPanel + Integrations.

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================"
echo "    System Stack & cPanel Installer   "
echo "======================================"

echo "Choose your preferred web stack:"
echo "1. Apache LAMP Stack"
echo "2. NGINX Stack"
echo "3. NGINX + Apache + PHP-FPM (Hybrid)"
echo -n "Select option (1-3): "
read stack_choice

if [[ ! "$stack_choice" =~ ^[1-3]$ ]]; then
    echo "Invalid choice. Exiting."
    exit 1
fi

echo -n "Do you want to install MongoDB? (y/n): "
read install_mongo

export INSTALL_MONGODB=$install_mongo

echo ""
echo "======================================"
echo "    Installing Web Stack...           "
echo "======================================"

apt-get update
apt-get upgrade -y

# Set auto stack choice and execute script normally to preserve stdin
export AUTO_STACK_CHOICE=$stack_choice
bash "$SCRIPT_DIR/scripts/web-stack-installer.sh"

echo ""
echo "======================================"
echo "    Installing Python cPanel Stack    "
echo "======================================"

bash "$SCRIPT_DIR/scripts/install_cpanel.sh"

echo ""
echo "======================================"
echo "    Installation Complete!            "
echo "======================================"
echo "You can now login to your cPanel at http://<your-server-ip>:2083"
