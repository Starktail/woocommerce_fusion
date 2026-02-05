import traceback

import frappe
from frappe import _


def execute():
	"""
	Set the newly created 'Enable Tax Lines Sync' checkbox field to 1 for all WooCommerce Servers
	"""
	try:
		# Reload doc to ensure that the new field `enable_tax_lines_sync` exists
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")

		wc_servers = frappe.get_all("WooCommerce Server")
		for wc_server in wc_servers:
			frappe.db.set_value(
				"WooCommerce Server", wc_server.name, "enable_tax_lines_sync", 1, update_modified=False
			)

	except Exception as err:
		print(_("Failed to set 'Enable Tax Lines Sync' field on WooCommerce Server"))
		print(traceback.format_exception(err))
