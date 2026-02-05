#!/bin/bash

set -e

cd ~ || exit

sudo apt update
sudo apt remove mysql-server mysql-client
sudo apt install libcups2-dev redis-server mariadb-client libmariadb-dev
# Dependencies for cypress: https://docs.cypress.io/guides/continuous-integration/introduction#UbuntuDebian
sudo apt-get install libgtk2.0-0 libgtk-3-0 libgbm-dev libnotify-dev libgconf-2-4 libnss3 libxss1 libasound2 libxtst6 xauth xvfb

pip install frappe-bench

bench init --skip-assets --python "$(which python)" --frappe-branch "$TEST_AGAINST_FRAPPE_VERSION" ~/frappe-bench          

mkdir ~/frappe-bench/sites/test_site
cp -r "${GITHUB_WORKSPACE}/.github/helper/site_config.json" ~/frappe-bench/sites/test_site/

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE USER 'test_frappe'@'localhost' IDENTIFIED BY 'test_frappe'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE DATABASE test_frappe"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "GRANT ALL PRIVILEGES ON \`test_frappe\`.* TO 'test_frappe'@'localhost'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "FLUSH PRIVILEGES"

install_whktml() {
    wget -O /tmp/wkhtmltox.deb https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-2/wkhtmltox_0.12.6.1-2.jammy_amd64.deb
    sudo apt install /tmp/wkhtmltox.deb

}
install_whktml &

cd ~/frappe-bench || exit

sed -i 's/watch:/# watch:/g' Procfile
sed -i 's/schedule:/# schedule:/g' Procfile
sed -i 's/socketio:/# socketio:/g' Procfile
sed -i 's/redis_socketio:/# redis_socketio:/g' Procfile

# Get dependent apps

bench get-app https://github.com/frappe/erpnext --branch $TEST_AGAINST_ERPNEXT_VERSION --resolve-deps


bench get-app --overwrite woocommerce_fusion "${GITHUB_WORKSPACE}"
bench --verbose setup env --python python3.10
bench --verbose setup requirements --dev

bench start &>> ~/frappe-bench/bench_start.log &
CI=Yes bench build --app frappe &
bench --site test_site reinstall --yes

# Install dependent apps

bench --verbose --site test_site install-app erpnext


bench --verbose --site test_site install-app woocommerce_fusion