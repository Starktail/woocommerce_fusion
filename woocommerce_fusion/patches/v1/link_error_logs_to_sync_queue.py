import frappe


def execute():
	"""
	Backfill the new WooCommerce Sync Queue "error_log" field for entries that failed before
	the link was recorded
	"""
	frappe.reload_doc("woocommerce", "doctype", "WooCommerce Sync Queue")

	failed_rows = frappe.get_all(
		"WooCommerce Sync Queue",
		filters={"status": "Failed", "error_log": ("is", "not set")},
		pluck="name",
	)
	if not failed_rows:
		return

	for queue_row_name in failed_rows:
		error_log = frappe.get_all(
			"Error Log",
			filters={
				"method": "WooCommerce Batch Error",
				"error": ("like", f"%Queue row: {queue_row_name}%"),
			},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)
		if error_log:
			frappe.db.set_value(
				"WooCommerce Sync Queue",
				queue_row_name,
				"error_log",
				error_log[0],
				update_modified=False,
			)

	frappe.db.commit()
