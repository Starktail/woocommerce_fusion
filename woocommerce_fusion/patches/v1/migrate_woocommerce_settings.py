import traceback

import frappe
from frappe import _
from frappe.utils.fixtures import sync_fixtures


def execute():
	"""
	Try to get settings from deprecated "Woocommerce Settings" (erpnext) and "WooCommerce Additional Settings" (woocommerce_fusion) doctypes
	"""
	try:
		# Sync fixtures to ensure that the custom fields `woocommerce_server` exist
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Integration Settings")
		sync_fixtures("woocommerce_fusion")

		# Old settings doctypes
		woocommerce_settings = frappe.get_single("Woocommerce Settings")
		woocommerce_additional_settings = frappe.get_single("WooCommerce Additional Settings")

		# New settings doctypes
		woocommerce_integration_settings = frappe.get_single("WooCommerce Integration Settings")

		new_field_names = [f.fieldname for f in woocommerce_integration_settings.meta.fields]

		# Copy fields from "Woocommerce Settings" with the same fieldname
		for field in woocommerce_settings.meta.fields:
			if field.fieldname in new_field_names and field.fieldtype not in (
				"Column Break",
				"Section Break",
				"HTML",
				"Table",
			):
				setattr(
					woocommerce_integration_settings,
					field.fieldname,
					getattr(woocommerce_settings, field.fieldname),
				)
				print(_("Copying WooCommerce Settings: {}").format(field.fieldname))

		# Copy fields from "WooCommerce Additional Settings" with the same fieldname
		for field in woocommerce_additional_settings.meta.fields:
			if field.fieldname in new_field_names and field.fieldtype not in (
				"Column Break",
				"Section Break",
				"HTML",
				"Table",
			):
				setattr(
					woocommerce_integration_settings,
					field.fieldname,
					getattr(woocommerce_additional_settings, field.fieldname),
				)
				print(_("Copying WooCommerce Settings: {}").format(field.fieldname))

		# Copy Child Table records
		for row in woocommerce_additional_settings.servers:
			woocommerce_integration_settings.append(
				"servers",
				{
					"enable_sync": row.enable_sync,
					"wc_plugin_advanced_shipment_tracking": row.wc_plugin_advanced_shipment_tracking,
					"woocommerce_server": row.woocommerce_server,
					"woocommerce_server_url": row.woocommerce_server_url,
					"secret": row.secret,
					"api_consumer_key": row.api_consumer_key,
					"api_consumer_secret": row.api_consumer_secret,
					"wc_ast_shipment_providers": row.wc_ast_shipment_providers,
					"enable_payments_sync": row.enable_payments_sync,
					"payment_method_bank_account_mapping": row.payment_method_bank_account_mapping,
					"payment_method_gl_account_mapping": row.payment_method_gl_account_mapping,
				},
			)

		woocommerce_integration_settings.save()
	except Exception as err:
		print(_("Failed to get settings from deprecated 'Woocommerce Settings' doctypes"))
		print(traceback.format_exception(err))
