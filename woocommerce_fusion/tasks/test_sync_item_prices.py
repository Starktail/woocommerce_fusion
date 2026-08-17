# Copyright (c) 2024, Dirk van der Laarse and Contributors
# See license.txt

from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from woocommerce_fusion.tasks.sync_item_prices import (
	SynchroniseItemPrice,
	_format_sale_date,
)
from woocommerce_fusion.tasks.sync_items import (
	ERPNextItemToSync,
	get_item_price_rate,
	get_item_sale_price_data,
)


def _make_wc_server(**kwargs) -> frappe._dict:
	return frappe._dict(
		name=kwargs.get("name", "site1.example.com"),
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


class TestFormatSaleDate(UnitTestCase):
	def test_date_string_converted_to_iso_datetime(self):
		self.assertEqual(_format_sale_date("2024-06-15"), "2024-06-15T00:00:00")

	def test_none_returns_none(self):
		self.assertIsNone(_format_sale_date(None))

	def test_empty_string_returns_none(self):
		self.assertIsNone(_format_sale_date(""))

	def test_date_with_time_preserved(self):
		self.assertEqual(_format_sale_date("2024-06-15 12:30:00"), "2024-06-15T12:30:00")


class TestApplySalePrice(UnitTestCase):
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


class TestGetErpnextSalePrices(UnitTestCase):
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


PRICE_SCOPE_SERVER_URL = "https://price-scope-unit-test.example.com"
PRICE_SCOPE_SERVER = "price-scope-unit-test.example.com"
PRICE_SCOPE_ITEM = "UNIT-PRICE-SCOPE-ITEM"
PRICE_SCOPE_WC_ID = "9001"
PRICE_SCOPE_BATCH = "UNIT-PRICE-SCOPE-BATCH"
PRICE_SCOPE_CUSTOMER = "_Unit Test WC Price Customer"
REGULAR_PRICE_LIST = "_Unit Test WC Selling"
SALES_PRICE_LIST = "_Unit Test WC Sale"


def _ensure_price_scope_fixtures() -> None:
	"""
	Create the WooCommerce Server, price lists, customer and linked Item used by the
	item-wide price scoping tests.
	"""
	for price_list in (REGULAR_PRICE_LIST, SALES_PRICE_LIST):
		if not frappe.db.exists("Price List", price_list):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": price_list,
					"currency": "USD",
					"selling": 1,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

	if not frappe.db.exists("WooCommerce Server", PRICE_SCOPE_SERVER):
		server = frappe.new_doc("WooCommerce Server")
		server.woocommerce_server_url = PRICE_SCOPE_SERVER_URL
		# These tests only need this server to exist, so that the Item can carry a link to it. Give it
		# credentials and leave sync off
		server.api_consumer_key = "ck_price_scope_unit_test"
		server.api_consumer_secret = "cs_price_scope_unit_test"
		server.enable_sync = 0
		server.enable_price_list_sync = 1
		server.price_list = REGULAR_PRICE_LIST
		server.enable_sales_price_list_sync = 1
		server.sales_price_list = SALES_PRICE_LIST
		server.creation_user = "Administrator"
		# Mandatory only for the warehouse and accounting fields, which these tests never reach
		server.insert(ignore_permissions=True, ignore_mandatory=True)

	if not frappe.db.exists("Customer", PRICE_SCOPE_CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": PRICE_SCOPE_CUSTOMER,
				"customer_type": "Individual",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item", PRICE_SCOPE_ITEM):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": PRICE_SCOPE_ITEM,
				"item_name": PRICE_SCOPE_ITEM,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "UNIT-PRICE-SCOPE-BATCH-.###",
			}
		)
		item.insert(ignore_permissions=True)
	else:
		item = frappe.get_doc("Item", PRICE_SCOPE_ITEM)

	if not frappe.db.exists("Batch", PRICE_SCOPE_BATCH):
		frappe.get_doc(
			{
				"doctype": "Batch",
				"batch_id": PRICE_SCOPE_BATCH,
				"item": PRICE_SCOPE_ITEM,
			}
		).insert(ignore_permissions=True)

	if not any(row.woocommerce_server == PRICE_SCOPE_SERVER for row in item.woocommerce_servers):
		row = item.append("woocommerce_servers")
		row.woocommerce_server = PRICE_SCOPE_SERVER
		row.woocommerce_id = PRICE_SCOPE_WC_ID
		row.enabled = 1
		item.save(ignore_permissions=True)


def _make_item_price(price_list: str, rate: float, batch_no=None, customer=None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": PRICE_SCOPE_ITEM,
			"price_list": price_list,
			"price_list_rate": rate,
			"batch_no": batch_no,
			"customer": customer,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestItemWidePriceScoping(IntegrationTestCase):
	"""
	A WooCommerce product carries one product-wide price, so batch- and party-scoped
	Item Price rows must never compete with the item-wide row.
	"""

	def setUp(self):
		_ensure_price_scope_fixtures()
		frappe.db.delete("Item Price", {"item_code": PRICE_SCOPE_ITEM})

		self.item_wide = _make_item_price(REGULAR_PRICE_LIST, 100)
		_make_item_price(REGULAR_PRICE_LIST, 60, batch_no="UNIT-PRICE-SCOPE-BATCH")
		_make_item_price(REGULAR_PRICE_LIST, 80, customer=PRICE_SCOPE_CUSTOMER)

		self.item_wide_sale = _make_item_price(SALES_PRICE_LIST, 90)
		_make_item_price(SALES_PRICE_LIST, 50, batch_no="UNIT-PRICE-SCOPE-BATCH")
		_make_item_price(SALES_PRICE_LIST, 70, customer=PRICE_SCOPE_CUSTOMER)

	def _sync(self) -> SynchroniseItemPrice:
		sync = _make_sync(name=PRICE_SCOPE_SERVER, sales_price_list=SALES_PRICE_LIST)
		sync.wc_server.price_list = REGULAR_PRICE_LIST
		sync.item_code = PRICE_SCOPE_ITEM
		sync.item_price_list = []
		return sync

	def test_get_erpnext_item_prices_returns_only_the_item_wide_row(self):
		sync = self._sync()
		sync.get_erpnext_item_prices()

		self.assertEqual([row.name for row in sync.item_price_list], [self.item_wide])
		self.assertEqual(sync.item_price_list[0].price_list_rate, 100)

	def test_get_erpnext_sale_prices_returns_only_the_item_wide_row(self):
		sync = self._sync()
		sync.get_erpnext_sale_prices()

		self.assertEqual(list(sync.sale_price_map.keys()), [PRICE_SCOPE_WC_ID])
		self.assertEqual(sync.sale_price_map[PRICE_SCOPE_WC_ID].name, self.item_wide_sale)
		self.assertEqual(sync.sale_price_map[PRICE_SCOPE_WC_ID].price_list_rate, 90)


class TestItemWidePriceScopingOnCreatePath(IntegrationTestCase):
	"""
	get_item_price_rate / get_item_sale_price_data feed _build_create_payload, which the
	BatchProcessor uses to create WooCommerce products - so they need the same scoping.
	"""

	def setUp(self):
		_ensure_price_scope_fixtures()
		frappe.db.delete("Item Price", {"item_code": PRICE_SCOPE_ITEM})
		frappe.clear_cache(doctype="WooCommerce Server")

		_make_item_price(REGULAR_PRICE_LIST, 100)
		_make_item_price(REGULAR_PRICE_LIST, 60, batch_no=PRICE_SCOPE_BATCH)
		_make_item_price(REGULAR_PRICE_LIST, 80, customer=PRICE_SCOPE_CUSTOMER)

		_make_item_price(SALES_PRICE_LIST, 90)
		_make_item_price(SALES_PRICE_LIST, 50, batch_no=PRICE_SCOPE_BATCH)
		_make_item_price(SALES_PRICE_LIST, 70, customer=PRICE_SCOPE_CUSTOMER)

		self.item_for_sync = ERPNextItemToSync(
			item=frappe.get_doc("Item", PRICE_SCOPE_ITEM),
			item_woocommerce_server_idx=1,
		)

	def test_get_item_price_rate_ignores_batch_and_party_rows(self):
		self.assertEqual(get_item_price_rate(self.item_for_sync), 100)

	def test_get_item_sale_price_data_ignores_batch_and_party_rows(self):
		sale_price = get_item_sale_price_data(self.item_for_sync)
		self.assertIsNotNone(sale_price)
		self.assertEqual(sale_price.price_list_rate, 90)

	def test_prices_are_matched_on_item_code_and_not_item_name(self):
		"""
		Item Price.item_code links to the Item's code, which is not necessarily its name.
		"""
		frappe.db.set_value("Item", PRICE_SCOPE_ITEM, "item_name", "A Different Display Name")
		item_for_sync = ERPNextItemToSync(
			item=frappe.get_doc("Item", PRICE_SCOPE_ITEM),
			item_woocommerce_server_idx=1,
		)

		self.assertEqual(get_item_price_rate(item_for_sync), 100)
		self.assertEqual(get_item_sale_price_data(item_for_sync).price_list_rate, 90)


class TestDisabledItemPriceScope(IntegrationTestCase):
	"""
	A disabled Item is skipped by price sync, so its product keeps the price of its last
	synchronisation and an expired sale is never cleared.
	"""

	def setUp(self):
		_ensure_price_scope_fixtures()
		frappe.db.delete("Item Price", {"item_code": PRICE_SCOPE_ITEM})
		self.item_wide = _make_item_price(REGULAR_PRICE_LIST, 100)
		self.item_wide_sale = _make_item_price(SALES_PRICE_LIST, 90)
		frappe.db.set_value("Item", PRICE_SCOPE_ITEM, "disabled", 1)

	def tearDown(self):
		frappe.db.set_value("Item", PRICE_SCOPE_ITEM, "disabled", 0)

	def _sync(self, sync_prices_for_disabled_items: int) -> SynchroniseItemPrice:
		sync = _make_sync(name=PRICE_SCOPE_SERVER, sales_price_list=SALES_PRICE_LIST)
		sync.wc_server.price_list = REGULAR_PRICE_LIST
		sync.wc_server.sync_prices_for_disabled_items = sync_prices_for_disabled_items
		sync.item_code = PRICE_SCOPE_ITEM
		sync.item_price_list = []
		return sync

	def test_a_disabled_item_is_skipped_by_default(self):
		sync = self._sync(0)
		sync.get_erpnext_item_prices()
		sync.get_erpnext_sale_prices()

		self.assertEqual(sync.item_price_list, [])
		self.assertEqual(sync.sale_price_map, {})

	def test_a_disabled_item_is_included_when_the_server_asks_for_it(self):
		sync = self._sync(1)
		sync.get_erpnext_item_prices()
		sync.get_erpnext_sale_prices()

		self.assertEqual([row.name for row in sync.item_price_list], [self.item_wide])
		self.assertEqual(sync.sale_price_map[PRICE_SCOPE_WC_ID].name, self.item_wide_sale)

	def test_both_queries_agree_so_a_sale_is_not_cleared_by_accident(self):
		"""
		A disabled Item reaching the regular list but not the sale map would have its sale price
		cleared, as though the sale had been withdrawn
		"""
		for setting in (0, 1):
			with self.subTest(sync_prices_for_disabled_items=setting):
				sync = self._sync(setting)
				sync.get_erpnext_item_prices()
				sync.get_erpnext_sale_prices()

				self.assertEqual(bool(sync.item_price_list), bool(sync.sale_price_map))
