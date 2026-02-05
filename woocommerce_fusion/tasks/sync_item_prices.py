from time import sleep

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def update_item_price_for_woocommerce_item_from_hook(doc, method):
	if not frappe.flags.in_test:
		if doc.doctype == "Item Price":
			frappe.enqueue(
				"woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
				enqueue_after_commit=True,
				item_code=doc.item_code,
				item_price_doc=doc,
			)


@frappe.whitelist()
def run_item_price_sync_in_background():
	frappe.enqueue(run_item_price_sync, queue="long", timeout=3600)


@frappe.whitelist()
def run_item_price_sync(item_code: str | None = None, item_price_doc: ItemPrice | None = None):
	sync = SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc)
	sync.run()
	return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of ERPNext Items with WooCommerce Products
	"""

	item_code: str | None
	item_price_list: list

	def __init__(
		self,
		servers: list[WooCommerceServer | frappe._dict] | None = None,
		item_code: str | None = None,
		item_price_doc: ItemPrice | None = None,
	) -> None:
		super().__init__(servers)
		self.item_code = item_code
		self.item_price_doc = item_price_doc
		self.wc_server = None
		self.item_price_list = []

	def run(self) -> None:
		"""
		Run synchornisation
		"""
		for server in self.servers:
			self.wc_server = server
			self.get_erpnext_item_prices()
			self.sync_items_with_woocommerce_products()

	def get_erpnext_item_prices(self) -> None:
		"""
		Get list of ERPNext Item Prices to synchronise,
		"""
		self.item_price_list = []
		if self.wc_server.enable_sync and self.wc_server.enable_price_list_sync and self.wc_server.price_list:
			ip = qb.DocType("Item Price")
			iwc = qb.DocType("Item WooCommerce Server")
			item = qb.DocType("Item")
			and_conditions = []
			and_conditions.append(ip.price_list == self.wc_server.price_list)
			and_conditions.append(iwc.woocommerce_server == self.wc_server.name)
			and_conditions.append(item.disabled == 0)
			and_conditions.append(iwc.woocommerce_id.isnotnull())
			and_conditions.append(iwc.enabled == 1)
			if self.item_code:
				and_conditions.append(ip.item_code == self.item_code)

			self.item_price_list = (
				qb.from_(ip)
				.inner_join(iwc)
				.on(iwc.parent == ip.item_code)
				.inner_join(item)
				.on(item.name == ip.item_code)
				.select(ip.name, ip.item_code, ip.price_list_rate, iwc.woocommerce_server, iwc.woocommerce_id)
				.where(Criterion.all(and_conditions))
				.run(as_dict=True)
			)

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Synchronise Item Prices with WooCommerce Products
		"""
		for item_price in self.item_price_list:
			# Get the WooCommerce Product doc
			wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
				domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
			)
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product", "name": wc_product_name})

			try:
				wc_product.load_from_db()

				# If self.item_price_doc is set, set the price_list_rate accordingly, else use the price_list_rate from the price list
				price_list_rate = (
					self.item_price_doc.price_list_rate
					if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.price_list
					else item_price.price_list_rate
				)
				# Handle blank string for regular_price
				if not wc_product.regular_price:
					wc_product.regular_price = 0
				# When the price is set, the WooCommerce API returns a string value, when the price is not set, it returns a float value of 0.0
				wc_product_regular_price = (
					float(wc_product.regular_price)
					if isinstance(wc_product.regular_price, str)
					else wc_product.regular_price
				)
				if wc_product_regular_price != price_list_rate:
					wc_product.regular_price = price_list_rate
					wc_product.save()
			except Exception:
				error_message = f"{frappe.get_traceback()}\n\n Product Data: \n{wc_product.as_dict()}"
				frappe.log_error("WooCommerce Error: Price List Sync", error_message)

			sleep(self.wc_server.price_list_delay_per_item)
