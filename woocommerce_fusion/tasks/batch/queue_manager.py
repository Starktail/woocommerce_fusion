from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from woocommerce_fusion.tasks.batch.batch_processor import BatchProcessor


def should_flush(server_name: str) -> tuple[bool, str]:
	"""
	Returns (should_flush, reason). Flushes if:
	  - pending count >= batch_size_limit, OR
	  - oldest pending entry is older than flush_interval_minutes
	"""
	server = frappe.get_cached_doc("WooCommerce Server", server_name)
	batch_size_limit = server.batch_size_limit or 100
	flush_interval = server.batch_flush_interval_minutes or 1

	pending_count = frappe.db.count(
		"WooCommerce Sync Queue",
		{"woocommerce_server": server_name, "status": "Pending"},
	)

	if pending_count >= batch_size_limit:
		return True, "buffer_full"

	if pending_count > 0:
		oldest_creation = frappe.db.get_value(
			"WooCommerce Sync Queue",
			{"woocommerce_server": server_name, "status": "Pending"},
			"creation",
			order_by="creation asc",
		)
		if oldest_creation:
			age = now_datetime() - get_datetime(oldest_creation)
			if age >= timedelta(minutes=flush_interval):
				return True, "timer_elapsed"

	return False, ""


def flush_pending(server_name: str, reason: str = "manual") -> dict:
	"""
	Flush all Pending queue entries for server_name as one or more batch calls.
	"""
	pending_rows = frappe.db.get_all(
		"WooCommerce Sync Queue",
		filters={"woocommerce_server": server_name, "status": "Pending"},
		fields=[
			"name",
			"sync_type",
			"direction",
			"wc_resource_type",
			"parent_woocommerce_id",
			"woocommerce_id",
			"reference_doctype",
			"reference_name",
			"item_woocommerce_server_idx",
			"triggered_by",
			"extra_data",
		],
		order_by="creation asc",
	)

	if not pending_rows:
		return {"flushed": 0, "success": 0, "failed": 0}

	# Lock: mark all current Pending rows as Processing
	names = [r.name for r in pending_rows]
	frappe.db.set_value(
		"WooCommerce Sync Queue",
		{"name": ["in", names]},
		"status",
		"Processing",
		update_modified=False,
	)
	if not frappe.flags.in_test:
		frappe.db.commit()

	# Group by (wc_resource_type, parent_woocommerce_id, sync_type, direction)
	groups: dict[tuple, list] = {}
	for row in pending_rows:
		key = (row.wc_resource_type, row.parent_woocommerce_id or "", row.sync_type, row.direction)
		groups.setdefault(key, []).append(row)

	processor = BatchProcessor(server_name=server_name)
	server = frappe.get_cached_doc("WooCommerce Server", server_name)
	chunk_size = server.batch_size_limit or 100

	totals = {"success": 0, "failed": 0}

	def run_chunks(resource_type: str, parent_id: str | None, rows: list) -> None:
		for chunk_start in range(0, len(rows), chunk_size):
			chunk = rows[chunk_start : chunk_start + chunk_size]
			success, failed = processor.process_chunk(
				rows=chunk,
				resource_type=resource_type,
				parent_id=parent_id or None,
				flush_reason=reason,
			)
			totals["success"] += success
			totals["failed"] += failed

	# Process parents (and everything that is not a variation) first, so that a parent product
	# created in this same flush has a WooCommerce ID before its variations are sent.
	variation_rows: list = []
	for (resource_type, parent_id, _sync_type, _direction), rows in groups.items():
		if resource_type == "product_variation":
			variation_rows.extend(rows)
			continue
		run_chunks(resource_type, parent_id, rows)

	# Variations: re-resolve each row's parent WooCommerce ID (the parent may have just been
	# created above), then group by the resolved parent for the /products/{parent}/variations/batch
	# endpoint.
	if variation_rows:
		variation_groups: dict[str, list] = {}
		for row in variation_rows:
			parent_wc_id = row.parent_woocommerce_id
			if not parent_wc_id and row.reference_name:
				parent_wc_id = _resolve_variation_parent_id(server_name, row.reference_name)
			row.parent_woocommerce_id = parent_wc_id
			variation_groups.setdefault(parent_wc_id or "", []).append(row)
		for parent_wc_id, rows in variation_groups.items():
			run_chunks("product_variation", parent_wc_id or None, rows)

	return {
		"flushed": totals["success"] + totals["failed"],
		"success": totals["success"],
		"failed": totals["failed"],
	}


def _resolve_variation_parent_id(server_name: str, variant_item_code: str) -> str | None:
	"""
	Resolve the WooCommerce ID of a variant item's parent (template) for the given server.
	Used at flush time because the parent may have been created earlier in the same flush.
	"""
	variant_of = frappe.db.get_value("Item", variant_item_code, "variant_of")
	if not variant_of:
		return None
	return frappe.db.get_value(
		"Item WooCommerce Server",
		{"parent": variant_of, "woocommerce_server": server_name},
		"woocommerce_id",
	)


@frappe.whitelist()
def check_and_flush_all_servers():
	"""
	Called by the scheduler every minute. Flushes servers whose queue is ready.
	"""
	servers = frappe.get_all(
		"WooCommerce Server",
		filters={"enable_sync": 1, "enable_batch_api": 1},
		pluck="name",
	)
	for server_name in servers:
		ready, reason = should_flush(server_name)
		if ready:
			frappe.enqueue(
				"woocommerce_fusion.tasks.batch.queue_manager.flush_pending",
				server_name=server_name,
				reason=reason,
				queue="long",
			)


@frappe.whitelist()
def manual_flush(server_name: str):
	"""Whitelisted for the UI 'Flush Now' button."""
	return flush_pending(server_name, reason="manual")
