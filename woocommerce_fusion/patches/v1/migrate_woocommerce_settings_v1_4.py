import traceback

import frappe
from frappe import _


def execute():
	"""
	Try to get settings from deprecated "WooCommerce Integration Settings" to "WooCommerce Server" doctypes
	"""
	try:
		# Sync fixtures to ensure that the custom fields `woocommerce_server` exist
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")

		# Old settings doctypes
		woocommerce_integration_settings = frappe.get_single("WooCommerce Integration Settings")

		# New settings doctypes
		for wc_server in woocommerce_integration_settings.servers:
			woocommerce_server_doc = frappe.get_doc("WooCommerce Server", wc_server.woocommerce_server)
			new_field_names = [f.fieldname for f in woocommerce_server_doc.meta.fields]

			# Copy fields from "WooCommerce Integration Settings" with the same fieldname
			for field in woocommerce_integration_settings.meta.fields:
				if field.fieldname in new_field_names and field.fieldtype not in (
					"Column Break",
					"Section Break",
					"HTML",
					"Table",
					"Button",
				):
					setattr(
						woocommerce_server_doc,
						field.fieldname,
						getattr(woocommerce_integration_settings, field.fieldname),
					)
					print(_("Copying WooCommerce Settings: {}").format(field.fieldname))

			# Copy fields from "WooCommerce Integration Settings Servers" with the same fieldname
			for field in wc_server.meta.fields:
				if field.fieldname in new_field_names and field.fieldtype not in (
					"Column Break",
					"Section Break",
					"HTML",
					"Table",
					"Button",
				):
					setattr(
						woocommerce_server_doc,
						field.fieldname,
						getattr(wc_server, field.fieldname),
					)
					print(_("Copying WooCommerce Settings: {}").format(field.fieldname))

			woocommerce_server_doc.save()

	except Exception as err:
		print(_("Failed to get settings from deprecated 'Woocommerce Settings' doctypes"))
		print(traceback.format_exception(err))
