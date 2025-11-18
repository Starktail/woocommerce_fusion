from __future__ import unicode_literals

import traceback

import frappe
from frappe import _


def execute():
	"""
	Set default values for new sync direction and order status filter fields on existing WooCommerce Servers
	"""
	try:
		# Reload docs to ensure that the new fields and child table exist
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server Order Status Filter")

		wc_servers = frappe.get_all("WooCommerce Server")
		for wc_server in wc_servers:
			wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server.name)

			# Set default sync_direction to "Bidirectional" if not already set
			if not wc_server_doc.sync_direction:
				wc_server_doc.sync_direction = "Bidirectional"

			# Set default order_status_filter child table rows if not already set
			if not wc_server_doc.order_status_filter or len(wc_server_doc.order_status_filter) == 0:
				# Add default statuses: processing, shipped, completed
				for status in ["processing", "shipped", "completed"]:
					wc_server_doc.append("order_status_filter", {"woocommerce_order_status": status})

			wc_server_doc.save()

	except Exception as err:
		print(_("Failed to set default sync direction and order status filter on WooCommerce Server"))
		print(traceback.format_exception(err))
