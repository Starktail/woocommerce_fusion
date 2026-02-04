import frappe


def execute():
	"""
	Try to get settings from deprecated "WooCommerce Integration Settings" to "WooCommerce Server" doctypes
	"""
	frappe.delete_doc("DocType", "WooCommerce Additional Settings Servers", ignore_missing=True)
