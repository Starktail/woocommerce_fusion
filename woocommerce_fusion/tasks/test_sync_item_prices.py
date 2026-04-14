# Copyright (c) 2024, Dirk van der Laarse and Contributors
# See license.txt

from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.sync_item_prices import (
	SynchroniseItemPrice,
	_format_sale_date,
)


def _make_wc_server(**kwargs) -> frappe._dict:
	return frappe._dict(
		name="site1.example.com",
		enable_sync=1,
		enable_price_list_sync=1,
		price_list="Standard Selling",
		enable_sales_price_list_sync=kwargs.get("enable_sales_price_list_sync", 1),
		sales_price_list=kwargs.get("sales_price_list", "Sale Prices"),
		price_list_delay_per_item=0,
	)


def _make_sync(**kwargs) -> SynchroniseItemPrice:
	sync = SynchroniseItemPrice.__new__(SynchroniseItemPrice)
	sync.wc_server = _make_wc_server(**kwargs)
	sync.item_price_doc = None
	sync.sale_price_map = {}
	return sync


def _make_wc_product(sale_price=0, date_on_sale_from=None, date_on_sale_to=None):
	wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
	wc_product.sale_price = sale_price
	wc_product.date_on_sale_from = date_on_sale_from
	wc_product.date_on_sale_to = date_on_sale_to
	return wc_product


class TestFormatSaleDate(FrappeTestCase):
	def test_date_string_converted_to_iso_datetime(self):
		self.assertEqual(_format_sale_date("2024-06-15"), "2024-06-15T00:00:00")

	def test_none_returns_none(self):
		self.assertIsNone(_format_sale_date(None))

	def test_empty_string_returns_none(self):
		self.assertIsNone(_format_sale_date(""))

	def test_date_with_time_preserved(self):
		self.assertEqual(_format_sale_date("2024-06-15 12:30:00"), "2024-06-15T12:30:00")


class TestApplySalePrice(FrappeTestCase):
	def test_sets_price_and_dates_when_record_exists(self):
		"""
		_apply_sale_price should update sale_price, date_on_sale_from and
		date_on_sale_to when a matching sale price record exists.
		"""
		sync = _make_sync()
		sync.sale_price_map = {
			"42": frappe._dict(
				price_list_rate=49.99,
				valid_from="2024-06-01",
				valid_upto="2024-06-30",
			)
		}
		wc_product = _make_wc_product()

		dirty = sync._apply_sale_price(wc_product, "42")

		self.assertTrue(dirty)
		self.assertEqual(float(wc_product.sale_price), 49.99)
		self.assertEqual(wc_product.date_on_sale_from, "2024-06-01T00:00:00")
		self.assertEqual(wc_product.date_on_sale_to, "2024-06-30T00:00:00")

	def test_clears_price_and_dates_when_no_record(self):
		"""
		_apply_sale_price should clear sale_price and dates when no matching
		sale price record exists in sale_price_map.
		"""
		sync = _make_sync()
		sync.sale_price_map = {}
		wc_product = _make_wc_product(
			sale_price=29.99,
			date_on_sale_from="2024-01-01T00:00:00",
			date_on_sale_to="2024-01-31T00:00:00",
		)

		dirty = sync._apply_sale_price(wc_product, "99")

		self.assertTrue(dirty)
		self.assertEqual(float(wc_product.sale_price), 0)
		self.assertIsNone(wc_product.date_on_sale_from)
		self.assertIsNone(wc_product.date_on_sale_to)

	def test_returns_false_when_already_in_sync(self):
		"""
		_apply_sale_price should return False when WooCommerce already has the
		correct sale price and dates.
		"""
		sync = _make_sync()
		sync.sale_price_map = {
			"42": frappe._dict(
				price_list_rate=49.99,
				valid_from="2024-06-01",
				valid_upto="2024-06-30",
			)
		}
		wc_product = _make_wc_product(
			sale_price="49.99",
			date_on_sale_from="2024-06-01T00:00:00",
			date_on_sale_to="2024-06-30T00:00:00",
		)

		dirty = sync._apply_sale_price(wc_product, "42")

		self.assertFalse(dirty)

	def test_uses_item_price_doc_rate_when_it_belongs_to_sales_list(self):
		"""
		When item_price_doc belongs to the sales_price_list, its rate should be
		used instead of the queried sale_price_map rate.
		"""
		sync = _make_sync()
		sync.item_price_doc = frappe._dict(
			price_list="Sale Prices",
			price_list_rate=39.99,
			valid_from="2024-07-01",
			valid_upto="2024-07-31",
		)
		sync.sale_price_map = {
			"42": frappe._dict(
				price_list_rate=49.99,
				valid_from="2024-06-01",
				valid_upto="2024-06-30",
			)
		}
		wc_product = _make_wc_product()

		sync._apply_sale_price(wc_product, "42")

		self.assertEqual(float(wc_product.sale_price), 39.99)
		self.assertEqual(wc_product.date_on_sale_from, "2024-07-01T00:00:00")
		self.assertEqual(wc_product.date_on_sale_to, "2024-07-31T00:00:00")

	def test_ignores_item_price_doc_when_it_belongs_to_regular_list(self):
		"""
		When item_price_doc belongs to the regular price_list (not sales_price_list),
		the queried sale_price_map rate should be used.
		"""
		sync = _make_sync()
		sync.item_price_doc = frappe._dict(
			price_list="Standard Selling",
			price_list_rate=99.99,
			valid_from=None,
			valid_upto=None,
		)
		sync.sale_price_map = {
			"42": frappe._dict(
				price_list_rate=49.99,
				valid_from="2024-06-01",
				valid_upto="2024-06-30",
			)
		}
		wc_product = _make_wc_product()

		sync._apply_sale_price(wc_product, "42")

		self.assertEqual(float(wc_product.sale_price), 49.99)

	def test_sets_none_dates_when_record_has_no_validity_dates(self):
		"""
		When a sale price record has no valid_from / valid_upto, dates should
		be cleared on the WooCommerce product (no date restriction).
		"""
		sync = _make_sync()
		sync.sale_price_map = {"42": frappe._dict(price_list_rate=49.99, valid_from=None, valid_upto=None)}
		wc_product = _make_wc_product(
			date_on_sale_from="2024-01-01T00:00:00",
			date_on_sale_to="2024-01-31T00:00:00",
		)

		dirty = sync._apply_sale_price(wc_product, "42")

		self.assertTrue(dirty)
		self.assertIsNone(wc_product.date_on_sale_from)
		self.assertIsNone(wc_product.date_on_sale_to)


class TestGetErpnextSalePrices(FrappeTestCase):
	def test_returns_empty_map_when_feature_disabled(self):
		sync = _make_sync(enable_sales_price_list_sync=0)
		sync.get_erpnext_sale_prices()
		self.assertEqual(sync.sale_price_map, {})

	def test_returns_empty_map_when_no_sales_price_list_set(self):
		sync = _make_sync(sales_price_list=None)
		sync.get_erpnext_sale_prices()
		self.assertEqual(sync.sale_price_map, {})

	def test_returns_empty_map_when_price_list_sync_disabled(self):
		sync = _make_sync()
		sync.wc_server.enable_price_list_sync = 0
		sync.get_erpnext_sale_prices()
		self.assertEqual(sync.sale_price_map, {})

	@patch("woocommerce_fusion.tasks.sync_item_prices.qb", new_callable=MagicMock)
	def test_keys_results_by_woocommerce_id(self, mock_qb):
		sync = _make_sync()
		sync.item_code = None

		mock_query = MagicMock()
		mock_qb.DocType.return_value = MagicMock()
		mock_qb.from_.return_value = mock_query
		mock_query.inner_join.return_value = mock_query
		mock_query.on.return_value = mock_query
		mock_query.select.return_value = mock_query
		mock_query.where.return_value = mock_query
		mock_query.run.return_value = [
			frappe._dict(
				name="IP-001",
				item_code="ITEM-001",
				price_list_rate=49.99,
				valid_from="2024-06-01",
				valid_upto="2024-06-30",
				woocommerce_id="42",
			)
		]

		sync.get_erpnext_sale_prices()

		self.assertIn("42", sync.sale_price_map)
		self.assertEqual(sync.sale_price_map["42"].price_list_rate, 49.99)
