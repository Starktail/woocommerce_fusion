# Copyright (c) 2024, Dirk van der Laarse and Contributors
# See license.txt

import frappe
from erpnext import get_default_company
from erpnext.stock.doctype.item.test_item import create_item
from frappe.utils import add_to_date, nowdate

from woocommerce_fusion.tasks.sync_item_prices import run_item_price_sync
from woocommerce_fusion.tasks.test_integration_helpers import (
	TestIntegrationWooCommerce,
	get_woocommerce_server,
)

SALES_PRICE_LIST = "_Test Sale Price List"


class TestIntegrationSalePriceSync(TestIntegrationWooCommerce):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

	def setUp(self):
		super().setUp()
		# Create the sale price list if it doesn't exist
		if not frappe.db.exists("Price List", SALES_PRICE_LIST):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": SALES_PRICE_LIST,
					"selling": 1,
					"currency": "USD",
				}
			).insert()

		# Enable sale price list sync on the server
		self.wc_server.enable_sales_price_list_sync = 1
		self.wc_server.sales_price_list = SALES_PRICE_LIST
		self.wc_server.save()

	def _create_linked_item(self, item_code: str, wc_product_id: int, regular_price: float = 100) -> object:
		item = create_item(item_code, valuation_rate=10, warehouse=None, company=get_default_company())
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()
		# A regular price is required for the sync loop to run for this item
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": self.wc_server.price_list,
				"price_list_rate": regular_price,
			}
		).insert()
		return item

	def _create_sale_price(
		self,
		item_code: str,
		rate: float,
		valid_from: str | None = None,
		valid_upto: str | None = None,
	) -> object:
		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": SALES_PRICE_LIST,
				"price_list_rate": rate,
				"valid_from": valid_from,
				"valid_upto": valid_upto,
			}
		)
		doc.insert()
		return doc

	def test_sale_price_is_pushed_to_woocommerce(self):
		"""
		Sale price from the Sales Price List should be synced to WooCommerce sale_price.
		"""
		wc_product_id = self.post_woocommerce_product(product_name="SALE001", regular_price=100)
		item = self._create_linked_item("SALE001", wc_product_id)
		self._create_sale_price("SALE001", rate=75)

		result = run_item_price_sync(item_code=item.name)
		self.assertEqual(result, True)

		product_data = self.get_woocommerce_product(wc_product_id)
		self.assertEqual(float(product_data["sale_price"]), 75)

	def test_sale_price_with_validity_dates_sets_woocommerce_sale_dates(self):
		"""
		When the Item Price has valid_from / valid_upto dates, WooCommerce
		date_on_sale_from and date_on_sale_to should be populated accordingly.
		"""
		wc_product_id = self.post_woocommerce_product(product_name="SALE002", regular_price=100)
		item = self._create_linked_item("SALE002", wc_product_id)

		valid_from = nowdate()
		valid_upto = add_to_date(nowdate(), days=7)
		self._create_sale_price("SALE002", rate=60, valid_from=valid_from, valid_upto=valid_upto)

		run_item_price_sync(item_code=item.name)

		product_data = self.get_woocommerce_product(wc_product_id)
		self.assertEqual(float(product_data["sale_price"]), 60)
		# WooCommerce returns dates in ISO 8601 — just check the date portion
		self.assertTrue(product_data["date_on_sale_from"].startswith(valid_from))
		self.assertTrue(product_data["date_on_sale_to"].startswith(valid_upto))

	def test_sale_price_without_validity_dates_has_no_end_date(self):
		"""
		When the Item Price has no validity dates, the sale has no end date
		on WooCommerce. WooCommerce auto-populates date_on_sale_from with the
		current timestamp when a sale price is set without a start date, but
		date_on_sale_to must remain null (open-ended sale).
		"""
		wc_product_id = self.post_woocommerce_product(product_name="SALE003", regular_price=100)
		item = self._create_linked_item("SALE003", wc_product_id)
		self._create_sale_price("SALE003", rate=80, valid_from=None, valid_upto=None)

		run_item_price_sync(item_code=item.name)

		product_data = self.get_woocommerce_product(wc_product_id)
		self.assertEqual(float(product_data["sale_price"]), 80)
		# WooCommerce auto-sets date_on_sale_from to now when no start date is sent;
		# date_on_sale_to must be null to indicate an open-ended sale.
		self.assertIsNone(product_data.get("date_on_sale_to"))

	def test_removing_sale_price_clears_woocommerce_sale_price(self):
		"""
		When no Item Price record exists on the Sales Price List for an item,
		the sale_price on WooCommerce should be cleared (set to "").
		"""
		wc_product_id = self.post_woocommerce_product(product_name="SALE004", regular_price=100)
		item = self._create_linked_item("SALE004", wc_product_id)

		# First push a sale price so WooCommerce has one
		sale_price_doc = self._create_sale_price("SALE004", rate=50)
		run_item_price_sync(item_code=item.name)
		self.assertEqual(float(self.get_woocommerce_product(wc_product_id)["sale_price"]), 50)

		# Delete the sale price record and sync again
		sale_price_doc.delete()
		run_item_price_sync(item_code=item.name)

		product_data = self.get_woocommerce_product(wc_product_id)
		# WooCommerce returns "" or omits the field when no sale price is active
		self.assertFalse(float(product_data.get("sale_price") or 0) > 0)

	def test_sale_price_sync_skipped_when_feature_disabled(self):
		"""
		When enable_sales_price_list_sync is off, WooCommerce sale_price should
		not be modified even if a sale price record exists in ERPNext.
		"""
		wc_product_id = self.post_woocommerce_product(product_name="SALE005", regular_price=100)
		item = self._create_linked_item("SALE005", wc_product_id)
		self._create_sale_price("SALE005", rate=45)

		# Disable sale price sync
		self.wc_server.enable_sales_price_list_sync = 0
		self.wc_server.save()

		run_item_price_sync(item_code=item.name)

		product_data = self.get_woocommerce_product(wc_product_id)
		# Sale price should remain unset (WooCommerce default is "")
		self.assertFalse(float(product_data.get("sale_price") or 0) > 0)
