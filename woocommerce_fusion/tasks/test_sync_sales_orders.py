import json
from unittest.mock import Mock, call, patch

import frappe
from erpnext import get_default_company
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.sync_sales_orders import SynchroniseSalesOrder, find_existing_contact
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

default_company = get_default_company()
default_bank = "Test Bank"
default_bank_account = "Checking Account"


@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.get_cached_doc")
class TestWooCommerceSync(FrappeTestCase):
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
	def test_sync_sales_order_should_create_so_if_no_so(
		self, mock_create_sales_order, mock_get_wc_servers
	):
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


def create_bank_account(
	bank_name=default_bank, account_name="_Test Bank", company=default_company
):
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
		gl_account = frappe.get_doc(
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

	return frappe.get_doc("Account", {"account_name": account_name})
