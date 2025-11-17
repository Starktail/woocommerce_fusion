from __future__ import unicode_literals

import traceback

import frappe
from frappe import _


def execute():
	"""
	Set default values for new sync direction and order status filter fields on existing WooCommerce Servers
	"""
	try:
		# Reload doc to ensure that the new fields exist
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")

		wc_servers = frappe.get_all("WooCommerce Server")
		for wc_server in wc_servers:
			# Set default sync_direction to "Bidirectional" if not already set
			frappe.db.set_value(
				"WooCommerce Server",
				wc_server.name,
				"sync_direction",
				"Bidirectional",
				update_modified=False,
			)

			# Set default order_status_filter to ["processing", "shipped", "completed"] if not already set
			frappe.db.set_value(
				"WooCommerce Server",
				wc_server.name,
				"order_status_filter",
				'["processing","shipped","completed"]',
				update_modified=False,
			)

	except Exception as err:
		print(_("Failed to set default sync direction and order status filter on WooCommerce Server"))
		print(traceback.format_exception(err))
