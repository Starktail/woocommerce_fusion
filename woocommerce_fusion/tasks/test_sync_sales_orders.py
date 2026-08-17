import json
from unittest.mock import Mock, call, patch

import frappe
from erpnext import get_default_company
from frappe.tests import IntegrationTestCase

from woocommerce_fusion.tasks.sync_sales_orders import (
	SynchroniseSalesOrder,
	encode_line_item_meta_display_values,
	find_customer_by_email_domain,
	find_existing_contact,
	find_existing_customer,
	get_customer_selling_price_list,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

default_company = get_default_company()
default_bank = "Test Bank"
default_bank_account = "Checking Account"


@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.get_cached_doc")
class TestWooCommerceSync(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.
		customer = create_customer()
		create_contact(customer)

	@patch.object(SynchroniseSalesOrder, "update_sales_order")
	def test_sync_sales_order_should_update_sales_order_if_so_is_older(
		self, mock_update_sales_order, mock_get_wc_servers
	):
		"""
		Test that the 'sync_sales_orders' function should update the sales order
		if the sales order is older than the corresponding WooCommerce order
		"""
		# Initialise class
		sync = SynchroniseSalesOrder()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Sales Order
		sales_order = frappe.get_doc({"doctype": "Sales Order"})
		sales_order.name = "SO-0001"
		sales_order.woocommerce_server = woocommerce_server
		sales_order.woocommerce_id = woocommerce_id
		sales_order.modified = "2023-01-01"
		sync.sales_order = sales_order

		# Create dummy WooCommerce Order
		wc_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		wc_order.woocommerce_server = woocommerce_server
		wc_order.id = woocommerce_id
		wc_order.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_order.woocommerce_date_modified = "2023-12-31"
		sync.woocommerce_order = wc_order

		# Call the method under test
		sync.sync_wc_order_with_erpnext_order()

		# Assert that the sales order need to be updated
		mock_update_sales_order.assert_called_once_with(wc_order, sales_order)

	@patch.object(SynchroniseSalesOrder, "create_and_link_payment_entry")
	@patch.object(SynchroniseSalesOrder, "update_woocommerce_order")
	def test_sync_sales_order_should_update_wc_order_if_so_is_newer(
		self,
		mock_update_woocommerce_order,
		mock_create_and_link_payment_entry,
		mock_get_wc_servers,
	):
		"""
		Test that the 'sync_sales_order' function should update the WooCommerce order
		if the sales order is newer than the corresponding WooCommerce order
		"""
		# Initialise class
		sync = SynchroniseSalesOrder()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Sales Order
		sales_order = frappe._dict()
		sales_order.name = "SO-0001"
		sales_order.woocommerce_server = woocommerce_server
		sales_order.woocommerce_id = woocommerce_id
		sales_order.modified = "2023-12-25"
		sales_order.docstatus = 1
		sales_order.reload = Mock()
		sales_order.save = Mock()
		sync.sales_order = sales_order

		# Create dummy WooCommerce Order
		wc_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		wc_order.woocommerce_server = woocommerce_server
		wc_order.id = woocommerce_id
		wc_order.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_order.woocommerce_date_modified = "2023-01-01"
		sync.woocommerce_order = wc_order

		# Call the method under test
		sync.sync_wc_order_with_erpnext_order()

		# Assert that the sales order need to be updated
		mock_update_woocommerce_order.assert_called_once_with(wc_order, sales_order)

	@patch.object(SynchroniseSalesOrder, "create_sales_order")
	def test_sync_sales_order_should_create_so_if_no_so(self, mock_create_sales_order, mock_get_wc_servers):
		"""
		Test that the 'sync_sales_order' function should create a Sales Order if
		there are no corresponding Sales orders
		"""
		# Initialise class
		sync = SynchroniseSalesOrder()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy WooCommerce Order
		wc_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		wc_order.woocommerce_server = woocommerce_server
		wc_order.id = woocommerce_id
		wc_order.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		sync.woocommerce_order = wc_order

		# Call the method under test
		sync.sync_wc_order_with_erpnext_order()

		# Assert that the sales order need to be created
		mock_create_sales_order.assert_called_once()
		self.assertEqual(mock_create_sales_order.call_args.args[0], wc_order)

	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	def test_successful_payment_entry_creation(self, mock_frappe_new_doc, mock_get_wc_servers):
		# Initialise class
		sync = SynchroniseSalesOrder()

		# Arrange
		wc_order = frappe._dict(
			{
				"payment_method": "PayPal",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "PayPal",
				"total": 100,
			}
		)

		mock_sales_order = frappe._dict(
			woocommerce_server="example.com",
			woocommerce_payment_entry=None,
			customer="customer_1",
			grand_total=100,
			name="SO-0001",
			docstatus=1,
			per_billed=0,
		)

		mock_get_wc_servers.return_value = frappe._dict(
			enable_payments_sync=1,
			woocommerce_server_url="http://example.com",
			payment_method_bank_account_mapping=json.dumps({"PayPal": "Bank Account"}),
			payment_method_gl_account_mapping=json.dumps({"PayPal": "GL Account"}),
		)

		# Act
		sync.create_and_link_payment_entry(wc_order, mock_sales_order)

		# Assert
		self.assertIsNotNone(mock_sales_order.woocommerce_payment_entry)
		mock_frappe_new_doc.assert_called_once_with("Payment Entry")

	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	def test_that_no_payment_entry_is_created_when_mapping_is_null(
		self, mock_frappe_new_doc, mock_get_wc_servers
	):
		# Arrange
		sync = SynchroniseSalesOrder()
		wc_order = frappe._dict(
			{
				"payment_method": "EFT",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "EFT",
			}
		)

		mock_sales_order = frappe._dict(
			woocommerce_server="example.com",
			woocommerce_payment_entry=None,
			customer="customer_1",
			grand_total=100,
			name="SO-0001",
			docstatus=1,
			per_billed=0,
		)

		mock_get_wc_servers.return_value = frappe._dict(
			enable_payments_sync=1,
			woocommerce_server_url="http://example.com",
			payment_method_bank_account_mapping=json.dumps({"EFT": None}),
			payment_method_gl_account_mapping=json.dumps({"EFT": None}),
		)

		# Act
		sync.create_and_link_payment_entry(wc_order, mock_sales_order)

		# Assert
		self.assertIsNone(mock_sales_order.woocommerce_payment_entry)
		mock_frappe_new_doc.assert_not_called()

	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.get_all")
	def test_payment_entry_created_with_sales_invoice_as_reference(
		self, mock_frappe_get_all, mock_frappe_new_doc, mock_get_wc_servers
	):
		"""
		Test that the created Payment Entry's reference is set to the linked Sales Invoice when
		a Sales Invoice is already created for the Sales Order
		"""
		# Initialise class
		sync = SynchroniseSalesOrder()

		# Arrange
		wc_order = frappe._dict(
			{
				"payment_method": "PayPal",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "PayPal",
				"total": 100,
			}
		)

		mock_sales_order = frappe._dict(
			woocommerce_server="example.com",
			woocommerce_payment_entry=None,
			customer="customer_1",
			grand_total=100,
			name="SO-0001",
			docstatus=1,
			per_billed=1,
		)

		mock_sales_invoice_item = frappe._dict(parent="INVOICE-12345")

		mock_get_wc_servers.return_value = frappe._dict(
			enable_payments_sync=1,
			woocommerce_server_url="http://example.com",
			payment_method_bank_account_mapping=json.dumps({"PayPal": "Bank Account"}),
			payment_method_gl_account_mapping=json.dumps({"PayPal": "GL Account"}),
		)
		mock_frappe_get_all.return_value = [mock_sales_invoice_item]

		mock_payment_entry = frappe._dict(name="PE-000001")

		mock_payment_entry.update = Mock()
		mock_row = frappe._dict()
		mock_payment_entry.append = Mock()
		mock_payment_entry.append.return_value = mock_row
		mock_payment_entry.save = Mock()
		mock_frappe_new_doc.return_value = mock_payment_entry

		# Act
		sync.create_and_link_payment_entry(wc_order, mock_sales_order)

		# Assert
		self.assertEqual(mock_sales_order.woocommerce_payment_entry, "PE-000001")
		mock_frappe_new_doc.assert_called_once_with("Payment Entry")
		self.assertEqual(mock_row.reference_name, "INVOICE-12345")

	@patch.object(SynchroniseSalesOrder, "create_address")
	@patch.object(SynchroniseSalesOrder, "update_address")
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	def test_create_single_address_created_when_same(
		self,
		mock_frappe_new_doc,
		mock_update_address,
		mock_create_address,
		mock_get_wc_servers,
	):
		# Initialise class
		sync = SynchroniseSalesOrder()
		sync.customer = frappe._dict({"name": "Test Customer"})

		# Arrange
		address = {
			"first_name": "Samwise",
			"last_name": "Gangee",
			"company": "",
			"address_1": "Ring Lane",
			"address_2": "",
			"city": "Shire",
			"state": "ME",
			"postcode": "12121",
			"country": "DE",
			"email": "samwise@me.net",
			"phone": "0123323216",
		}

		wc_order = frappe._dict(
			{
				"payment_method": "PayPal",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "PayPal",
				"total": 100,
				"billing": json.dumps(address),
				"shipping": json.dumps(address),
			}
		)

		mock_get_wc_servers.return_value = frappe._dict(
			woocommerce_server_url="http://example.com",
		)

		# Act
		sync.create_or_update_address(wc_order)

		# Assert that a single address is created
		# mock_update_address.assert_called_once_with("Payment Entry")
		mock_create_address.assert_called_once_with(
			address,
			sync.customer,
			"Billing",
			is_primary_address=1,
			is_shipping_address=1,
		)

	@patch.object(SynchroniseSalesOrder, "create_address")
	@patch.object(SynchroniseSalesOrder, "update_address")
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	def test_create_multiple_addresses_created_when_different(
		self,
		mock_frappe_new_doc,
		mock_update_address,
		mock_create_address,
		mock_get_wc_servers,
	):
		# Initialise class
		sync = SynchroniseSalesOrder()
		sync.customer = frappe._dict({"name": "Test Customer"})

		# Arrange
		address_billing = {
			"first_name": "Samwise",
			"last_name": "Gangee",
			"company": "",
			"address_1": "Ring Lane",
			"address_2": "",
			"city": "Shire",
			"state": "ME",
			"postcode": "12121",
			"country": "DE",
			"email": "samwise@me.net",
			"phone": "0123323216",
		}
		address_shipping = address_billing.copy()
		address_shipping["postcode"] = "42069"

		wc_order = frappe._dict(
			{
				"payment_method": "PayPal",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "PayPal",
				"total": 100,
				"billing": json.dumps(address_billing),
				"shipping": json.dumps(address_shipping),
			}
		)

		mock_get_wc_servers.return_value = frappe._dict(
			woocommerce_server_url="http://example.com",
		)

		# Act
		sync.create_or_update_address(wc_order)

		# Assert that a single address is created
		expected_calls = [
			call(
				address_billing,
				sync.customer,
				"Billing",
				is_primary_address=1,
				is_shipping_address=0,
			),
			call(
				address_shipping,
				sync.customer,
				"Shipping",
				is_primary_address=0,
				is_shipping_address=1,
			),
		]

		mock_create_address.assert_has_calls(expected_calls)

	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.log_error")
	def test_no_payment_entry_created_when_total_is_zero(
		self, mock_log_error, mock_frappe_new_doc, mock_get_wc_servers
	):
		"""
		Test that no payment entry is created when the WooCommerce order total is 0
		"""
		# Initialise class
		sync = SynchroniseSalesOrder()

		# Arrange
		wc_order = frappe._dict(
			{
				"payment_method": "PayPal",
				"date_paid": "2023-01-01",
				"name": "wc_order_1",
				"payment_method_title": "PayPal",
				"total": 0,
				"id": "123",
			}
		)

		mock_sales_order = frappe._dict(
			woocommerce_server="example.com",
			woocommerce_payment_entry=None,
			customer="customer_1",
			grand_total=0,
			name="SO-0001",
			docstatus=1,
			per_billed=0,
		)

		mock_get_wc_servers.return_value = frappe._dict(
			enable_payments_sync=1,
			woocommerce_server_url="http://example.com",
			payment_method_bank_account_mapping=json.dumps({"PayPal": "Bank Account"}),
			payment_method_gl_account_mapping=json.dumps({"PayPal": "GL Account"}),
		)

		# Act
		result = sync.create_and_link_payment_entry(wc_order, mock_sales_order)

		# Assert
		self.assertTrue(result)
		self.assertIsNone(mock_sales_order.woocommerce_payment_entry)
		mock_frappe_new_doc.assert_not_called()

	def test_contact_found_with_email(self, mock_get_cached_doc):
		result = find_existing_contact(email="test@test.test", phone=None)
		self.assertIsNotNone(result)

	def test_contact_found_with_phone(self, mock_get_cached_doc):
		result = find_existing_contact(email=None, phone="0123456789")
		self.assertIsNotNone(result)

	def test_contact_email_doesnt_exist(self, mock_get_cached_doc):
		result = find_existing_contact(email="doesntexist@db.test", phone=None)
		self.assertEqual(result, None)


class TestCustomerSellingPriceList(IntegrationTestCase):
	"""
	A Sales Order created from a WooCommerce order used to keep the Selling Settings default price
	list, so its rates showed next to the ones WooCommerce charged as if the customer got a discount.
	"""

	def setUp(self):
		self.price_list = create_price_list("_Test Woo Trade Selling", currency="ZAR")
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Test Customer for Price List",
				"customer_type": "Individual",
				"default_price_list": self.price_list,
			}
		).insert(ignore_permissions=True)

	def test_the_customers_own_price_list_is_used(self):
		self.assertEqual(
			get_customer_selling_price_list(self.customer.name, "ZAR"),
			self.price_list,
		)

	def test_the_customer_groups_price_list_is_used_as_a_fallback(self):
		self.customer.default_price_list = None
		self.customer.save()
		frappe.db.set_value(
			"Customer Group", self.customer.customer_group, "default_price_list", self.price_list
		)
		frappe.clear_cache(doctype="Customer Group")

		self.assertEqual(get_customer_selling_price_list(self.customer.name, "ZAR"), self.price_list)

		frappe.db.set_value("Customer Group", self.customer.customer_group, "default_price_list", None)

	def test_a_price_list_in_another_currency_is_declined(self):
		"""
		Converting would need an exchange rate, and a missing one would fail the whole order sync
		"""
		self.assertIsNone(get_customer_selling_price_list(self.customer.name, "USD"))

	def test_a_customer_without_a_price_list_keeps_the_default(self):
		self.customer.default_price_list = None
		self.customer.save()

		self.assertIsNone(get_customer_selling_price_list(self.customer.name, "ZAR"))


class TestCustomerMatching(IntegrationTestCase):
	"""
	Guest orders were keyed on the WooCommerce order ID, so every order from the same person created
	another Customer. `find_existing_contact` could already find the person by email, but its result
	was only used to set `customer_primary_contact`, after the duplicate had been created.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# One Customer for the whole class: a fixture per test would accumulate autonamed duplicates
		# on the same domain, and the domain lookup deliberately returns the oldest of them
		cls.email = "match-me@corporate-test.example"
		cls.customer_name = frappe.db.get_value("Customer", {"woocommerce_identifier": cls.email}, "name")
		if not cls.customer_name:
			cls.customer_name = (
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": "Test Customer for Matching",
						"customer_type": "Company",
						"woocommerce_identifier": cls.email,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

	def setUp(self):
		self.server = frappe._dict(customer_matching_email_domains=None, enable_dual_accounts=0)

	def test_a_customer_is_found_on_its_identifier(self):
		self.assertEqual(find_existing_customer(self.email, self.server), self.customer_name)

	def test_the_lookup_is_case_insensitive(self):
		self.assertEqual(find_existing_customer(self.email.upper(), self.server), self.customer_name)

	def test_a_customer_is_found_through_a_contact_holding_the_address(self):
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Contact For Matching"})
		contact.add_email("through-contact@corporate-test.example", is_primary=1)
		contact.append("links", {"link_doctype": "Customer", "link_name": self.customer_name})
		contact.insert(ignore_permissions=True)

		self.assertEqual(
			find_existing_customer("through-contact@corporate-test.example", self.server),
			self.customer_name,
		)

	def test_an_unknown_address_matches_nothing(self):
		self.assertIsNone(find_existing_customer("nobody@corporate-test.example", self.server))
		self.assertIsNone(find_existing_customer("", self.server))

	def test_a_listed_domain_links_a_second_buyer_at_the_same_company(self):
		self.server.customer_matching_email_domains = "corporate-test.example"

		self.assertEqual(
			find_existing_customer("someone-else@corporate-test.example", self.server),
			self.customer_name,
		)

	def test_an_unlisted_domain_does_not(self):
		"""
		Most customers order from a free mail provider, so matching every domain would put all of them
		on one account
		"""
		self.server.customer_matching_email_domains = "acme.co.za\nother.example"

		self.assertIsNone(find_existing_customer("someone-else@corporate-test.example", self.server))

	def test_the_domain_list_tolerates_blanks_and_an_at_prefix(self):
		self.server.customer_matching_email_domains = "\n  @Corporate-Test.Example  \n\n"

		self.assertEqual(
			find_customer_by_email_domain("someone-else@corporate-test.example", self.server),
			self.customer_name,
		)


class TestOrderLineItemFieldMap(IntegrationTestCase):
	"""
	The order line mapper matched nothing on a meta key WooCommerce had not written, then raised
	IndexError on `matches[0]` for a product that had no name to raise the proper error against.
	"""

	def setUp(self):
		self.sync = SynchroniseSalesOrder()
		self.sync.sales_order = frappe._dict(name="SO-0001")
		self.sync.woocommerce_order = frappe._dict(
			name="site1.example.com~1", woocommerce_server="site1.example.com"
		)
		self.so_item = frappe._dict(item_code="TEST-ITEM", warehouse="Stores - SC")
		self.server = frappe._dict(order_line_item_field_map=[])

	def map_field(self, jsonpath: str):
		self.server.order_line_item_field_map = [
			frappe._dict(erpnext_field_name="warehouse | Warehouse", woocommerce_field_name=jsonpath)
		]

	def set_fields(self, line_item):
		with patch(
			"woocommerce_fusion.tasks.sync_sales_orders.frappe.get_cached_doc", return_value=self.server
		):
			return self.sync.set_wc_order_line_items_mapped_fields(line_item, self.so_item)

	def test_a_meta_row_that_woocommerce_has_not_written_is_created(self):
		self.map_field("$.meta_data[?key='_warehouse'].value")
		line_item = {"product_id": 1, "meta_data": [{"key": "_other", "value": "x"}]}

		dirty, line_item = self.set_fields(line_item)

		self.assertTrue(dirty)
		self.assertEqual(line_item["meta_data"][-1], {"key": "_warehouse", "value": "Stores - SC"})

	def test_an_existing_meta_row_is_updated(self):
		self.map_field("$.meta_data[?key='_warehouse'].value")
		line_item = {"product_id": 1, "meta_data": [{"key": "_warehouse", "value": "Stale"}]}

		dirty, line_item = self.set_fields(line_item)

		self.assertTrue(dirty)
		self.assertEqual(line_item["meta_data"][0]["value"], "Stores - SC")

	def test_a_matching_value_is_not_dirty(self):
		self.map_field("$.meta_data[?key='_warehouse'].value")
		line_item = {"product_id": 1, "meta_data": [{"key": "_warehouse", "value": "Stores - SC"}]}

		dirty, _line_item = self.set_fields(line_item)

		self.assertFalse(dirty)

	def test_a_target_that_cannot_be_created_raises(self):
		"""
		Previously this fell through to `matches[0]` and raised IndexError instead
		"""
		self.map_field("$.meta_data[7].value")

		with self.assertRaises(ValueError):
			self.set_fields({"product_id": 1, "meta_data": []})

	def test_a_target_that_cannot_be_created_is_skipped_for_a_new_order(self):
		self.map_field("$.meta_data[7].value")
		self.sync.woocommerce_order.name = None

		dirty, _line_item = self.set_fields({"product_id": 1, "meta_data": []})

		self.assertFalse(dirty)


class TestLineItemMetaDisplayValues(IntegrationTestCase):
	"""
	WooCommerce declares a line item's meta `display_value` as a string and 400s the whole PUT when an
	object is sent for it. Line items are carried over from the order as fetched, so structured meta
	written by a plugin - Shipping Label Wizard's `_slw_data` - used to go straight back out and the
	order could never be updated again.
	"""

	def test_an_object_display_value_is_encoded(self):
		encoded = encode_line_item_meta_display_values(
			[{"key": "_slw_data", "value": "x", "display_value": {"box": 1, "labels": ["a"]}}]
		)

		self.assertEqual(encoded[0]["display_value"], json.dumps({"box": 1, "labels": ["a"]}))
		# `value` is mixed in WooCommerce's schema, so it has to survive untouched
		self.assertEqual(encoded[0]["value"], "x")

	def test_a_list_display_value_is_encoded(self):
		encoded = encode_line_item_meta_display_values([{"key": "_k", "display_value": ["a", "b"]}])

		self.assertEqual(encoded[0]["display_value"], json.dumps(["a", "b"]))

	def test_values_that_woocommerce_accepts_are_left_alone(self):
		meta_data = [
			{"key": "_a", "display_value": "Plain text"},
			{"key": "_b", "display_value": 7},
			{"key": "_c", "display_value": None},
			{"key": "_d", "value": {"not": "touched"}},
			{"key": "_e"},
		]

		self.assertEqual(encode_line_item_meta_display_values(meta_data), meta_data)

	def test_the_source_line_items_are_not_mutated(self):
		"""
		The caller compares against the line items it fetched, so encoding may not reach back into them
		"""
		meta_data = [{"key": "_slw_data", "display_value": {"box": 1}}]

		encode_line_item_meta_display_values(meta_data)

		self.assertEqual(meta_data[0]["display_value"], {"box": 1})

	def test_no_meta_data(self):
		self.assertEqual(encode_line_item_meta_display_values(None), [])
		self.assertEqual(encode_line_item_meta_display_values([]), [])

	def test_the_order_push_sends_an_encoded_display_value(self):
		"""
		End to end over `update_woocommerce_order`: the rewritten line items are what gets PUT
		"""
		sync = SynchroniseSalesOrder()

		wc_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		wc_order.woocommerce_server = "site1.example.com"
		wc_order.id = 1
		wc_order.name = generate_woocommerce_record_name_from_domain_and_id("site1.example.com", 1)
		wc_order.line_items = json.dumps(
			[
				{
					"id": 11,
					"product_id": 101,
					"quantity": 1,
					"meta_data": [{"key": "_slw_data", "display_value": {"box": 1}}],
				},
				{"id": 12, "product_id": 102, "quantity": 1, "meta_data": []},
			]
		)
		wc_order.save = Mock()
		sync.woocommerce_order = wc_order

		# A real document, because `frappe._dict.items` resolves to the dict method
		sales_order = frappe.get_doc({"doctype": "Sales Order"})
		sales_order.name = "SO-0001"
		sales_order.append("items", {"item_code": "TEST-ITEM", "qty": 2, "rate": 100})
		sync.sales_order = sales_order

		wc_server = frappe._dict(
			{
				"name": "site1.example.com",
				"enable_so_status_sync": 0,
				"sync_so_items_to_wc": 1,
				"order_line_item_field_map": [],
			}
		)

		with (
			patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.get_cached_doc", return_value=wc_server),
			patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.get_value", return_value="101"),
		):
			sync.update_woocommerce_order(wc_order, sales_order)

		wc_order.save.assert_called_once()
		# The cleared originals come first, then the rebuilt lines
		new_line_item = json.loads(wc_order.line_items)[-1]
		self.assertEqual(
			new_line_item["meta_data"], [{"key": "_slw_data", "display_value": json.dumps({"box": 1})}]
		)


def create_price_list(name: str, currency: str) -> str:
	if frappe.db.exists("Price List", name):
		return name

	return (
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"currency": currency,
				"selling": 1,
				"enabled": 1,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def create_customer():
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": "Test Customer for Contacts",
			"customer_type": "Individual",
		}
	).insert(ignore_permissions=True)
	return customer.name


def create_contact(customer_doc_name):
	contact = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": "Test",
			"last_name": "Customer",
		}
	)
	contact.append("links", {"link_doctype": "Customer", "link_name": customer_doc_name})
	contact.append("email_ids", {"email_id": "test@test.test", "is_primary": 1})
	contact.append("phone_nos", {"phone": "0123456789", "is_primary_phone": 1})
	contact.insert(ignore_permissions=True)


def create_bank_account(bank_name=default_bank, account_name="_Test Bank", company=default_company):
	try:
		gl_account = frappe.get_doc(
			{
				"doctype": "Account",
				"company": company,
				"account_name": account_name,
				"parent_account": "Bank Accounts - SC",
				"account_number": "1",
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	try:
		frappe.get_doc(
			{
				"doctype": "Bank",
				"bank_name": bank_name,
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	try:
		bank_account_doc = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": default_bank_account,
				"bank": bank_name,
				"account": gl_account.name,
				"is_company_account": 1,
				"company": company,
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	return bank_account_doc


def create_gl_account_for_bank(account_name="_Test Bank"):
	try:
		frappe.get_doc(
			{
				"doctype": "Account",
				"company": get_default_company(),
				"account_name": account_name,
				"parent_account": "Bank Accounts - SC",
				"type": "Bank",
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	return frappe.get_doc("Account", {"account_name": account_name, "company": get_default_company()})
