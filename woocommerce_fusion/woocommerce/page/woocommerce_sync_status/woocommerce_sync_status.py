import frappe


@frappe.whitelist()
def get_dashboard_data(
	server_name: str | None = None,
	direction: str | None = None,
	pending_start: int = 0,
	failed_start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Returns all data needed to render the sync status dashboard."""
	pending_start = int(pending_start)
	failed_start = int(failed_start)
	page_length = int(page_length)

	filters = {}
	if server_name:
		filters["woocommerce_server"] = server_name

	# Direction only narrows the pending/failed tables, not the per-server summary counts.
	row_filters = {**filters}
	if direction:
		row_filters["direction"] = direction

	# Queue summary by (status, direction, sync_type) - covers both inbound and outbound
	queue_summary = frappe.db.get_all(
		"WooCommerce Sync Queue",
		filters=filters,
		fields=[
			"status",
			"direction",
			"sync_type",
			"woocommerce_server",
			"count(name) as count",
			"min(creation) as oldest",
		],
		group_by="status, direction, sync_type, woocommerce_server",
		order_by="woocommerce_server, direction, sync_type, status",
	)

	pending_filters = {**row_filters, "status": "Pending"}
	pending_total = frappe.db.count("WooCommerce Sync Queue", pending_filters)
	pending_items = frappe.get_all(
		"WooCommerce Sync Queue",
		filters=pending_filters,
		fields=[
			"name",
			"woocommerce_server",
			"sync_type",
			"direction",
			"wc_resource_type",
			"woocommerce_id",
			"reference_doctype",
			"reference_name",
			"triggered_by",
			"trigger_reference_doctype",
			"trigger_reference_name",
			"creation",
		],
		order_by="creation asc",
		limit_start=pending_start,
		limit_page_length=page_length,
	)

	failed_filters = {**row_filters, "status": "Failed"}
	failed_total = frappe.db.count("WooCommerce Sync Queue", failed_filters)
	failed_items = frappe.get_all(
		"WooCommerce Sync Queue",
		filters=failed_filters,
		fields=[
			"name",
			"woocommerce_server",
			"sync_type",
			"direction",
			"wc_resource_type",
			"woocommerce_id",
			"reference_doctype",
			"reference_name",
			"triggered_by",
			"error_message",
			"batch_log",
			"error_log",
			"creation",
		],
		order_by="modified desc",
		limit_start=failed_start,
		limit_page_length=page_length,
	)

	batch_logs = frappe.get_all(
		"WooCommerce Batch Log",
		filters=filters,
		fields=[
			"name",
			"woocommerce_server",
			"resource_type",
			"status",
			"total_items",
			"successful_items",
			"failed_items",
			"flush_reason",
			"flushed_at",
		],
		order_by="flushed_at desc",
		limit=20,
	)

	servers = frappe.get_all(
		"WooCommerce Server",
		filters={"enable_sync": 1},
		fields=[
			"name",
			"enable_batch_api",
			"batch_flush_interval_minutes",
			"batch_size_limit",
			"woocommerce_server_url",
		],
	)

	return {
		"queue_summary": queue_summary,
		"pending_items": pending_items,
		"pending_total": pending_total,
		"pending_start": pending_start,
		"failed_items": failed_items,
		"failed_total": failed_total,
		"failed_start": failed_start,
		"page_length": page_length,
		"batch_logs": batch_logs,
		"servers": servers,
	}


@frappe.whitelist()
def retry_failed(queue_entry_name: str):
	"""Reset a failed queue entry back to Pending for retry."""
	retry_count = frappe.db.get_value("WooCommerce Sync Queue", queue_entry_name, "retry_count") or 0
	frappe.db.set_value(
		"WooCommerce Sync Queue",
		queue_entry_name,
		{
			"status": "Pending",
			"error_message": "",
			"batch_log": None,
			"error_log": None,
			"retry_count": retry_count + 1,
		},
	)


@frappe.whitelist()
def retry_all_failed(server_name: str | None = None):
	"""Reset all Failed entries for a server back to Pending."""
	filters = {"status": "Failed"}
	if server_name:
		filters["woocommerce_server"] = server_name
	frappe.db.set_value(
		"WooCommerce Sync Queue",
		filters,
		{"status": "Pending", "error_message": "", "batch_log": None, "error_log": None},
	)
