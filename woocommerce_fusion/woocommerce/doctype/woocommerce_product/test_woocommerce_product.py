# Copyright (c) 2024, Dirk van der Laarse and Contributors
# See license.txt

from datetime import datetime

from frappe.tests import UnitTestCase

from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)


def _base_product(**overrides) -> dict:
	product = {
		"type": "simple",
		"weight": 1.5,
		"regular_price": 99.99,
		"sale_price": 0,
		"date_on_sale_from": None,
		"date_on_sale_to": None,
		"woocommerce_name": "Test Product",
		"related_ids": [],
		"parent_id": None,
	}
	product.update(overrides)
	return product


class TestWooCommerceProduct(UnitTestCase):
	def test_clean_up_product_clears_sale_price_with_empty_string(self):
		"""
		When sale_price is 0, clean_up_product_before_write should send ""
		so WooCommerce actually clears an existing sale price.
		"""
		result = WooCommerceProduct.clean_up_product_before_write(_base_product(sale_price=0))
		self.assertIn("sale_price", result)
		self.assertEqual(result["sale_price"], "")

	def test_clean_up_product_sets_sale_price_as_string_when_positive(self):
		result = WooCommerceProduct.clean_up_product_before_write(_base_product(sale_price=49.99))
		self.assertEqual(result["sale_price"], "49.99")

	def test_clean_up_product_clears_sale_price_when_none(self):
		result = WooCommerceProduct.clean_up_product_before_write(_base_product(sale_price=None))
		self.assertEqual(result["sale_price"], "")

	def test_clean_up_product_serialises_sale_dates_as_strings(self):
		result = WooCommerceProduct.clean_up_product_before_write(
			_base_product(
				sale_price=49.99,
				date_on_sale_from="2024-06-01T00:00:00",
				date_on_sale_to="2024-06-30T00:00:00",
			)
		)
		self.assertEqual(result["date_on_sale_from"], "2024-06-01T00:00:00")
		self.assertEqual(result["date_on_sale_to"], "2024-06-30T00:00:00")

	def test_clean_up_product_serialises_datetime_objects(self):
		result = WooCommerceProduct.clean_up_product_before_write(
			_base_product(
				sale_price=49.99,
				date_on_sale_from=datetime(2024, 6, 1, 0, 0, 0),
				date_on_sale_to=datetime(2024, 6, 30, 23, 59, 59),
			)
		)
		self.assertEqual(result["date_on_sale_from"], "2024-06-01T00:00:00")
		self.assertEqual(result["date_on_sale_to"], "2024-06-30T23:59:59")

	def test_clean_up_product_sets_none_for_missing_sale_dates(self):
		result = WooCommerceProduct.clean_up_product_before_write(
			_base_product(date_on_sale_from=None, date_on_sale_to=None)
		)
		self.assertIsNone(result["date_on_sale_from"])
		self.assertIsNone(result["date_on_sale_to"])

	def test_clean_up_product_drops_related_ids(self):
		result = WooCommerceProduct.clean_up_product_before_write(_base_product())
		self.assertNotIn("related_ids", result)
