import traceback

import frappe
from frappe import _


def execute():
	"""
	Update the Item Map on WooCommerce Server. The woocommerce_field_name now represents a JSONPath expression
	in stead of a fieldname. This patch will update the Item Map to reflect the new JSONPath expression.
	"""
	try:
		wc_server_items = frappe.get_all(
			"WooCommerce Server Item Field", fields=["name", "woocommerce_field_name"]
		)
		for wc_server_item in wc_server_items:
			frappe.db.set_value(
				"WooCommerce Server Item Field",
				wc_server_item.name,
				"woocommerce_field_name",
				"$." + wc_server_item.woocommerce_field_name,
			)

	except Exception as err:
		print(_("Failed to migrate WooCommerce Server Item Map fields to JSONPath"))
		print(traceback.format_exception(err))
