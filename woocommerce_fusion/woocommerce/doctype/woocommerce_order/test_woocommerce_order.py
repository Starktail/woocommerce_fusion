# Copyright (c) 2023, Dirk van der Laarse and Contributors
# See license.txt

import json
from copy import deepcopy
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import frappe
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.utils import API, APIWithRequestLogging
from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_DELIMITER,
	WooCommerceOrder,
	WooCommerceOrderAPI,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
	get_domain_and_id_from_woocommerce_record_name,
)


@patch.object(WooCommerceOrder, "_init_api")
class TestWooCommerceOrder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	def test_get_list_returns_orders_with_name_attribute(self, mock_init_api):
		"""
		Test that get_list returns a list of Orders, each with a 'name' attribute
		"""
		nr_of_orders = 3
		woocommerce_server_url = "http://site1.example.com"

		# Create mock API object
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server_url,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]

		mock_init_api.return_value = mock_api_list

		# Define the mock response from the get method
		mock_get_response = Mock()
		mock_get_response.status_code = 200
		mock_get_response.json.return_value = wc_response_for_list_of_orders(nr_of_orders)
		mock_get_response.headers = {"x-wp-total": nr_of_orders}

		# Set the mock response to be returned when get is called on the mock API
		mock_api_list[0].api.get.return_value = mock_get_response

		# Call the method to be tested
		woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		orders = woocommerce_order.get_list({})

		# Check that the API was initialised
		mock_init_api.assert_called_once()

		# Check that the API was called
		mock_api_list[0].api.get.assert_called_once()

		# Verify that the orders endpoint is called
		self.assertEqual(mock_api_list[0].api.get.call_args.args[0], "orders")

		# Verify that the list of orders have been retrieved
		self.assertEqual(len(orders), nr_of_orders)

		# Verify that a 'name' attribute has been created with value of '[domain]~[id]'
		for order in orders:
			self.assertIsNotNone(order.name)
			expected_name = urlparse(woocommerce_server_url).netloc + WC_ORDER_DELIMITER + str(order.id)
			self.assertEqual(order.name, expected_name)

	def test_get_list_pagination_works(self, mock_init_api):
		"""
		Test that get_list's pagination works as expected
		"""

		# Create mock API object list with 3 WooCommerce servers/API's
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url="http://site1.example.com",
				woocommerce_server="site1.example.com",
			),
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url="http://site2.example.com",
				woocommerce_server="site2.example.com",
			),
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url="http://site3.example.com",
				woocommerce_server="site3.example.com",
			),
		]

		mock_init_api.return_value = mock_api_list

		# Define the mock response from the get method
		order_counts = [10, 20, 30]
		for x, woocommerce_api in enumerate(mock_api_list):
			mock_get_response = Mock()
			mock_get_response.status_code = 200
			nr_of_orders = order_counts[x]
			mock_get_response.json.return_value = wc_response_for_list_of_orders(
				nr_of_orders, woocommerce_api.woocommerce_server_url
			)
			mock_get_response.headers = {"x-wp-total": nr_of_orders}

			# Set the mock response to be returned when get is called on the mock API
			woocommerce_api.api.get.return_value = mock_get_response

		# Parameterize this test for different combinations of 'page_length' and 'start' arguments
		test_parameters = [
			frappe._dict(
				{
					"args": {"page_length": 10, "start": 0},
					"expected_order_counts": (
						10,
						0,
						0,
					),  # expect 10 orders from API 1, and 0 from API 2 and API3
				}
			),
			frappe._dict({"args": {"page_length": 10, "start": 10}, "expected_order_counts": (0, 10, 0)}),
			frappe._dict({"args": {"page_length": 10, "start": 30}, "expected_order_counts": (0, 0, 10)}),
			frappe._dict({"args": {"page_length": 20, "start": 5}, "expected_order_counts": (5, 15, 0)}),
			frappe._dict({"args": {"page_length": 5, "start": 40}, "expected_order_counts": (0, 0, 5)}),
			frappe._dict({"args": {"page_length": 60, "start": 0}, "expected_order_counts": (10, 20, 30)}),
		]
		for param in test_parameters:
			with self.subTest(param=param):
				# Call the method to be tested
				woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})

				orders = woocommerce_order.get_list(param.args)

				# Verify that the list of orders have been retrieved
				self.assertEqual(len(orders), param.args["page_length"])

				# Verify that the orders were combined correctly from the API's
				order_counts_for_api1 = len(
					[
						order
						for order in orders
						if order.woocommerce_server == mock_api_list[0].woocommerce_server
					]
				)
				order_counts_for_api2 = len(
					[
						order
						for order in orders
						if order.woocommerce_server == mock_api_list[1].woocommerce_server
					]
				)
				order_counts_for_api3 = len(
					[
						order
						for order in orders
						if order.woocommerce_server == mock_api_list[2].woocommerce_server
					]
				)
				self.assertEqual(
					(order_counts_for_api1, order_counts_for_api2, order_counts_for_api3),
					param.expected_order_counts,
				)

	def test_load_from_db_initialises_doctype_with_all_values(self, mock_init_api):
		"""
		Test that load_from_db returns an Order
		"""
		order_id = 1
		woocommerce_server_url = "http://site1.example.com"
		woocommerce_server = "site1.example.com"

		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the get method
		mock_get_response = Mock()
		mock_get_response.json.return_value = deepcopy(dummy_wc_order)

		# Set the mock response to be returned when get is called on the mock API
		mock_api_list[0].api.get.return_value = mock_get_response

		# Patch out the __init__ method
		with patch.object(WooCommerceOrder, "__init__", return_value=None) as _mock_init:
			# Patch out the call_super_init method
			with patch.object(WooCommerceOrder, "call_super_init") as mocked_super_call:
				# Patch out the get_additional_order_attributes
				with patch.object(
					WooCommerceOrder, "get_additional_order_attributes"
				) as mock_get_additional_order_attributes:
					# Set the mock_get_additional_order_attributes method to return its argument
					mock_get_additional_order_attributes.side_effect = lambda x: x

					# Instantiate the class
					woocommerce_order = WooCommerceOrder()
					woocommerce_order.doctype = "WooCommerce Order"
					woocommerce_order.name = woocommerce_server + WC_ORDER_DELIMITER + str(order_id)

					# Call load_from_db
					woocommerce_order.load_from_db()

					# Check that super's __init__ was called
					mocked_super_call.assert_called_once()

					# Check that all order fields are valid
					for key, value in mocked_super_call.call_args.args[0].items():
						# Test that Lists and Dicts are in JSON format, except for meta fieds
						meta_data_fields = [
							"modified",
							"woocommerce_server",
							"name",
							"doctype",
							"woocommerce_date_created",
							"woocommerce_date_created_gmt",
							"woocommerce_date_modified",
							"woocommerce_date_modified_gmt",
						]
						if key not in meta_data_fields:
							if isinstance(dummy_wc_order.get(key), dict) or isinstance(
								dummy_wc_order.get(key), list
							):
								self.assertEqual(json.loads(value), dummy_wc_order.get(key))
							else:
								self.assertEqual(value, dummy_wc_order.get(key))

		# Check that the API was initialised
		mock_init_api.assert_called_once()

		# Check that the API was called
		mock_api_list[0].api.get.assert_called_once()

		# Verify that the orders endpoint is called
		self.assertEqual(mock_api_list[0].api.get.call_args.args[0], f"orders/{order_id}")

	def test_db_insert_makes_post_call(self, mock_init_api):
		"""
		Test that db_insert makes a POST call to the WooCommerce API
		"""
		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url="http://site1.example.com",
				woocommerce_server="site1.example.com",
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the post method
		mock_post_response = Mock()
		mock_post_response.status_code = 201
		mock_post_response.json.return_value = {"id": 69, "date_modified": "2020-01-01"}

		# Set the mock response to be returned when POST is called on the mock API
		mock_api_list[0].api.post.return_value = mock_post_response

		# Prepare the mock order data by dumping lists and dicts to json
		mock_order_data = deepcopy(dummy_wc_order)
		for key, _value in mock_order_data.items():
			if isinstance(mock_order_data[key], dict) or isinstance(mock_order_data[key], list):
				mock_order_data[key] = json.dumps(dummy_wc_order[key])

		# Call db_insert
		woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		woocommerce_order.status = "Hello World"
		woocommerce_order.woocommerce_server = "site1.example.com"
		woocommerce_order.db_insert()

		# Check that the API was initialised
		mock_init_api.assert_called_once()

		# Check that the API was called
		mock_api_list[0].api.post.assert_called_once()

		# Verify that the orders endpoint is called
		self.assertEqual(mock_api_list[0].api.post.call_args.args[0], "orders")

		# Verify that an attribute is passed to the API
		self.assertTrue("status" in mock_api_list[0].api.post.call_args.kwargs["data"])
		self.assertEqual(mock_api_list[0].api.post.call_args.kwargs["data"]["status"], "Hello World")

	def test_db_insert_with_failed_post_call_throws_error(self, mock_init_api):
		"""
		Test that db_insert with a failed POST call throws an error
		"""
		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url="http://site1.example.com",
				woocommerce_server="site1.example.com",
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the post method
		mock_post_response = Mock()
		mock_post_response.status_code = 400

		# Set the mock response to be returned when POST is called on the mock API
		mock_api_list[0].api.post.return_value = mock_post_response

		# Prepare the mock order data by dumping lists and dicts to json
		mock_order_data = deepcopy(dummy_wc_order)
		for key, _value in mock_order_data.items():
			if isinstance(mock_order_data[key], dict) or isinstance(mock_order_data[key], list):
				mock_order_data[key] = json.dumps(dummy_wc_order[key])

		# Call db_insert
		woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		woocommerce_order.woocommerce_server_url = "http://site1.example.com"

		# Verify that an Exception is thrown
		with self.assertRaises(Exception) as context:
			woocommerce_order.db_insert()

		# Verify that the Exception is a Validation Error
		self.assertEqual("ValidationError", context.exception.__class__.__name__)

	@patch(
		"woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order.WooCommerceOrder.update_shipment_tracking"
	)
	def test_db_update_makes_put_call(self, mock_update_shipment_tracking, mock_init_api):
		"""
		Test that db_update makes a PUT call to the WooCommerce API
		"""
		order_id = 1
		woocommerce_server_url = "http://site1.example.com"

		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server_url,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the put method
		mock_put_response = Mock()
		mock_put_response.status_code = 200
		mock_put_response.json.return_value = {"date_modified": "2024-01-01"}

		# Set the mock response to be returned when PUT is called on the mock API
		mock_api_list[0].api.put.return_value = mock_put_response

		# Prepare the mock order data by dumping lists and dicts to json
		mock_order_data = deepcopy(dummy_wc_order)
		for key, _value in mock_order_data.items():
			if isinstance(mock_order_data[key], dict) or isinstance(mock_order_data[key], list):
				mock_order_data[key] = json.dumps(dummy_wc_order[key])

		# Call db_update
		woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		woocommerce_order.name = woocommerce_server_url + WC_ORDER_DELIMITER + str(order_id)
		woocommerce_order._doc_before_save = deepcopy(woocommerce_order)
		woocommerce_order.status = "Hello World"
		woocommerce_order.db_update()

		# Check that the API was initialised
		mock_init_api.assert_called_once()

		# Check that the API was called
		mock_api_list[0].api.put.assert_called_once()

		# Verify that the orders endpoint is called
		self.assertEqual(mock_api_list[0].api.put.call_args.args[0], f"orders/{order_id}")

		# Verify that an attribute is passed to the API
		self.assertTrue("status" in mock_api_list[0].api.put.call_args.kwargs["data"])
		self.assertEqual(mock_api_list[0].api.put.call_args.kwargs["data"]["status"], "Hello World")

	def test_get_additional_order_attributes_makes_api_get(self, mock_init_api):
		"""
		Test that the get_additional_order_attributes method makes an API call
		"""
		order_id = 1
		woocommerce_server_url = "http://site1.example.com"
		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server_url,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the get method
		mock_get_response = Mock()
		mock_get_response.json.return_value = None

		# Set the mock response to be returned when get is called on the mock API
		mock_api_list[0].api.get.return_value = mock_get_response

		# Patch out the __init__ method and set the required fields
		with patch.object(WooCommerceOrder, "__init__", return_value=None) as _mock_init:
			woocommerce_order = WooCommerceOrder()
			woocommerce_order.name = woocommerce_server_url + WC_ORDER_DELIMITER + str(order_id)
			woocommerce_order.current_wc_api = mock_api_list[0]
			woocommerce_order.get_additional_order_attributes({})

		# Check that the API was called
		mock_api_list[0].api.get.assert_called_once()

		# Verify that the shipment-trackings endpoint is called
		self.assertEqual(mock_api_list[0].api.get.call_args.args[0], f"orders/{order_id}/shipment-trackings")

	def test_update_shipment_tracking_makes_api_post_when_shipment_trackings_changes(self, mock_init_api):
		"""
		Test that the update_shipment_tracking method makes an API POST call
		"""
		order_id = 1
		woocommerce_server_url = "http://site1.example.com"
		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server_url,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the post method
		mock_post_response = Mock()
		mock_post_response.status_code = 201

		# Set the mock response to be returned when post is called on the mock API
		mock_api_list[0].api.post.return_value = mock_post_response

		# Patch out the __init__ method and set the required fields
		with patch.object(WooCommerceOrder, "__init__", return_value=None) as _mock_init:
			woocommerce_order = WooCommerceOrder()
			woocommerce_order.init_api()
			woocommerce_order.name = woocommerce_server_url + WC_ORDER_DELIMITER + str(order_id)
			woocommerce_order.shipment_trackings = json.dumps([{"foo": "bar"}])
			woocommerce_order._doc_before_save = frappe._dict(
				{"shipment_trackings": json.dumps([{"foo": "baz"}])}
			)
			woocommerce_order.update_shipment_tracking()

		# Check that the API was called
		mock_api_list[0].api.post.assert_called_once()

		# Verify that the shipment-trackings endpoint is called
		self.assertEqual(
			mock_api_list[0].api.post.call_args.args[0], f"orders/{order_id}/shipment-trackings/"
		)

	def test_update_shipment_tracking_does_not_make_api_post_when_shipment_trackings_is_unchanged(
		self, mock_init_api
	):
		"""
		Test that the update_shipment_tracking method does not make an API POST call when
		shipment_trackings is unchanged
		"""
		order_id = 1
		woocommerce_server_url = "http://site1.example.com"
		# Setup mock API
		mock_api_list = [
			WooCommerceOrderAPI(
				api=Mock(),
				woocommerce_server_url=woocommerce_server_url,
				woocommerce_server=woocommerce_server_url,
				wc_plugin_advanced_shipment_tracking=1,
			)
		]
		mock_init_api.return_value = mock_api_list

		# Define the mock response from the post method
		mock_post_response = Mock()
		mock_post_response.status_code = 201

		# Set the mock response to be returned when post is called on the mock API
		mock_api_list[0].api.post.return_value = mock_post_response

		# Patch out the __init__ method and set the required fields
		with patch.object(WooCommerceOrder, "__init__", return_value=None) as _mock_init:
			woocommerce_order = WooCommerceOrder()
			woocommerce_order.init_api()
			woocommerce_order.name = woocommerce_server_url + WC_ORDER_DELIMITER + str(order_id)
			woocommerce_order.shipment_trackings = json.dumps([{"foo": "bar"}])
			woocommerce_order._doc_before_save = frappe._dict(
				{"shipment_trackings": json.dumps([{"foo": "bar"}])}
			)
			woocommerce_order.update_shipment_tracking()

		# Check that the API was not called
		mock_api_list[0].api.post.assert_not_called()

	def test_generate_woocommerce_record_name_from_domain_and_id(self, mock_init_api):
		"""
		Test that generate_woocommerce_record_name_from_domain_and_id function performs as expected
		"""
		domain = "site1.example.com"
		order_id = 4
		delimiter = "|"
		result = generate_woocommerce_record_name_from_domain_and_id(domain, order_id, delimiter)
		self.assertEqual(result, "site1.example.com|4")

	def test_get_domain_and_id_from_woocommerce_record_name(self, mock_init_api):
		"""
		Test that get_domain_and_id_from_woocommerce_record_name function performs as expected
		"""
		delimiter = "$"
		name = "site2.example.com$3"
		domain, order_id = get_domain_and_id_from_woocommerce_record_name(name, delimiter)
		self.assertEqual(domain, "site2.example.com")
		self.assertEqual(order_id, 3)


def wc_response_for_list_of_orders(nr_of_orders=5, site="example.com"):
	"""
	Generate a dummy list of orders as if it was returned from the WooCommerce API
	"""
	dummy_wc_order_instance = deepcopy(dummy_wc_order)
	dummy_wc_order_instance["woocommerce_server"] = site
	return [frappe._dict(dummy_wc_order_instance) for i in range(nr_of_orders)]


dummy_wc_order = {
	"billing": {
		"address_1": "1",
		"address_2": "2",
		"city": "a",
		"company": "b",
		"country": "c",
		"first_name": "d",
		"last_name": "e",
		"phone": "f",
		"postcode": "g",
		"state": "h",
		"email": "i",
	},
	"cart_hash": "",
	"cart_tax": "0.00",
	"coupon_lines": [],
	"created_via": "admin",
	"currency": "ZAR",
	"currency_symbol": "R",
	"customer_id": 0,
	"customer_ip_address": "",
	"customer_note": "",
	"customer_user_agent": "",
	"date_completed": None,
	"date_completed_gmt": None,
	"date_created": "2023-05-20T13:12:23",
	"date_created_gmt": "2023-05-20T13:12:23",
	"date_modified": "2023-05-20T13:12:39",
	"date_modified_gmt": "2023-05-20T13:12:39",
	"date_paid": "2023-05-20T13:12:39",
	"date_paid_gmt": "2023-05-20T13:12:39",
	"discount_tax": "0.00",
	"discount_total": "0.00",
	"fee_lines": [],
	"id": 15,
	"is_editable": False,
	"line_items": [
		{
			"id": 2,
			"image": {
				"id": "12",
				"src": "https://wootest.mysite.com/wp-content/uploads/2023/05/hoodie-with-logo-2.jpg",
			},
			"meta_data": [],
			"name": "Hoodie",
			"parent_name": None,
			"price": 45,
			"product_id": 13,
			"quantity": 1,
			"sku": "",
			"subtotal": "45.00",
			"subtotal_tax": "0.00",
			"tax_class": "",
			"taxes": [],
			"total": "45.00",
			"total_tax": "0.00",
			"variation_id": 0,
		}
	],
	"meta_data": [],
	"needs_payment": False,
	"needs_processing": True,
	"number": "15",
	"order_key": "wc_order_YpxrBDm0nyUkk",
	"parent_id": 0,
	"payment_method": "",
	"payment_method_title": "",
	"payment_url": "https://wootest.mysite.com/checkout/order-pay/15/?pay_for_order=true&key=wc_order_YpxrBDm0nyUkk",
	"prices_include_tax": False,
	"refunds": [],
	"shipping": {
		"address_1": "1",
		"address_2": "2",
		"city": "a",
		"company": "b",
		"country": "c",
		"first_name": "d",
		"last_name": "e",
		"phone": "f",
		"postcode": "g",
		"state": "h",
	},
	"shipping_lines": [],
	"shipping_tax": "0.00",
	"shipping_total": "0.00",
	"status": "processing",
	"tax_lines": [],
	"total": "45.00",
	"total_tax": "0.00",
	"transaction_id": "",
	"version": "7.7.0",
}


class TestAPIWithRequestLogging(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	def setUp(self):
		self.api = APIWithRequestLogging(url="foo", consumer_key="bar", consumer_secret="baz")

	@patch("woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order.frappe.enqueue")
	def test_request_success(self, mock_enqueue):
		# Mock the parent class's _API__request method
		with patch.object(API, "_API__request", return_value="success_response") as mock_super:
			# Make a request
			response = self.api._API__request("GET", "test_endpoint", {"key": "value"})

			# Verify the parent method was called correctly
			mock_super.assert_called_once_with("GET", "test_endpoint", {"key": "value"}, None)

			# Verify the response is correct
			self.assertEqual(response, "success_response")
