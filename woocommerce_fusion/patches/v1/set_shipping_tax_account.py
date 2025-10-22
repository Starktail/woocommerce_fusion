from __future__ import unicode_literals

import traceback

import frappe
from frappe import _


def execute():
	"""
	Set the newly created 'Account Head for Shipping Tax' field to the existing Tax Account field for all WooCommerce Servers
	"""
	try:
		# Reload doc to ensure that the new field `f_n_f_tax_account` exists
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")

		wc_servers = frappe.get_all("WooCommerce Server")
		for wc_server in wc_servers:
			wc_server_doc = frappe.get_doc("WooCommerce Server", wc_server.name)
			if wc_server_doc.tax_account:
				frappe.db.set_value(
					"WooCommerce Server",
					wc_server.name,
					"f_n_f_tax_account",
					wc_server_doc.tax_account,
					update_modified=False,
				)

	except Exception as err:
		print(_("Failed to set 'Account Head for Shipping Tax' field on WooCommerce Server"))
		print(traceback.format_exception(err))
