from time import sleep

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion
from frappe.query_builder.functions import IfNull
from frappe.utils import get_datetime

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce, get_variation_parent_woocommerce_id
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def _format_sale_date(date_value) -> str | None:
	"""
	Convert an ERPNext Date value (string or date object) to an ISO 8601 datetime string
	expected by the WooCommerce REST API, or None to clear the field.

	ERPNext Item Price valid_from / valid_upto are Date fields, e.g. "2024-06-01".
	WooCommerce accepts "2024-06-01T00:00:00".
	"""
	if not date_value:
		return None
	return get_datetime(date_value).strftime("%Y-%m-%dT%H:%M:%S")


def item_wide_price_conditions(ip) -> list:
	"""
	Conditions restricting an Item Price query to rows that apply to the whole item.
	"""
	return [
		IfNull(ip.batch_no, "") == "",
		IfNull(ip.customer, "") == "",
		IfNull(ip.supplier, "") == "",
	]


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
	Class for managing synchronisation of ERPNext Item Prices with WooCommerce Products
	"""

	item_code: str | None
	item_price_list: list
	sale_price_map: dict

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
		self.sale_price_map = {}

	def run(self) -> None:
		for server in self.servers:
			self.wc_server = server
			self.get_erpnext_item_prices()
			self.get_erpnext_sale_prices()
			if server.enable_batch_api:
				from woocommerce_fusion.tasks.batch.sync_item_prices_batch import enqueue_price_updates

				enqueue_price_updates(self)
			else:
				self.sync_items_with_woocommerce_products()

	def get_erpnext_item_prices(self) -> None:
		"""
		Get list of ERPNext Item Prices to synchronise.
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
			and_conditions.extend(item_wide_price_conditions(ip))
			if self.item_code:
				and_conditions.append(ip.item_code == self.item_code)

			self.item_price_list = (
				qb.from_(ip)
				.inner_join(iwc)
				.on(iwc.parent == ip.item_code)
				.inner_join(item)
				.on(item.name == ip.item_code)
				.select(
					ip.name,
					ip.item_code,
					ip.price_list_rate,
					iwc.woocommerce_server,
					iwc.woocommerce_id,
					item.variant_of,
				)
				.where(Criterion.all(and_conditions))
				.run(as_dict=True)
			)

	def get_erpnext_sale_prices(self) -> None:
		"""
		Build a lookup map of woocommerce_id → sale Item Price record using the
		server's configured Sales Price List.
		"""
		self.sale_price_map = {}
		if not (
			self.wc_server.enable_sync
			and self.wc_server.enable_price_list_sync
			and self.wc_server.enable_sales_price_list_sync
			and self.wc_server.sales_price_list
		):
			return

		ip = qb.DocType("Item Price")
		iwc = qb.DocType("Item WooCommerce Server")
		item = qb.DocType("Item")

		and_conditions = [
			ip.price_list == self.wc_server.sales_price_list,
			iwc.woocommerce_server == self.wc_server.name,
			item.disabled == 0,
			iwc.woocommerce_id.isnotnull(),
			iwc.enabled == 1,
			*item_wide_price_conditions(ip),
		]
		if self.item_code:
			and_conditions.append(ip.item_code == self.item_code)

		results = (
			qb.from_(ip)
			.inner_join(iwc)
			.on(iwc.parent == ip.item_code)
			.inner_join(item)
			.on(item.name == ip.item_code)
			.select(
				ip.name,
				ip.item_code,
				ip.price_list_rate,
				ip.valid_from,
				ip.valid_upto,
				iwc.woocommerce_id,
			)
			.where(Criterion.all(and_conditions))
			.run(as_dict=True)
		)

		for row in results:
			self.sale_price_map[row.woocommerce_id] = row

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Synchronise Item Prices with WooCommerce Products
		"""
		for item_price in self.item_price_list:
			wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
				domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
			)
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product", "name": wc_product_name})
			# Handle variants
			if item_price.variant_of:
				wc_product.parent_id = get_variation_parent_woocommerce_id(
					item_price.woocommerce_server, item_price.item_code
				)

			try:
				wc_product.load_from_db()
				wc_product_dirty = False

				# ── Regular price ────────────────────────────────────────────────
				price_list_rate = (
					self.item_price_doc.price_list_rate
					if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.price_list
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
					wc_product.regular_price = price_list_rate
					wc_product_dirty = True

				# ── Sale price ───────────────────────────────────────────────────
				if self.wc_server.enable_sales_price_list_sync and self.wc_server.sales_price_list:
					wc_product_dirty |= self._apply_sale_price(wc_product, item_price.woocommerce_id)

				if wc_product_dirty:
					wc_product.save()

			except Exception:
				error_message = f"{frappe.get_traceback()}\n\n Product Data: \n{wc_product.as_dict()}"
				frappe.log_error("WooCommerce Error: Price List Sync", error_message)

			sleep(self.wc_server.price_list_delay_per_item)

	def _apply_sale_price(self, wc_product: WooCommerceProduct, woocommerce_id: str) -> bool:
		"""
		Set or clear sale_price, date_on_sale_from and date_on_sale_to on the
		WooCommerce Product based on the sale_price_map entry for this product.

		Returns True if any field was changed, False otherwise.
		"""
		sale_price_record = self.sale_price_map.get(woocommerce_id)
		dirty = False

		if sale_price_record:
			if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.sales_price_list:
				new_sale_price = float(self.item_price_doc.price_list_rate or 0)
				new_valid_from = self.item_price_doc.valid_from
				new_valid_upto = self.item_price_doc.valid_upto
			else:
				new_sale_price = float(sale_price_record.price_list_rate or 0)
				new_valid_from = sale_price_record.valid_from
				new_valid_upto = sale_price_record.valid_upto
		else:
			new_sale_price = 0
			new_valid_from = None
			new_valid_upto = None

		current_sale_price = (
			float(wc_product.sale_price)
			if wc_product.sale_price and isinstance(wc_product.sale_price, str)
			else float(wc_product.sale_price or 0)
		)

		if current_sale_price != new_sale_price:
			wc_product.sale_price = new_sale_price
			dirty = True

		new_from_str = _format_sale_date(new_valid_from)
		new_to_str = _format_sale_date(new_valid_upto)

		if wc_product.date_on_sale_from != new_from_str:
			wc_product.date_on_sale_from = new_from_str
			dirty = True

		if wc_product.date_on_sale_to != new_to_str:
			wc_product.date_on_sale_to = new_to_str
			dirty = True

		return dirty
