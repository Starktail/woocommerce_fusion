import frappe

from woocommerce_fusion.tasks.sync_item_prices import SynchroniseItemPrice
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def enqueue_price_updates(sync: SynchroniseItemPrice) -> None:
	"""
	Enqueue an item_price operation (with the computed price payload stored in extra_data)
	for each Item Price in sync.item_price_list, instead of PUTting directly.
	"""
	for item_price in sync.item_price_list:
		try:
			payload = _build_price_payload(sync, item_price)
			if payload:
				enqueue_item(
					woocommerce_server=sync.wc_server.name,
					item_code=item_price.item_code,
					item_woocommerce_server_idx=0,
					sync_type="item_price",
					resource_type="product",
					woocommerce_id=str(item_price.woocommerce_id),
					direction="outbound",
					triggered_by="Scheduled",
					trigger_reference_doctype="Item Price",
					trigger_reference_name=item_price.name,
					extra_data=payload,
				)
		except Exception:
			frappe.log_error("WooCommerce Batch Price Enqueue Error", frappe.get_traceback())


def _build_price_payload(sync: SynchroniseItemPrice, item_price) -> dict | None:
	"""
	Load the WooCommerce Product, compare prices, and return the dict of changed price
	fields, or None if nothing changed. Avoids enqueuing no-ops.
	"""
	wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
		domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
	)
	wc_product = frappe.get_doc({"doctype": "WooCommerce Product", "name": wc_product_name})
	wc_product.load_from_db()

	payload = {}

	# ── Regular price ────────────────────────────────────────────────────────────
	price_list_rate = (
		sync.item_price_doc.price_list_rate
		if sync.item_price_doc and sync.item_price_doc.price_list == sync.wc_server.price_list
		else item_price.price_list_rate
	)
	if not wc_product.regular_price:
		wc_product.regular_price = 0
	wc_product_regular_price = (
		float(wc_product.regular_price)
		if isinstance(wc_product.regular_price, str)
		else wc_product.regular_price
	)
	if wc_product_regular_price != price_list_rate:
		payload["regular_price"] = str(price_list_rate)

	# ── Sale price ─────────────────────────────────────────────────────────────────
	if sync.wc_server.enable_sales_price_list_sync and sync.wc_server.sales_price_list:
		if sync._apply_sale_price(wc_product, item_price.woocommerce_id):
			payload["sale_price"] = (
				str(wc_product.sale_price)
				if wc_product.sale_price and float(wc_product.sale_price) > 0
				else ""
			)
			payload["date_on_sale_from"] = wc_product.date_on_sale_from
			payload["date_on_sale_to"] = wc_product.date_on_sale_to

	return payload or None


class SynchroniseItemPriceBatch(SynchroniseItemPrice):
	"""
	Batch-mode subclass of SynchroniseItemPrice. Overrides
	sync_items_with_woocommerce_products() to enqueue operations instead of PUTting directly.
	"""

	def sync_items_with_woocommerce_products(self) -> None:
		enqueue_price_updates(self)
