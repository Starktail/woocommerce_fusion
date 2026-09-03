# Copyright (c) 2026, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class WooCommerceSyncQueue(Document):
	@staticmethod
	def clear_old_logs(days=30):
		"""
		Called by Frappe's log cleanup scheduler (Log Settings).
		Only purge terminal rows - Pending and Failed are kept for manual review.
		"""
		frappe.db.delete(
			"WooCommerce Sync Queue",
			{
				"status": ["in", ["Completed", "Skipped", "Superseded"]],
				"modified": ["<", frappe.utils.add_days(None, -days)],
			},
		)


# A scheduled sweep re-queues the same resource every run. When the failure is one no retry
# can fix - order data ERPNext rejects, a product deleted on WooCommerce - that means failing
# forever. Park the resource after this many consecutive failures instead.
MAX_CONSECUTIVE_FAILURES = 5


def _park_reason(
	woocommerce_server: str,
	sync_type: str,
	direction: str,
	triggered_by: str,
	filters: dict,
) -> str | None:
	"""
	Reason to park this enqueue as Skipped, or None to queue it normally.

	Only scheduled sweeps are parked - a Hook or Manual trigger is someone acting deliberately,
	and a manual retry from the Sync Status page puts a Failed row back to Pending, which frees
	the resource again.
	"""
	if triggered_by != "Scheduled":
		return None

	recent = frappe.get_all(
		"WooCommerce Sync Queue",
		filters={
			"woocommerce_server": woocommerce_server,
			"sync_type": sync_type,
			"direction": direction,
			"status": ["in", ("Failed", "Completed")],
			**filters,
		},
		pluck="status",
		order_by="creation desc",
		limit=MAX_CONSECUTIVE_FAILURES,
	)
	if len(recent) < MAX_CONSECUTIVE_FAILURES or any(status != "Failed" for status in recent):
		return None

	return (
		f"Parked after {MAX_CONSECUTIVE_FAILURES} consecutive failures. "
		"Fix the cause, then retry from the WooCommerce Sync Status page."
	)


def _supersede_pending(
	woocommerce_server: str,
	sync_type: str,
	direction: str,
	filters: dict,
) -> None:
	"""
	Mark any existing Pending row matching the given filters as Superseded so a
	fresh row can be inserted, preserving full history.
	"""
	existing_name = frappe.db.get_value(
		"WooCommerce Sync Queue",
		{
			"woocommerce_server": woocommerce_server,
			"sync_type": sync_type,
			"direction": direction,
			"status": "Pending",
			**filters,
		},
		"name",
	)
	if existing_name:
		frappe.db.set_value(
			"WooCommerce Sync Queue",
			existing_name,
			"status",
			"Superseded",
			update_modified=False,
		)


def enqueue_item(
	woocommerce_server: str,
	item_code: str,
	item_woocommerce_server_idx: int,
	sync_type: str = "item",
	resource_type: str = "product",
	parent_woocommerce_id: str | None = None,
	woocommerce_id: str | None = None,
	direction: str = "outbound",
	triggered_by: str = "Hook",
	trigger_reference_doctype: str | None = None,
	trigger_reference_name: str | None = None,
	extra_data: dict | None = None,
) -> str:
	"""
	Add an item/item_price/stock operation to the sync queue. Returns the new queue entry name.

	direction: "outbound" (ERPNext → WC) or "inbound" (WC → ERPNext).

	Deduplicates by (woocommerce_server, reference_name, sync_type, direction): if a Pending
	row already exists for this combination, it is marked "Superseded" before inserting the new
	row. This preserves full history - each enqueue event gets its own row.
	"""
	filters = {"reference_name": item_code}
	_supersede_pending(woocommerce_server, sync_type, direction, filters)
	park_reason = _park_reason(woocommerce_server, sync_type, direction, triggered_by, filters)

	doc = frappe.get_doc(
		{
			"doctype": "WooCommerce Sync Queue",
			"woocommerce_server": woocommerce_server,
			"sync_type": sync_type,
			"direction": direction,
			"wc_resource_type": resource_type,
			"woocommerce_id": woocommerce_id,
			"parent_woocommerce_id": parent_woocommerce_id,
			"reference_doctype": "Item",
			"reference_name": item_code,
			"item_woocommerce_server_idx": item_woocommerce_server_idx,
			"triggered_by": triggered_by,
			"trigger_reference_doctype": trigger_reference_doctype,
			"trigger_reference_name": trigger_reference_name,
			"extra_data": json.dumps(extra_data) if extra_data else None,
			"status": "Skipped" if park_reason else "Pending",
			"error_message": park_reason,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def enqueue_order(
	woocommerce_server: str,
	woocommerce_order_id: str,
	new_status: str | None = None,
	direction: str = "outbound",
	triggered_by: str = "Hook",
) -> str:
	"""
	Queue an order change. direction = "outbound" (ERPNext → WC) or "inbound" (WC → ERPNext).

	Deduplicates by (woocommerce_server, woocommerce_id, sync_type, direction): if a Pending
	row exists for the same direction, mark it Superseded and insert a fresh row so the latest
	status value is always used at flush time. reference_name stores the WC status string for
	outbound; for inbound it is unused.
	"""
	filters = {"woocommerce_id": woocommerce_order_id}
	_supersede_pending(woocommerce_server, "order", direction, filters)
	park_reason = _park_reason(woocommerce_server, "order", direction, triggered_by, filters)

	doc = frappe.get_doc(
		{
			"doctype": "WooCommerce Sync Queue",
			"woocommerce_server": woocommerce_server,
			"sync_type": "order",
			"direction": direction,
			"wc_resource_type": "order",
			"woocommerce_id": woocommerce_order_id,
			"reference_doctype": "WooCommerce Order",
			"reference_name": new_status or "",
			"triggered_by": triggered_by,
			"status": "Skipped" if park_reason else "Pending",
			"error_message": park_reason,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name
