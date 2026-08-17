from unittest.mock import patch

import frappe
from erpnext import get_default_company
from erpnext.selling.doctype.sales_order.sales_order import update_status
from erpnext.stock.doctype.item.test_item import create_item
from parameterized import parameterized

from woocommerce_fusion.tasks.sync_sales_orders import (
	get_addresses_linking_to,
	get_tax_inc_price_for_woocommerce_line_item,
	run_sales_order_sync,
)
from woocommerce_fusion.tasks.test_integration_helpers import (
	TestIntegrationWooCommerce,
	create_gl_account_for_shipping_tax,
	create_shipping_rule,
	default_warehouse,
	get_woocommerce_server,
)

BATCH_MODES = [("single_call", False), ("batch_api", True)]


@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.log_error")
class TestIntegrationWooCommerceSync(TestIntegrationWooCommerce):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	def setUp(self):
		super().setUp()
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 1
		wc_server.enable_payments_sync = 1
		wc_server.enable_shipping_methods_sync = 0
		wc_server.enable_so_status_sync = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.order_line_item_field_map = []
		wc_server.item_field_map = []
		wc_server.save()

	def _create_sales_taxes_and_charges_template(
		self, wc_server, rate: float, included_in_rate: bool = False
	) -> str:
		taxes_and_charges_template = None
		title = f"_Test Sales Taxes and Charges Template for Woo {rate}-{included_in_rate}"
		if frappe.db.exists("Sales Taxes and Charges Template", {"title": title}):
			taxes_and_charges_template = frappe.get_doc("Sales Taxes and Charges Template", {"title": title})
		else:
			taxes_and_charges_template = frappe.get_doc(
				{
					"company": wc_server.company,
					"doctype": "Sales Taxes and Charges Template",
					"taxes": [
						{
							"account_head": wc_server.tax_account,
							"charge_type": "On Net Total",
							"description": "VAT",
							"doctype": "Sales Taxes and Charges",
							"parentfield": "taxes",
							"rate": rate,
							"included_in_print_rate": included_in_rate,
						}
					],
					"title": title,
				}
			).insert()
		return taxes_and_charges_template.name

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_sales_order(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales order when there is a new
		WooCommerce order.

		Assumes that the Wordpress Site we're testing against has:
		- Tax enabled
		- Sales prices include tax
		"""
		self._set_batch_mode(batch_enabled)
		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, customer_note="The big brown fox"
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct payment method title on Sales Order
		self.assertEqual(sales_order.woocommerce_payment_method, "Doge")

		# Expect correct items in Sales Order
		self.assertEqual(sales_order.items[0].rate, 8.7)
		self.assertEqual(sales_order.items[0].qty, 1)

		# Expect correct tax rows in Sales Order
		self.assertEqual(sales_order.taxes[0].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[0].rate, 0)
		self.assertEqual(sales_order.taxes[0].tax_amount, 1.3)
		self.assertEqual(sales_order.taxes[0].total, 10)
		self.assertEqual(sales_order.taxes[0].account_head, "VAT - SC")

		# Expect correct customer note
		self.assertEqual(sales_order.custom_woocommerce_customer_note, "The big brown fox")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_sales_order_in_usd(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales order in the correct currency
		when currency is different from base currency

		Assumes that the Wordpress Site we're testing against has:
		- Tax enabled
		- Sales prices include tax
		"""
		self._set_batch_mode(batch_enabled)
		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, currency="USD"
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_currency = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "currency")
		self.assertIsNotNone(sales_order_currency)

		# Expect correct currency in Sales Order
		self.assertEqual(sales_order_currency, "USD")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(
		[
			(False, True, 50, 13.04, 26.08, 100),
			(True, True, 50, 13.04, 26.08, 100),
			(False, False, 43.48, 13.04, 26.08, 100),
			(True, False, 43.48, 13.04, 26.08, 100),
		]
	)
	def test_sync_create_new_sales_order_with_tax_template_and_shipping(
		self,
		mock_log_error,
		batch_enabled,
		included_in_rate,
		expected_item_rate,
		expected_tax_amount,
		expected_base_tax_amount,
		expected_total_amount,
	):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales order with a Tax Template
		for a new WooCommerce order when a Sales Taxes and Charges template is set.

		Assumes that the Wordpress Site we're testing against has:
		- Tax enabled, at a rate of 15%
		- Sales prices include tax

		Parameterisation: (batch_enabled, included_in_rate, expected item.rate, expected tax_amount, expected total_tax_amount)
		1. Tax Template that includes tax so Item Rate should include Tax (=50), and tax should be 50 x 2 x 15/115 = 13.04
		2. Tax Template that excludes tax so Item Rate should exclude Tax (=43.48), and tax should be 50 x 2 x 15/115 = 13.04

		"""
		self._set_batch_mode(batch_enabled)

		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		template_name = self._create_sales_taxes_and_charges_template(
			wc_server, rate=15, included_in_rate=included_in_rate
		)
		wc_server.use_actual_tax_type = 0
		wc_server.sales_taxes_and_charges_template = template_name
		# On Frappe v16 the shipping tax account must differ from the template's VAT account,
		# otherwise ERPNext's account-keyed tax map zeroes the calculated VAT (see
		# WooCommerceServer.validate_tax_account_uniqueness).
		wc_server.f_n_f_tax_account = create_gl_account_for_shipping_tax()
		wc_server.flags.ignore_mandatory = True
		wc_server.shipping_rule_map = []
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=50, item_qty=2, shipping_method_id="flat_rate"
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct payment method title on Sales Order
		self.assertEqual(sales_order.woocommerce_payment_method, "Doge")

		# Expect correct items in Sales Order
		self.assertEqual(sales_order.items[0].rate, expected_item_rate)  # should show tax inclusive price
		self.assertEqual(sales_order.items[0].qty, 2)

		# Expect correct tax rows in Sales Order
		self.assertEqual(sales_order.taxes[0].charge_type, "On Net Total")
		self.assertEqual(sales_order.taxes[0].rate, 15)
		self.assertEqual(sales_order.taxes[0].tax_amount, expected_tax_amount)
		self.assertEqual(sales_order.taxes[0].base_tax_amount, expected_base_tax_amount)
		self.assertEqual(sales_order.taxes[0].total, expected_total_amount)
		self.assertEqual(sales_order.taxes[0].account_head, "VAT - SC")

		# Expect correct tax rows in Sales Order
		self.assertEqual(sales_order.taxes[-1].account_head, wc_server.f_n_f_account)
		self.assertEqual(sales_order.taxes[-1].tax_amount, 10)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_sales_order_and_pe(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales orders and a Payment Entry
		when there is a new fully paid WooCommerce orders.
		"""
		self._set_batch_mode(batch_enabled)
		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(set_paid=True)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order and linked Payment Entry in ERPNext
		sales_order_payment_entry = frappe.get_value(
			"Sales Order", {"woocommerce_id": wc_order_id}, "woocommerce_payment_entry"
		)
		self.assertIsNotNone(sales_order_payment_entry)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_draft_sales_order(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Draft Sales order without errors
		when the submit_sales_orders setting is set to 0
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.enable_payments_sync = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(set_paid=True)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)

		# Teardown
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_link_payment_entry_after_so_submitted(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a linked Payment Entry if there are no linked
		PE's on a now-submitted Sales Order
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(set_paid=True)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect no linked Payment Entry
		sales_order = frappe.get_doc("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNone(sales_order.woocommerce_payment_entry)
		self.assertEqual(sales_order.custom_attempted_woocommerce_auto_payment_entry, 0)

		# Action: Submit the Sales Order
		sales_order.submit()

		# Run synchronisation again
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect linked Payment Entry this time
		sales_order = frappe.get_doc("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNotNone(sales_order.woocommerce_payment_entry)
		self.assertEqual(sales_order.custom_attempted_woocommerce_auto_payment_entry, 1)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_sales_order_with_mapped_field(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales order when there is a new
		WooCommerce order, and that mapped fields are taken into account
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		# Map Erpnext Sales Order Item description to WC Order Line Meta Data with key 'custom_field'
		wc_server.order_line_item_field_map = []
		row = wc_server.append("order_line_item_field_map")
		row.erpnext_field_name = "description | Description"
		row.woocommerce_field_name = "$.meta_data[?(@.key=='custom_field')].value"
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=1,
			customer_note="The big brown fox",
			line_item_metadata=[{"key": "custom_field", "value": "custom_value"}],
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect value in mapped field in Sales Order Item
		self.assertEqual(sales_order.items[0].description, "custom_value")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_updates_woocommerce_order(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method updates a WooCommerce Order
		with changed fields from Sales Order
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.enable_payments_sync = 0
		wc_server.sync_so_items_to_wc = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=3
		)

		# Create an additional item in WooCommerce and in ERPNext, and link them
		wc_product_id = self.post_woocommerce_product(product_name="ADDITIONAL_ITEM", regular_price=20)
		# Create the same product in ERPNext and link it
		item = create_item(
			"ADDITIONAL_ITEM", valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# In ERPNext, change quantity of first item, and add an additional item
		sales_order.items[0].qty = 2
		sales_order.append(
			"items",
			{
				"item_code": item.name,
				"delivery_date": sales_order.delivery_date,
				"qty": 1,
				"rate": 20,
				"warehouse": "Stores - SC",
			},
		)
		sales_order.save()
		sales_order.submit()

		# Run synchronisation again, to sync the Sales Order changes
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect WooCommerce Order to have updated items
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		wc_line_items = wc_order.get("line_items")
		self.assertEqual(wc_line_items[0].get("quantity"), 2)
		self.assertEqual(wc_line_items[1].get("name"), item.name)
		self.assertEqual(wc_line_items[1].get("quantity"), 1)
		self.assertEqual(get_tax_inc_price_for_woocommerce_line_item(wc_line_items[1]), 20)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_updates_woocommerce_order_with_mapped_field(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method updates a WooCommerce Order
		with changed fields from Sales Order, and that mapped fields are taken into account
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.enable_payments_sync = 0
		wc_server.sync_so_items_to_wc = 1
		wc_server.flags.ignore_mandatory = True

		# Map Erpnext Sales Order Item description to WC Order Line Meta Data with key 'custom_field'
		wc_server.order_line_item_field_map = []
		row = wc_server.append("order_line_item_field_map")
		row.erpnext_field_name = "description | Description"
		row.woocommerce_field_name = "$.meta_data[?(@.key=='custom_field')].value"

		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=3,
			line_item_metadata=[{"key": "custom_field", "value": "custom_value"}],
		)

		# Create an additional item in WooCommerce and in ERPNext, and link them
		wc_product_id = self.post_woocommerce_product(product_name="ADDITIONAL_ITEM", regular_price=20)
		# Create the same product in ERPNext and link it
		item = create_item(
			"ADDITIONAL_ITEM", valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product_id
		row.woocommerce_server = get_woocommerce_server(self.wc_url).name
		item.save()

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# In ERPNext, change description (mapped field) of first item, and add an additional item
		sales_order.items[0].description = "custom_value_from_erpnext"
		sales_order.save()
		sales_order.submit()

		# Run synchronisation again, to sync the Sales Order changes
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect WooCommerce Order to have updated items
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		wc_line_items = wc_order.get("line_items")
		self.assertEqual(wc_line_items[0]["meta_data"][0]["value"], "custom_value_from_erpnext")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_uses_dummy_item_for_deleted_item(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method uses a placeholder item when
		synchronising with a WooCommerce Order that has a deleted item
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.enable_payments_sync = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(set_paid=True)

		# Get the WooCommerce Product ID and delete the Product
		wc_order = self.get_woocommerce_order(wc_order_id)
		wc_product_id = wc_order["line_items"][0]["product_id"]
		self.delete_woocommerce_product(wc_product_id)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect placeholder item
		self.assertEqual(sales_order.items[0].item_code, "DELETED_WOOCOMMERCE_PRODUCT")

		# Teardown
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_use_same_customer_for_multiple_orders(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method does not create a duplicate Customer when the same
		customer places another order
		"""
		self._set_batch_mode(batch_enabled)
		same_customer_email = "same@customer.com"

		# Create a new order in WooCommerce
		wc_order_id_first, wc_order_name_first = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, customer_id=1, email=same_customer_email
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name_first)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_orders = frappe.get_all(
			"Sales Order", filters={"woocommerce_id": wc_order_id_first}, fields=["name", "customer"]
		)
		self.assertEqual(len(sales_orders), 1)

		# Expect newly created Customer in ERPNext
		customer_name = frappe.get_value("Customer", {"woocommerce_identifier": same_customer_email}, "name")
		self.assertIsNotNone(customer_name)

		# Expect single Address for customer, marked as preferred billing and shipping address
		addresses = get_addresses_linking_to("Customer", customer_name)
		self.assertEqual(len(addresses), 1)
		address_doc = frappe.get_doc("Address", addresses[0].name)
		self.assertEqual(address_doc.is_primary_address, 1)
		self.assertEqual(address_doc.is_shipping_address, 1)

		# Place another order from the same customer with a changed address
		wc_order_id_second, wc_order_name_second = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=2,
			customer_id=1,
			email=same_customer_email,
			address_1="New New Street 420",
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name_second)
		self._flush_if_batch()

		# Expect that the order has been allocated to the initial customer
		_sales_order_name, sales_order_customer = frappe.get_value(
			"Sales Order", {"woocommerce_id": wc_order_id_second}, ["name", "customer"]
		)
		self.assertEqual(sales_order_customer, customer_name)

		# Expect an updated address
		addresses = get_addresses_linking_to("Customer", customer_name)
		address_doc = frappe.get_doc("Address", addresses[0].name)
		self.assertEqual(address_doc.address_line1, "New New Street 420")

		# Delete orders in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id_first)
		self.delete_woocommerce_order(wc_order_id=wc_order_id_second)

	@parameterized.expand(BATCH_MODES)
	def test_sync_links_shipping_rule(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method links a Shipping Rule on the created
		Sales order when Shipping Rule Sync is enabled and a mapping exists.
		"""
		self._set_batch_mode(batch_enabled)
		# Setup: Create a Shipping Rule
		sr = create_shipping_rule(shipping_rule_type="Selling", shipping_rule_name="Woo Shipping")

		# Setup: Map WooCommerce Shipping Method to ERPNext Shipping Rule
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_shipping_methods_sync = 1
		wc_server.shipping_rule_map = []
		wc_server.append(
			"shipping_rule_map",
			{"wc_shipping_method_id": "flat_rate", "shipping_rule": sr.name},
		)
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, shipping_method_id="flat_rate"
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct Shipping Rule on Sales Order
		self.assertEqual(sales_order.shipping_rule, sr.name)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_with_shipping_rule_and_tax_template(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method links a Shipping Rule on the created
		Sales order when Shipping Rule Sync is enabled and a mapping exists, and handles
		a Sales Tax Templates at the same without duplicating shipping charges
		"""
		self._set_batch_mode(batch_enabled)
		# Setup: Create a Shipping Rule
		sr = create_shipping_rule(shipping_rule_type="Selling", shipping_rule_name="Woo Shipping")

		# Setup: Map WooCommerce Shipping Method to ERPNext Shipping Rule
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_shipping_methods_sync = 1
		wc_server.shipping_rule_map = []
		wc_server.append(
			"shipping_rule_map",
			{"wc_shipping_method_id": "flat_rate", "shipping_rule": sr.name},
		)

		# Setup: Tax Template
		template_name = self._create_sales_taxes_and_charges_template(
			wc_server, rate=15, included_in_rate=False
		)
		wc_server.use_actual_tax_type = 0
		wc_server.sales_taxes_and_charges_template = template_name
		# v16 requires a distinct shipping tax account when a tax template is used (see
		# WooCommerceServer.validate_tax_account_uniqueness).
		wc_server.f_n_f_tax_account = create_gl_account_for_shipping_tax()
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, shipping_method_id="flat_rate"
		)

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct Shipping Rule on Sales Order
		self.assertEqual(sales_order.shipping_rule, sr.name)

		# Expect correct tax rows in Sales Order
		self.assertEqual(sales_order.taxes[0].charge_type, "On Net Total")
		self.assertEqual(sales_order.taxes[0].rate, 15)
		self.assertEqual(sales_order.taxes[0].account_head, "VAT - SC")

		# Expect two charge rows in Sales Order, the first is the VAT tax row, and the second the 'Woo Shipping' row from the Shipping Rule
		self.assertEqual(len(sales_order.taxes), 2)
		self.assertEqual(sales_order.taxes[1].description, "Woo Shipping")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.enqueue")
	def test_sync_updates_woocommerce_order_status(self, _name, batch_enabled, mock_enqueue, mock_log_error):
		"""
		Test that the Sales Order Synchronisation method updates a WooCommerce Order's status
		with the correct mapped value if auto status sync is enabled
		"""
		self._set_batch_mode(batch_enabled)

		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 1
		wc_server.enable_payments_sync = 0
		wc_server.enable_so_status_sync = 1
		wc_server.sales_order_status_map = []
		wc_server.append(
			"sales_order_status_map",
			{
				"erpnext_sales_order_status": "On Hold",
				"woocommerce_sales_order_status": "On hold",
			},
		)
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=3
		)

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# In ERPNext, change order status
		sales_order.update_status("On Hold")

		# Run synchronisation again, to sync the Sales Order changes
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect WooCommerce Order to have updated status
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		self.assertEqual(wc_order["status"], "on-hold")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.enqueue")
	def test_sync_does_not_update_woocommerce_order_status_when_disabled(
		self, _name, batch_enabled, mock_enqueue, mock_log_error
	):
		"""
		When Sales Order Status Sync is DISABLED, syncing
		an ERPNext Sales Order that is newer than its WooCommerce Order (e.g. after submitting it)
		must not change the linked WooCommerce Order's status.
		"""
		self._set_batch_mode(batch_enabled)

		# Setup: status sync disabled, but a status map is present, so the only thing preventing
		# an outbound status push is the disabled flag
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.submit_sales_orders = 0
		wc_server.enable_payments_sync = 0
		wc_server.enable_so_status_sync = 0
		wc_server.sales_order_status_map = []
		wc_server.append(
			"sales_order_status_map",
			{
				"erpnext_sales_order_status": "On Hold",
				"woocommerce_sales_order_status": "On hold",
			},
		)
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce and sync it to ERPNext
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=3
		)
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)

		# In WooCommerce change the order's status
		self.update_woocommerce_order_status(wc_order_id, "completed")

		# Submit the ERPNext Sales Order so it becomes newer than the WooCommerce Order, routing
		# the next sync through the outbound (ERPNext -> WooCommerce) path
		sales_order = frappe.get_doc("Sales Order", sales_order_name)
		sales_order.submit()

		# Run synchronisation again
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect the WooCommerce Order status to be unchanged (not reverted by ERPNext)
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		self.assertEqual(wc_order["status"], "completed")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_so_items_to_wc_preserves_metadata(self, mock_log_error, _name, batch_enabled):
		"""
		Test that when 'sync_so_items_to_wc' is enabled, changes to ERPNext Sales Order
		are synced to WooCommerce Order while preserving metadata on the WooCommerce Order Line Items.
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.sync_so_items_to_wc = 1
		wc_server.submit_sales_orders = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce with metadata
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=1,
			line_item_metadata=[{"key": "custom_field", "value": "custom_value"}],
		)

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Change quantity of the item in ERPNext Sales Order
		sales_order.items[0].qty = 2
		sales_order.save()
		sales_order.submit()

		# Run synchronisation again, to sync the Sales Order changes
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		# Expect WooCommerce Order to have updated items and preserved metadata
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		wc_line_items = wc_order.get("line_items")
		self.assertEqual(wc_line_items[0].get("quantity"), 2)
		self.assertEqual(len(wc_line_items[0]["meta_data"]), 1)
		self.assertEqual(wc_line_items[0]["meta_data"][0]["key"], "custom_field")
		self.assertEqual(wc_line_items[0]["meta_data"][0]["value"], "custom_value")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_so_items_to_wc_with_structured_line_item_metadata(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that a line item carrying structured metadata can still be pushed back to WooCommerce.

		WooCommerce stores an object in a meta `value` and then reports the same object as that meta's
		`display_value`, but its order schema declares `display_value` a string and rejects the whole
		request with a 400 when one is sent back. Plugins do keep structured data there - Shipping Label
		Wizard's `_slw_data` - and line items are carried over from the order as fetched, so without
		encoding the value the order could never be updated again.
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.sync_so_items_to_wc = 1
		wc_server.submit_sales_orders = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce with an object as its metadata value
		slw_data = {"box": 1, "labels": ["a", "b"]}
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=1,
			line_item_metadata=[{"key": "_slw_data", "value": slw_data}],
		)

		# Assert the premise of this test: WooCommerce reports the object as the display_value too
		posted_order = self.get_woocommerce_order(order_id=wc_order_id)
		posted_meta = posted_order["line_items"][0]["meta_data"][0]
		self.assertEqual(posted_meta["value"], slw_data)
		self.assertNotIsInstance(
			posted_meta["display_value"],
			str,
			"WooCommerce no longer reports a structured display_value, so this test proves nothing",
		)

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()
		mock_log_error.assert_not_called()

		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Change quantity of the item in ERPNext Sales Order
		sales_order.items[0].qty = 2
		sales_order.save()
		sales_order.submit()

		# Run synchronisation again, to sync the Sales Order changes
		run_sales_order_sync(sales_order_name=sales_order.name)
		self._flush_if_batch()
		# A rejected PUT is logged rather than raised, so this is what catches the 400
		mock_log_error.assert_not_called()

		# Expect the quantity to have gone through, with the structured metadata intact
		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		wc_line_items = wc_order.get("line_items")
		self.assertEqual(wc_line_items[0].get("quantity"), 2)
		self.assertEqual(wc_line_items[0]["meta_data"][0]["key"], "_slw_data")
		self.assertEqual(wc_line_items[0]["meta_data"][0]["value"], slw_data)

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_sync_so_with_coupon(self, mock_log_error, _name, batch_enabled):
		"""
		Test that the Sales Order Synchronisation method creates a new Sales order when there is a new
		WooCommerce order, and that coupons are taken into account

		Assumes that the Wordpress Site we're testing against has:
		- Tax enabled
		- Sales prices include tax
		"""
		self._set_batch_mode(batch_enabled)
		# Create a new coupon in WooCommerce
		coupon_code = f"10off_{frappe.generate_hash()}"
		_coupon_id = self.post_woocommerce_coupon(coupon_code=coupon_code, percent_discount=10)

		# Create a new order in WooCommerce
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge", item_price=10, item_qty=1, coupon_code=coupon_code
		)
		# wc_order_id, wc_order_name = self.post_woocommerce_order(
		# 	payment_method_title="Doge", item_price=10, item_qty=1
		# )

		# Run synchronisation
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id})
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct payment method title on Sales Order
		self.assertEqual(sales_order.woocommerce_payment_method, "Doge")

		# Expect correct items in Sales Order
		self.assertEqual(sales_order.items[0].rate, 7.83)  # 8.7 - 10% coupon = 7.83
		self.assertEqual(sales_order.items[0].qty, 1)

		# Expect correct tax rows in Sales Order
		self.assertEqual(sales_order.taxes[0].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[0].rate, 0)
		self.assertEqual(sales_order.taxes[0].tax_amount, 1.17)  # 1.3 - 10% coupon = 1.17
		self.assertEqual(sales_order.taxes[0].total, 9)  # 10 - 10% coupon = 9
		self.assertEqual(sales_order.taxes[0].account_head, "VAT - SC")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_order_fee_lines_are_synced_when_enabled(self, mock_log_error, _name, batch_enabled):
		"""
		Test that Order Fee Lines are synchronised when enabled
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_order_fees_sync = 1
		wc_server.account_for_order_fee_lines = "Sales Expenses - SC"
		wc_server.account_for_negative_order_fee_lines = "Marketing Expenses - SC"
		wc_server.tax_account_for_order_fee_lines = "VAT - SC"
		wc_server.submit_sales_orders = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce with fee lines
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=10,
			item_qty=1,
			fee_lines=[
				{
					"name": "Local Pickup Fee",
					"tax_class": "",
					"tax_status": "taxable",
					"amount": "30",
					"total": "30.00",
					"total_tax": "4.50",
					"taxes": [{"id": 1, "total": "4.50", "subtotal": ""}],
					"meta_data": [],
				}
			],
		)

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct taxes and charges row for fee in Sales Order
		self.assertEqual(sales_order.taxes[-2].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[-2].rate, 0)
		self.assertEqual(sales_order.taxes[-2].tax_amount, 30)
		self.assertEqual(sales_order.taxes[-2].account_head, "Sales Expenses - SC")
		self.assertEqual(sales_order.taxes[-2].description, "Local Pickup Fee")

		# Expect correct taxes and charges row for fee tax in Sales Order
		self.assertEqual(sales_order.taxes[-1].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[-1].rate, 0)
		self.assertEqual(sales_order.taxes[-1].tax_amount, 4.5)
		self.assertEqual(sales_order.taxes[-1].account_head, "VAT - SC")

		# Delete order in WooCommerce
		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	@parameterized.expand(BATCH_MODES)
	def test_order_negative_fee_lines_are_synced_when_enabled(self, mock_log_error, _name, batch_enabled):
		"""
		Test that Negative Order Fee Lines are synchronised when enabled
		"""
		self._set_batch_mode(batch_enabled)
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_order_fees_sync = 1
		wc_server.account_for_order_fee_lines = "Sales Expenses - SC"
		wc_server.account_for_negative_order_fee_lines = "Marketing Expenses - SC"
		wc_server.tax_account_for_order_fee_lines = "VAT - SC"
		wc_server.submit_sales_orders = 0
		wc_server.flags.ignore_mandatory = True
		wc_server.save()

		# Create a new order in WooCommerce with fee lines
		wc_order_id, wc_order_name = self.post_woocommerce_order(
			payment_method_title="Doge",
			item_price=20,
			item_qty=1,
			# WooCommerce REST API always applies taxes when setting fee_lines (despite setting "tax_status" to "none")
			# So we'll test with "tax_status": "taxable"
			# https://github.com/woocommerce/woocommerce/issues/25719
			fee_lines=[
				{
					"name": "Voucher - New Launch",
					"tax_status": "taxable",
					"amount": "-10",
					"total": "-10.00",
					"meta_data": [],
				}
			],
		)

		# Run synchronisation for the ERPNext Sales Order to be created
		run_sales_order_sync(woocommerce_order_name=wc_order_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Sales Order in ERPNext
		sales_order_name = frappe.get_value("Sales Order", {"woocommerce_id": wc_order_id}, "name")
		self.assertIsNotNone(sales_order_name)
		sales_order = frappe.get_doc("Sales Order", sales_order_name)

		# Expect correct taxes and charges row for fee in Sales Order
		self.assertEqual(sales_order.taxes[-2].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[-2].rate, 0)
		self.assertEqual(sales_order.taxes[-2].tax_amount, -10)
		self.assertEqual(sales_order.taxes[-2].account_head, "Marketing Expenses - SC")
		self.assertEqual(sales_order.taxes[-2].description, "Voucher - New Launch")

		# Expect correct taxes and charges row for fee tax in Sales Order
		self.assertEqual(sales_order.taxes[-1].charge_type, "Actual")
		self.assertEqual(sales_order.taxes[-1].rate, 0)
		self.assertEqual(sales_order.taxes[-1].tax_amount, -1.5)
		self.assertEqual(sales_order.taxes[-1].account_head, "VAT - SC")

		# Delete order in WooCommerce
		# self.delete_woocommerce_order(wc_order_id=wc_order_id)
