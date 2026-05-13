import frappe


def execute():
	"""Reload WooCommerce Server, copy legacy Custom Field data to native field, remove Custom Field."""
	if not frappe.db.exists("DocType", "WooCommerce Server"):
		return

	try:
		frappe.reload_doc("woocommerce", "doctype", "WooCommerce Server")
	except Exception:
		frappe.log_error(
			title="WooCommerce Fusion patch: reload WooCommerce Server failed",
			message=frappe.get_traceback(),
		)
		return

	frappe.clear_cache(doctype="WooCommerce Server")
	meta = frappe.get_meta("WooCommerce Server", cached=False)
	if not meta.get_field("item_group_category_sync_root"):
		return

	cf_name = frappe.db.get_value(
		"Custom Field",
		{"dt": "WooCommerce Server", "fieldname": "paraguay_wc_item_group_sync_root"},
		"name",
	)
	if not cf_name:
		return

	if frappe.db.has_column("WooCommerce Server", "paraguay_wc_item_group_sync_root") and frappe.db.has_column(
		"WooCommerce Server", "item_group_category_sync_root"
	):
		frappe.db.sql(
			"""
			UPDATE `tabWooCommerce Server`
			SET `item_group_category_sync_root` = `paraguay_wc_item_group_sync_root`
			WHERE (item_group_category_sync_root IS NULL OR item_group_category_sync_root = '')
			AND (paraguay_wc_item_group_sync_root IS NOT NULL AND paraguay_wc_item_group_sync_root != '')
			"""
		)

	frappe.delete_doc("Custom Field", cf_name, force=True, ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(doctype="WooCommerce Server")
