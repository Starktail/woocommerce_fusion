from urllib.parse import urlparse

import frappe
from erpnext import get_default_company
from erpnext.stock.doctype.item.test_item import create_item
from parameterized import parameterized

from woocommerce_fusion.tasks.sync import get_variation_parent_woocommerce_id
from woocommerce_fusion.tasks.sync_item_prices import run_item_price_sync
from woocommerce_fusion.tasks.sync_items import run_item_sync
from woocommerce_fusion.tasks.test_integration_helpers import (
	TestIntegrationWooCommerce,
	default_warehouse,
	get_woocommerce_server,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

BATCH_MODES = [("single_call", False), ("batch_api", True)]


class TestIntegrationWooCommerceItemPriceSync(TestIntegrationWooCommerce):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@parameterized.expand(BATCH_MODES)
	def test_item_price_sync_when_synchronising_with_woocommerce(self, _name, batch_enabled):
		"""
		Test that the Item Price Synchronisation method posts the correct price to a WooCommerce website.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce, set regular price to 10
		wc_product_id = self.post_woocommerce_product(product_name="ITEM002", regular_price=10)

		# Create the same product in ERPNext (with opening stock of 5, not 1) and link it
		item = create_item(
			"ITEM002", valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# Add an Item Price
		item_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": "ITEM002",
				"price_list": "_Test Price List",
				"price_list_rate": 5000,
			}
		)
		item_price.insert()

		# Run synchronisation
		stock_update_result = run_item_price_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect successful update
		self.assertEqual(stock_update_result, True)

		# Expect correct price of 5000 in WooCommerce
		wc_price = self.get_woocommerce_product_price(product_id=wc_product_id)
		self.assertEqual(float(wc_price), 5000)

	@parameterized.expand(BATCH_MODES)
	def test_item_price_sync_ignored_if_item_disabled_when_synchronising_with_woocommerce(
		self, _name, batch_enabled
	):
		"""
		Test that the Item Price Synchronisation method does not post a price to a WooCommerce website when the item is disabled.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce, set regular price to 10
		wc_product_id = self.post_woocommerce_product(product_name="ITEM003", regular_price=10)

		# Create the same product in ERPNext (with opening stock of 5, not 1) and link it
		item = create_item(
			"ITEM003", valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name

		# Disable the item
		item.disabled = 1
		item.save()

		# Add an Item Price
		item_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": "ITEM003",
				"price_list": "_Test Price List",
				"price_list_rate": 6000,
			}
		)
		item_price.insert()

		# Run synchronisation
		stock_update_result = run_item_price_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect successful update
		self.assertEqual(stock_update_result, True)

		# Expect correct unchanged price of 10 in WooCommerce
		wc_price = self.get_woocommerce_product_price(product_id=wc_product_id)
		self.assertEqual(float(wc_price), 10)

	@parameterized.expand(BATCH_MODES)
	def test_item_price_sync_for_disabled_item_when_the_server_asks_for_it(self, _name, batch_enabled):
		"""
		Test that the Item Price Synchronisation method posts a price for a disabled Item when
		'Sync Prices for Disabled Items' is enabled. Without it the product keeps the price of its
		last synchronisation.
		"""
		self._set_batch_mode(batch_enabled)

		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.sync_prices_for_disabled_items = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new product in WooCommerce, set regular price to 10
		wc_product_id = self.post_woocommerce_product(product_name="ITEM004", regular_price=10)

		# Create the same disabled product in ERPNext and link it
		item = create_item(
			"ITEM004", valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.disabled = 1
		item.save()

		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": "ITEM004",
				"price_list": "_Test Price List",
				"price_list_rate": 6000,
			}
		).insert()

		# Run synchronisation
		self.assertEqual(run_item_price_sync(item_code=item.name), True)
		self._flush_if_batch()

		# Expect the disabled Item's price to have reached WooCommerce
		wc_price = self.get_woocommerce_product_price(product_id=wc_product_id)
		self.assertEqual(float(wc_price), 6000)

	@parameterized.expand(BATCH_MODES)
	def test_variation_price_sync_when_synchronising_with_woocommerce(self, _name, batch_enabled):
		"""
		Test that the Item Price Synchronisation method posts the price of a variant Item to the
		WooCommerce variation, and not to its parent product.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a variable product with one variation in WooCommerce, both priced at 10
		wc_variation_id = self.post_woocommerce_product(
			product_name="ITEM004", type="variation", attributes=["Material Type"], regular_price=10
		)

		# Sync inbound, so that ERPNext has the template Item and the variant Item, each linked
		# to their WooCommerce counterpart
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_variation_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		variant_item_code = frappe.db.get_value(
			"Item WooCommerce Server",
			{"woocommerce_server": self.wc_server.name, "woocommerce_id": wc_variation_id},
			"parent",
		)
		self.assertIsNotNone(variant_item_code)
		wc_parent_id = get_variation_parent_woocommerce_id(self.wc_server.name, variant_item_code)
		self.assertIsNotNone(wc_parent_id)

		# Add an Item Price for the variant Item
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": variant_item_code,
				"price_list": self.wc_server.price_list,
				"price_list_rate": 5000,
			}
		).insert()

		# Run synchronisation
		self.assertEqual(run_item_price_sync(item_code=variant_item_code), True)
		self._flush_if_batch()

		# Expect the price on the variation
		variation = self.get_woocommerce_product(product_id=wc_variation_id, parent_id=wc_parent_id)
		self.assertEqual(float(variation["regular_price"]), 5000)

		# Expect the parent product's own price left alone. A variable product derives its
		# displayed `price` from its variations, so only `regular_price` shows whether anything
		# was written to the parent itself - and writing there would be a silent no-op.
		parent = self.get_woocommerce_product(product_id=wc_parent_id)
		self.assertEqual(parent["type"], "variable")
		self.assertEqual(parent["regular_price"], "")
