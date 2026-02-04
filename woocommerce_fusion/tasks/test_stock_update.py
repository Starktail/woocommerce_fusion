from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.stock_update import (
	update_stock_levels_for_all_enabled_items_in_background,
	update_stock_levels_on_woocommerce_site,
)


class TestWooCommerceStockSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site(self, mock_wc_api, mock_frappe):
		# Set up a dummy item set to sync to two different WC sites
		some_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1),
				frappe._dict(woocommerce_id=2, woocommerce_server="woo2.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
		)
		mock_frappe.get_doc.return_value = some_item

		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(warehouse="Warehouse A", actual_qty=5),
			frappe._dict(warehouse="Warehouse B", actual_qty=10),
			frappe._dict(warehouse="Warehouse C", actual_qty=20),
		]
		mock_frappe.get_list.return_value = bin_list

		# Set up mock return values
		mock_frappe.get_cached_doc.side_effect = [
			frappe._dict(
				woocommerce_server="woo1.example.com",
				enable_sync=1,
				enable_stock_level_synchronisation=1,
				warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
			),
			frappe._dict(
				woocommerce_server="woo2.example.com",
				enable_sync=1,
				enable_stock_level_synchronisation=1,
				warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
			),
		]

		# Mock out calls to WooCommerce API's
		mock_put_response = Mock()
		mock_put_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.put.return_value = mock_put_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("some_item_code")

		# Assert that the inventories put calls were made with the correct arguments
		self.assertEqual(mock_api_instance.put.call_count, 2)
		actual_put_endpoints = [call.kwargs["endpoint"] for call in mock_api_instance.put.call_args_list]
		actual_put_data = [call.kwargs["data"] for call in mock_api_instance.put.call_args_list]

		expected_put_endpoints = ["products/1", "products/2"]
		expected_data = {"stock_quantity": 15}
		expected_put_data = [expected_data for x in range(2)]
		self.assertEqual(actual_put_endpoints, expected_put_endpoints)
		self.assertEqual(actual_put_data, expected_put_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site_variant(self, mock_wc_api, mock_frappe):
		# Set up a dummy variant item set to sync to a WC site
		variant_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=101, woocommerce_server="woo1.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
			variant_of="parent_item_code",
		)
		mock_frappe.get_doc.side_effect = [
			variant_item,
			frappe._dict(
				woocommerce_servers=[
					frappe._dict(woocommerce_id=100, woocommerce_server="woo1.example.com", enabled=1),
				]
			),
		]

		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(warehouse="Warehouse A", actual_qty=5),
			frappe._dict(warehouse="Warehouse B", actual_qty=10),
		]
		mock_frappe.get_list.return_value = bin_list

		# Set up mock return values
		mock_frappe.get_cached_doc.return_value = frappe._dict(
			woocommerce_server="woo1.example.com",
			enable_sync=1,
			enable_stock_level_synchronisation=1,
			warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
		)

		# Mock out calls to WooCommerce API's
		mock_put_response = Mock()
		mock_put_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.put.return_value = mock_put_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("variant_item_code")

		# Assert that the inventories put calls were made with the correct arguments
		self.assertEqual(mock_api_instance.put.call_count, 1)
		actual_put_endpoint = mock_api_instance.put.call_args.kwargs["endpoint"]
		actual_put_data = mock_api_instance.put.call_args.kwargs["data"]

		expected_put_endpoint = "products/100/variations/101"
		expected_data = {"stock_quantity": 15}
		self.assertEqual(actual_put_endpoint, expected_put_endpoint)
		self.assertEqual(actual_put_data, expected_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe.db.get_all")
	@patch("woocommerce_fusion.tasks.stock_update.frappe.enqueue")
	def test_update_stock_levels_for_all_enabled_items_in_background(self, mock_enqueue, mock_get_all):
		# Set up mock return values
		mock_get_all.side_effect = [
			[_dict({"name": f"Item-1-{x}"}) for x in range(500)],  # First page of results
			[_dict({"name": f"Item-2-{x}"}) for x in range(500)],  # Second page of results
			[],  # No more results, loop should exit
		]

		# Call the function
		update_stock_levels_for_all_enabled_items_in_background()

		# Assertions to check if get_all was called correctly
		self.assertEqual(mock_get_all.call_count, 3)
		expected_calls = [
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=0, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=500, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=1000, page_length=500),
		]
		mock_get_all.assert_has_calls(expected_calls, any_order=True)

		# Assertions to check if enqueue was called correctly
		# This assumes we have 1000 items, based on the pagination logic above.
		self.assertEqual(mock_enqueue.call_count, 1000)
		mock_enqueue.assert_called_with(
			"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			item_code="Item-2-499",  # Here we'd check for the last `item_code` being passed.
		)
