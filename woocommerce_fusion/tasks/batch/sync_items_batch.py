import frappe

from woocommerce_fusion.tasks.sync_items import ERPNextItemToSync, SynchroniseItem
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
)


class SynchroniseItemBatch(SynchroniseItem):
	"""
	Batch-mode subclass. Overrides _send_create and _send_update to enqueue operations
	instead of making immediate API calls. The conflict-resolution logic
	(_build_create_payload, _build_update_payload, sync hash checks) is entirely inherited
	and unchanged. Sync hash bookkeeping is deferred to the BatchProcessor at flush time.
	"""

	defer_sync_hash: bool = True

	def _send_create(self, item: ERPNextItemToSync) -> None:
		enqueue_item(
			woocommerce_server=item.item_woocommerce_server.woocommerce_server,
			item_code=item.item.name,
			item_woocommerce_server_idx=item.item_woocommerce_server_idx,
			resource_type=self._get_resource_type(item),
			parent_woocommerce_id=self._get_parent_woocommerce_id(item),
			woocommerce_id=None,
			triggered_by="Hook",
			trigger_reference_doctype="Item",
			trigger_reference_name=item.item.name,
		)

	def _send_update(self, item: ERPNextItemToSync) -> None:
		enqueue_item(
			woocommerce_server=item.item_woocommerce_server.woocommerce_server,
			item_code=item.item.name,
			item_woocommerce_server_idx=item.item_woocommerce_server_idx,
			resource_type=self._get_resource_type(item),
			parent_woocommerce_id=self._get_parent_woocommerce_id(item),
			woocommerce_id=str(item.item_woocommerce_server.woocommerce_id),
			triggered_by="Hook",
			trigger_reference_doctype="Item",
			trigger_reference_name=item.item.name,
		)

	def _get_resource_type(self, item: ERPNextItemToSync) -> str:
		if item.item.variant_of:
			return "product_variation"
		return "product"

	def _get_parent_woocommerce_id(self, item: ERPNextItemToSync) -> str | None:
		if self._get_resource_type(item) == "product_variation":
			parent_item = frappe.get_doc("Item", item.item.variant_of)
			for server_row in parent_item.woocommerce_servers:
				if server_row.woocommerce_server == item.item_woocommerce_server.woocommerce_server:
					return str(server_row.woocommerce_id) if server_row.woocommerce_id else None
		return None
