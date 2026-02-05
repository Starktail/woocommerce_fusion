#!/bin/bash

# This is used in pacakge.json to block the help pages from public access, and copy the assets to the correct directory

AUTH_CONTENT="import frappe
from frappe import _

if frappe.session.user=='Guest':
    frappe.throw(_(\"You need to be logged in to access this page\"), frappe.PermissionError)"

for file in woocommerce_fusion/www/woocommerce_fusion_*.html; do
  if [ -f "$file" ]; then
    py_file="woocommerce_fusion/www/$(basename "$file" .html).py"
    echo "$AUTH_CONTENT" > "$py_file"
  fi
done

rm -rf ./woocommerce_fusion/public/chunks
mv ./woocommerce_fusion/www/assets/woocommerce_fusion/chunks ./woocommerce_fusion/public/.
mv ./woocommerce_fusion/www/assets/woocommerce_fusion/*.js ./woocommerce_fusion/public/.
mv ./woocommerce_fusion/www/assets/woocommerce_fusion/*.css ./woocommerce_fusion/public/.