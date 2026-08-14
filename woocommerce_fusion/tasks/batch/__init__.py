import frappe


def get_item_sync_class(server_name: str):
	"""Return the appropriate SynchroniseItem class based on server config."""
	server = frappe.get_cached_doc("WooCommerce Server", server_name)
	if server.enable_batch_api:
		from woocommerce_fusion.tasks.batch.sync_items_batch import SynchroniseItemBatch

		return SynchroniseItemBatch
	from woocommerce_fusion.tasks.sync_items import SynchroniseItem

	return SynchroniseItem


def get_price_sync_class(server_name: str):
	"""Return the appropriate SynchroniseItemPrice class based on server config."""
	server = frappe.get_cached_doc("WooCommerce Server", server_name)
	if server.enable_batch_api:
		from woocommerce_fusion.tasks.batch.sync_item_prices_batch import SynchroniseItemPriceBatch

		return SynchroniseItemPriceBatch
	from woocommerce_fusion.tasks.sync_item_prices import SynchroniseItemPrice

	return SynchroniseItemPrice
