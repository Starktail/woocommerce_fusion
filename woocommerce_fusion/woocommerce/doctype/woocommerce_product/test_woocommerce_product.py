# Copyright (c) 2024, Dirk van der Laarse and Contributors
# See license.txt

from datetime import datetime
from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	WooCommerceAPI,
	generate_woocommerce_record_name_from_domain_and_id,
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


class TestWooCommerceProduct(FrappeTestCase):
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

	def test_clean_up_product_drops_prices_for_a_variable_product(self):
		"""
		A variable product's price comes from its variations, so we should not write one back.
		"""
		result = WooCommerceProduct.clean_up_product_before_write(
			_base_product(type="variable", regular_price=0, sale_price=0)
		)

		self.assertNotIn("regular_price", result)
		self.assertNotIn("sale_price", result)


class TestLoadFromDbEndpoint(FrappeTestCase):
	"""
	A WooCommerce variation lives under its parent product, so load_from_db has to address
	products/{parent_id}/variations/{id} instead of products/{id}.
	"""

	WC_SERVER = "site1.example.com"

	def _load(self, product_id: int, parent_id=None) -> str:
		"""Run load_from_db against a mocked API and return the endpoint it requested."""
		mock_api = Mock()
		mock_api.get.return_value.json.return_value = {"id": product_id, "parent_id": parent_id or 0}
		api_list = [
			WooCommerceAPI(
				api=mock_api,
				woocommerce_server_url=f"https://{self.WC_SERVER}",
				woocommerce_server=self.WC_SERVER,
			)
		]

		with patch.object(WooCommerceProduct, "__init__", return_value=None):
			with patch.object(WooCommerceProduct, "call_super_init"):
				with patch.object(WooCommerceProduct, "_init_api", return_value=api_list):
					with patch.object(WooCommerceProduct, "pre_init_document", side_effect=lambda r, **_: r):
						with patch.object(WooCommerceProduct, "after_load_from_db", side_effect=lambda r: r):
							wc_product = WooCommerceProduct()
							wc_product.doctype = "WooCommerce Product"
							wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
								self.WC_SERVER, product_id
							)
							wc_product.wc_api_list = None
							if parent_id:
								wc_product.parent_id = parent_id
							wc_product.load_from_db()

		return mock_api.get.call_args.args[0]

	def test_product_is_read_from_the_products_endpoint(self):
		self.assertEqual(self._load(11), "products/11")

	def test_variation_is_read_from_its_parents_endpoint(self):
		self.assertEqual(self._load(12, parent_id=11), "products/11/variations/12")
