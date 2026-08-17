import math
from urllib.parse import urlparse

import frappe
from erpnext import get_default_company
from erpnext.stock.doctype.item.test_item import create_item
from parameterized import parameterized

from woocommerce_fusion.tasks.stock_update import update_stock_levels_on_woocommerce_site
from woocommerce_fusion.tasks.test_integration_helpers import (
	TestIntegrationWooCommerce,
	get_woocommerce_server,
)

BATCH_MODES = [("single_call", False), ("batch_api", True)]


class TestIntegrationWooCommerceStockSync(TestIntegrationWooCommerce):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@parameterized.expand(BATCH_MODES)
	def test_stock_sync_when_synchronising_with_woocommerce(self, _name, batch_enabled):
		"""
		Test that the Stock Synchronisation method posts the correct stock level to a WooCommerce website.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce, set opening stock to 1
		wc_product_id = self.post_woocommerce_product(product_name="ITEM009", opening_stock=1)

		# Create the same product in ERPNext (with opening stock of 5, not 1) and link it
		item = create_item(
			"ITEM009",
			valuation_rate=10,
			warehouse="Stores - SC",
			company=get_default_company(),
			opening_stock=5,
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# Run synchronisation
		stock_update_result = update_stock_levels_on_woocommerce_site(item_code=item.name)
		self._flush_if_batch()

		# Expect successful update
		self.assertEqual(stock_update_result, True)

		# Expect correct stock level of 5 in WooCommerce
		wc_stock_level = self.get_woocommerce_product_stock_level(product_id=wc_product_id)
		self.assertEqual(wc_stock_level, 5)

	@parameterized.expand(BATCH_MODES)
	def test_stock_sync_with_decimal_when_synchronising_with_woocommerce(self, _name, batch_enabled):
		"""
		Test that the Stock Synchronisation method posts the correct stock level to a WooCommerce website
		while handling decimals.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce, set opening stock to 1
		wc_product_id = self.post_woocommerce_product(product_name="ITEM002", opening_stock=1)

		# Create the same product in ERPNext (with opening stock of 6.9, not 1) and link it
		item = create_item(
			"ITEM002",
			valuation_rate=10,
			warehouse="Stores - SC",
			company=get_default_company(),
			stock_uom="Kg",
			opening_stock=6.9,
		)
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# Run synchronisation
		stock_update_result = update_stock_levels_on_woocommerce_site(item_code=item.name)
		self._flush_if_batch()

		# Expect successful update
		self.assertEqual(stock_update_result, True)

		# Expect correct stock level of 6.9 rounded down in WooCommerce (WooCommerce API doesn't accept float values)
		wc_stock_level = self.get_woocommerce_product_stock_level(product_id=wc_product_id)
		self.assertEqual(wc_stock_level, math.floor(6.9))

	@parameterized.expand(BATCH_MODES)
	def test_stock_sync_pushes_zero_for_a_disabled_item(self, _name, batch_enabled):
		"""
		Test that disabling an Item clears its WooCommerce Product's stock, rather than leaving the
		product on sale at the stock level of its last synchronisation.
		"""
		self._set_batch_mode(batch_enabled)

		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.push_zero_stock_for_disabled_items = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		wc_product_id = self.post_woocommerce_product(product_name="ITEM010", opening_stock=1)
		item = create_item(
			"ITEM010",
			valuation_rate=10,
			warehouse="Stores - SC",
			company=get_default_company(),
			opening_stock=5,
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# The Item's own stock reaches WooCommerce while it is enabled
		update_stock_levels_on_woocommerce_site(item_code=item.name)
		self._flush_if_batch()
		self.assertEqual(self.get_woocommerce_product_stock_level(product_id=wc_product_id), 5)

		# Disabling it clears the product, even though the stock itself has not moved
		item.disabled = 1
		item.save()
		self.assertEqual(update_stock_levels_on_woocommerce_site(item_code=item.name), True)
		self._flush_if_batch()

		self.assertEqual(self.get_woocommerce_product_stock_level(product_id=wc_product_id), 0)
