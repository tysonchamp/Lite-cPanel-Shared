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

# Automatically set choice to 3 (Hybrid: NGINX + Apache + PHP-FPM)
export AUTO_STACK_CHOICE=3
export INSTALL_MONGODB="n"

echo ""
echo "======================================"
echo "    Installing Web Stack...           "
echo "======================================"

apt-get update
apt-get upgrade -y

# Execute script normally
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
