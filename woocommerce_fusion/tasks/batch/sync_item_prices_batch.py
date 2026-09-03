import frappe

from woocommerce_fusion.tasks.batch.batch_processor import bulk_get_products
from woocommerce_fusion.tasks.sync import get_variation_parent_woocommerce_id
from woocommerce_fusion.tasks.sync_item_prices import SynchroniseItemPrice
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
)


def enqueue_price_updates(sync: SynchroniseItemPrice) -> None:
	"""
	Enqueue an item_price operation (with the computed price payload stored in extra_data)
	for each Item Price in sync.item_price_list, instead of PUTting directly.
	"""
	wc_products, unavailable_ids = _fetch_woocommerce_products(sync)
	missing_ids = []

	for item_price in sync.item_price_list:
		woocommerce_id = str(item_price.woocommerce_id)
		if woocommerce_id in unavailable_ids:
			# Its group's fetch failed and was already logged
			continue

		wc_product = wc_products.get(woocommerce_id)
		if not wc_product:
			# Nothing came back for this id: the product was deleted on WooCommerce, or it is a
			# variation whose parent has no WooCommerce ID.
			missing_ids.append(woocommerce_id)
			continue

		try:
			payload = _build_price_payload(sync, item_price, wc_product)
			if payload:
				# Variations are flushed against products/{parent}/variations/batch
				is_variation = bool(item_price.variant_of)
				enqueue_item(
					woocommerce_server=sync.wc_server.name,
					item_code=item_price.item_code,
					item_woocommerce_server_idx=0,
					sync_type="item_price",
					resource_type="product_variation" if is_variation else "product",
					woocommerce_id=woocommerce_id,
					direction="outbound",
					triggered_by="Scheduled",
					trigger_reference_doctype="Item Price",
					trigger_reference_name=item_price.name,
					extra_data=payload,
				)
		except Exception:
			frappe.log_error("WooCommerce Batch Price Enqueue Error", frappe.get_traceback())

	if missing_ids:
		frappe.log_error(
			"WooCommerce Batch Price Sync: products not found",
			f"Server: {sync.wc_server.name}\n"
			f"No WooCommerce product came back for these IDs, so they were skipped "
			f"({len(missing_ids)}): {', '.join(missing_ids)}\n\n"
			"Either the product was deleted on WooCommerce, or it is a variation whose parent "
			"has no WooCommerce ID. Clear the WooCommerce ID or untick Enabled on the matching "
			"Item WooCommerce Server row to stop this recurring.",
		)


def _fetch_woocommerce_products(sync: SynchroniseItemPrice) -> tuple[dict, set]:
	"""
	Bulk-read every WooCommerce Product the run needs, in pages of 100, and return
	({woocommerce_id: product}, {ids whose fetch failed}).
	"""
	simple_ids = []
	# Variations are only readable under their parent product's endpoint, so they are fetched
	# one call per parent rather than in with everything else.
	variation_ids_by_parent: dict[str, list] = {}

	for item_price in sync.item_price_list:
		if item_price.variant_of:
			parent_id = get_variation_parent_woocommerce_id(
				item_price.woocommerce_server, item_price.item_code
			)
			if not parent_id:
				continue
			variation_ids_by_parent.setdefault(str(parent_id), []).append(str(item_price.woocommerce_id))
		else:
			simple_ids.append(str(item_price.woocommerce_id))

	products: dict = {}
	unavailable_ids: set = set()

	groups = [(None, simple_ids), *variation_ids_by_parent.items()]
	for parent_id, wc_ids in groups:
		if not wc_ids:
			continue
		try:
			products.update(bulk_get_products(sync.wc_server.name, wc_ids, parent_id=parent_id))
		except Exception:
			# One unreachable group must not cost the rest of the run.
			unavailable_ids.update(wc_ids)
			frappe.log_error("WooCommerce Batch Price Fetch Error", frappe.get_traceback())

	return products, unavailable_ids


def _build_price_payload(sync: SynchroniseItemPrice, item_price, wc_product) -> dict | None:
	"""
	Compare the ERPNext price against the already-fetched WooCommerce Product and return the
	dict of changed price fields, or None if nothing changed. Avoids enqueuing no-ops.
	"""
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
