from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
)


def enqueue_stock_update(
	woocommerce_server: str,
	item_code: str,
	woocommerce_id: str,
	stock_quantity: int,
	resource_type: str = "product",
	parent_woocommerce_id: str | None = None,
	triggered_by: str = "Hook",
) -> str:
	"""
	Enqueue a stock-level update for batch processing.

	The computed quantity is stored in extra_data so the BatchProcessor does not need to
	re-query bins at flush time. Stock has no conflict resolution (ERPNext is the source of
	truth), so the pre-computed value is used directly.
	"""
	return enqueue_item(
		woocommerce_server=woocommerce_server,
		item_code=item_code,
		item_woocommerce_server_idx=0,
		sync_type="stock",
		resource_type=resource_type,
		parent_woocommerce_id=parent_woocommerce_id,
		woocommerce_id=str(woocommerce_id),
		direction="outbound",
		triggered_by=triggered_by,
		trigger_reference_doctype="Item",
		trigger_reference_name=item_code,
		extra_data={"stock_quantity": stock_quantity},
	)
