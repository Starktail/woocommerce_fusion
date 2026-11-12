from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe.tests import UnitTestCase

from woocommerce_fusion.tasks.sync_items import (
	ERPNextItemToSync,
	SynchroniseItem,
	run_item_sync_from_hook,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


@patch("woocommerce_fusion.tasks.sync_items.run_item_sync")
@patch.object(SynchroniseItem, "set_sync_hash")
class TestWooCommerceSync(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@patch.object(SynchroniseItem, "update_item")
	def test_sync_items_while_passing_item_should_update_item_if_item_is_older(
		self, mock_update_item, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should update the item
		if the item is older than the corresponding WooCommerce product
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Item
		item = frappe.get_doc({"doctype": "Item"})
		item.name = "ITEM-0001"
		row = item.append("woocommerce_servers")
		row.woocommerce_id = woocommerce_id
		row.woocommerce_server = woocommerce_server
		item.modified = "2023-01-01"
		sync.item = ERPNextItemToSync(item, 1)

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_product.woocommerce_date_modified = "2023-12-31"
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be updated
		mock_update_item.assert_called_once_with(wc_product, sync.item)

	@patch("woocommerce_fusion.tasks.sync_items.frappe")
	@patch.object(SynchroniseItem, "update_woocommerce_product")
	def test_sync_items_while_passing_item_should_update_wc_product_if_item_is_newer(
		self, mock_update_woocommerce_product, mock_frappe, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should update the WooCommerce product
		if the item is newer than the corresponding WooCommerce product
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		mock_frappe.get_doc.return_value = Mock()
		mock_frappe.get_single.return_value = Mock()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Item
		item = frappe.get_doc({"doctype": "Item"})
		item.name = "ITEM-0001"
		row = item.append("woocommerce_servers")
		row.woocommerce_id = woocommerce_id
		row.woocommerce_server = woocommerce_server
		item.modified = "2023-12-25"
		item.docstatus = 1
		sync.item = ERPNextItemToSync(item, 1)

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_product.woocommerce_date_modified = "2023-01-01"
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be updated
		mock_update_woocommerce_product.assert_called_once_with(wc_product, sync.item)

	@patch("woocommerce_fusion.tasks.sync.frappe")
	@patch.object(SynchroniseItem, "create_item")
	def test_sync_items_while_passing_item_should_create_item_if_no_item(
		self, mock_create_item, mock_frappe, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should create a Item if
		there are no corresponding Items
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		mock_frappe.get_doc.return_value = Mock()
		mock_frappe.get_single.return_value = Mock()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be created
		mock_create_item.assert_called_once()
		self.assertEqual(mock_create_item.call_args.args[0], wc_product)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_server = "Test Server"
		wc_product.sku = "Test SKU"
		wc_product.woocommerce_id = 12345
		wc_product.woocommerce_name = "Test Product"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		item_mock.append.assert_called_once_with("woocommerce_servers")
		item_mock.insert.assert_called_once()

		self.assertEqual(item_mock.item_code, str(wc_product.woocommerce_id))
		self.assertEqual(item_mock.item_name, wc_product.woocommerce_name)
		self.assertEqual(item_mock.flags.ignore_mandatory, True)

		row = item_mock.append.return_value
		self.assertEqual(row.woocommerce_id, wc_product.woocommerce_id)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item_handles_woo_product_variant(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		parent_item_mock = MagicMock()
		parent_item_mock.item_code = 9999
		parent_item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock
		mock_run_item_sync.return_value = (parent_item_mock, None)

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_id = 42001
		wc_product.woocommerce_server = "Test Server"
		wc_product.type = "variation"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		# Assert that ERPNext item is created as a variant of a template item
		self.assertEqual(item_mock.variant_of, 9999)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item_handles_woo_product_with_variants(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_id = 42001
		wc_product.woocommerce_server = "Test Server"
		wc_product.type = "variable"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		# Assert that ERPNext template item is created
		self.assertEqual(item_mock.has_variants, 1)

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_sale_price_data")
	def test_create_woocommerce_product(
		self,
		mock_get_item_sale_price_data,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()
		wc_product_mock.woocommerce_id = 67890

		mock_get_doc.return_value = wc_product_mock
		mock_get_item_price_rate.return_value = "100.00"
		mock_get_item_sale_price_data.return_value = None

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 0
		item_mock.item.variant_of = None

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		mock_get_doc.assert_called_once_with({"doctype": "WooCommerce Product"})

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(wc_product_mock.woocommerce_server, item_woocommerce_server_mock.woocommerce_server)
		self.assertEqual(wc_product_mock.woocommerce_name, item_mock.item.item_name)
		self.assertEqual(wc_product_mock.type, "simple")
		self.assertEqual(wc_product_mock.regular_price, "100.00")

		self.assertEqual(item_woocommerce_server_mock.woocommerce_id, wc_product_mock.woocommerce_id)
		item_mock.item.save.assert_called_once()

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_sale_price_data")
	def test_create_woocommerce_product_from_variant(
		self,
		mock_get_item_sale_price_data,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()

		parent_item_mock = MagicMock()
		parent_item_mock.woocommerce_id = 696969

		mock_get_doc.side_effect = [wc_product_mock, parent_item_mock]

		mock_get_item_price_rate.return_value = "100.00"
		mock_get_item_sale_price_data.return_value = None

		mock_run_item_sync.return_value = (None, parent_item_mock)

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 0
		item_mock.item.variant_of = "696969"

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		expected_calls = [call({"doctype": "WooCommerce Product"}), call("Item", "696969")]
		mock_get_doc.assert_has_calls(expected_calls)

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(wc_product_mock.parent_id, 696969)
		self.assertEqual(wc_product_mock.type, "variation")
		item_mock.item.save.assert_called_once()

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_sale_price_data")
	def test_create_woocommerce_product_from_template_item(
		self,
		mock_get_item_sale_price_data,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()

		mock_get_doc.return_value = wc_product_mock

		mock_get_item_price_rate.return_value = "100.00"
		mock_get_item_sale_price_data.return_value = None

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 1
		item_mock.item.variant_of = None

		mock_run_item_sync.return_value = (item_mock, None)

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		mock_get_doc.assert_called_once_with({"doctype": "WooCommerce Product"})

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(wc_product_mock.type, "variable")
		item_mock.item.save.assert_called_once()


class TestRunItemSyncFromHook(UnitTestCase):
	"""
	Everything the Item hook enqueues has to be enqueued after commit. A worker that starts
	before the Item's transaction commits reads the document as it was before the edit, finds
	nothing to push to WooCommerce, and queues no sync operation at all.
	"""

	@patch("woocommerce_fusion.tasks.sync_items.frappe.msgprint")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.enqueue")
	def test_every_job_is_enqueued_after_commit(self, mock_enqueue, mock_get_cached_doc, _mock_msgprint):
		mock_get_cached_doc.return_value = frappe._dict(enable_sync=1)
		item = frappe._dict(
			doctype="Item",
			name="ITEM-1",
			flags=frappe._dict(),
			woocommerce_servers=[frappe._dict(woocommerce_server="site1.example.com")],
		)

		in_test = frappe.flags.in_test
		frappe.flags.in_test = False
		try:
			run_item_sync_from_hook(item, "on_update")
		finally:
			frappe.flags.in_test = in_test

		self.assertTrue(mock_enqueue.call_args_list)
		for enqueue_call in mock_enqueue.call_args_list:
			self.assertTrue(
				enqueue_call.kwargs.get("enqueue_after_commit"),
				f"{enqueue_call.args[0]} is enqueued before the transaction commits",
			)


class TestVariationSync(UnitTestCase):
	@patch("woocommerce_fusion.tasks.sync_items.frappe.db.exists", return_value=False)
	@patch("woocommerce_fusion.tasks.sync_items.run_item_sync")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	def test_variation_inherits_uom_and_item_group_from_parent(
		self, mock_get_server, mock_new_doc, mock_run_item_sync, _mock_exists
	):
		mock_get_server.return_value = frappe._dict(
			{
				"name": "woo.example.com",
				"uom": "Box",
				"item_group": "Server Default Group",
				"name_by": "Product ID",
				"enable_image_sync": 0,
			}
		)

		parent_item = frappe._dict(
			{
				"item_code": "PARENT-ITEM",
				"stock_uom": "Pair",
				"item_group": "Parent Item Group",
			}
		)
		mock_run_item_sync.return_value = (parent_item, None)

		def append_child(field, row=None):
			r = frappe._dict(row or {})
			getattr(item, field).append(r)
			return r

		item = frappe._dict(
			{
				"flags": frappe._dict(),
				"woocommerce_servers": [],
				"attributes": [],
				"append": append_child,
				"insert": Mock(),
			}
		)
		mock_new_doc.return_value = item

		sync = SynchroniseItem(servers=Mock())
		wc_product = frappe._dict(
			{
				"doctype": "WooCommerce Product",
				"woocommerce_server": "woo.example.com",
				"woocommerce_id": 55,
				"woocommerce_name": "Variant Product",
				"type": "variation",
				"parent_id": 10,
				"sku": "VAR-55",
				"attributes": "[]",
				"images": "[]",
			}
		)

		with (
			patch.object(sync, "create_or_update_item_attributes"),
			patch.object(sync, "set_item_fields", return_value=(False, item)),
			patch.object(sync, "set_sync_hash"),
		):
			sync.create_item(wc_product)

		self.assertEqual(item.stock_uom, "Pair")
		self.assertEqual(item.item_group, "Parent Item Group")
		self.assertEqual(item.variant_of, "PARENT-ITEM")

	@patch("woocommerce_fusion.tasks.sync_items.get_list_of_wc_products")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.db.set_single_value")
	def test_sync_products_modified_since_enqueues_variation_with_parent_id(
		self, _mock_set_val, mock_get_server, mock_get_products
	):
		from woocommerce_fusion.tasks.sync_items import sync_woocommerce_products_modified_since

		mock_get_server.return_value = frappe._dict(
			{
				"woocommerce_server": "woo.example.com",
				"enable_batch_api": 1,
			}
		)
		wc_var = frappe._dict(
			{
				"woocommerce_server": "woo.example.com",
				"woocommerce_id": 99,
				"type": "variation",
				"parent_id": 44,
			}
		)
		mock_get_products.return_value = [wc_var]

		with patch(
			"woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue.enqueue_item"
		) as mock_enqueue:
			sync_woocommerce_products_modified_since(date_time_from="2026-01-01 00:00:00")

			mock_enqueue.assert_called_once()
			kwargs = mock_enqueue.call_args.kwargs
			self.assertEqual(kwargs["resource_type"], "product_variation")
			self.assertEqual(kwargs["parent_woocommerce_id"], "44")
			self.assertEqual(kwargs["parent_id"], "44")

	@patch("woocommerce_fusion.tasks.sync_items.frappe.db.exists", return_value=False)
	@patch("woocommerce_fusion.tasks.sync_items.run_item_sync", side_effect=Exception("Parent not found"))
	@patch("woocommerce_fusion.tasks.sync_items.frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	def test_variation_with_failing_parent_falls_back_gracefully(
		self, mock_get_server, mock_new_doc, _mock_run_sync, _mock_exists
	):
		mock_get_server.return_value = frappe._dict(
			{
				"name": "woo.example.com",
				"uom": "Box",
				"item_group": "Server Default Group",
				"name_by": "Product ID",
				"enable_image_sync": 0,
			}
		)

		def append_child(field, row=None):
			r = frappe._dict(row or {})
			r.idx = len(getattr(item, field)) + 1
			getattr(item, field).append(r)
			return r

		item = frappe._dict(
			{
				"flags": frappe._dict(),
				"woocommerce_servers": [],
				"attributes": [],
				"append": append_child,
				"insert": Mock(),
			}
		)
		mock_new_doc.return_value = item

		sync = SynchroniseItem(servers=Mock())
		wc_product = frappe._dict(
			{
				"doctype": "WooCommerce Product",
				"woocommerce_server": "woo.example.com",
				"woocommerce_id": 56,
				"woocommerce_name": "Variant Product",
				"type": "variation",
				"parent_id": 11,
				"sku": "VAR-56",
				"attributes": "[]",
				"images": "[]",
			}
		)

		with (
			patch.object(sync, "create_or_update_item_attributes"),
			patch.object(sync, "set_item_fields", return_value=(False, item)),
			patch.object(sync, "set_sync_hash"),
		):
			sync.create_item(wc_product)

		self.assertEqual(item.stock_uom, "Box")
		self.assertEqual(item.item_group, "Server Default Group")

	@patch("woocommerce_fusion.tasks.sync_items.frappe.db.exists", return_value=False)
	@patch("woocommerce_fusion.tasks.sync_items.frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	def test_variation_without_parent_id_falls_back_gracefully(
		self, mock_get_server, mock_new_doc, _mock_exists
	):
		mock_get_server.return_value = frappe._dict(
			{
				"name": "woo.example.com",
				"uom": "Box",
				"item_group": "Server Default Group",
				"name_by": "Product ID",
				"enable_image_sync": 0,
			}
		)

		def append_child(field, row=None):
			r = frappe._dict(row or {})
			r.idx = len(getattr(item, field)) + 1
			getattr(item, field).append(r)
			return r

		item = frappe._dict(
			{
				"flags": frappe._dict(),
				"woocommerce_servers": [],
				"attributes": [],
				"append": append_child,
				"insert": Mock(),
			}
		)
		mock_new_doc.return_value = item

		sync = SynchroniseItem(servers=Mock())
		wc_product = frappe._dict(
			{
				"doctype": "WooCommerce Product",
				"woocommerce_server": "woo.example.com",
				"woocommerce_id": 57,
				"woocommerce_name": "Variant Product",
				"type": "variation",
				"parent_id": None,
				"sku": "VAR-57",
				"attributes": "[]",
				"images": "[]",
			}
		)

		with (
			patch.object(sync, "create_or_update_item_attributes"),
			patch.object(sync, "set_item_fields", return_value=(False, item)),
			patch.object(sync, "set_sync_hash"),
		):
			sync.create_item(wc_product)

		self.assertEqual(item.stock_uom, "Box")
		self.assertEqual(item.item_group, "Server Default Group")

	@patch("woocommerce_fusion.tasks.sync_items.get_list_of_wc_products")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc")
	@patch("woocommerce_fusion.tasks.sync_items.frappe.db.set_single_value")
	def test_sync_products_modified_since_strips_whitespace_on_variation_ids(
		self, _mock_set_val, mock_get_server, mock_get_products
	):
		from woocommerce_fusion.tasks.sync_items import sync_woocommerce_products_modified_since

		mock_get_server.return_value = frappe._dict(
			{
				"woocommerce_server": "woo.example.com",
				"enable_batch_api": 1,
			}
		)
		wc_var = frappe._dict(
			{
				"woocommerce_server": "woo.example.com",
				"woocommerce_id": " 99 \n",
				"type": "variation",
				"parent_id": " 44 \t",
			}
		)
		mock_get_products.return_value = [wc_var]

		with patch(
			"woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue.enqueue_item"
		) as mock_enqueue:
			sync_woocommerce_products_modified_since(date_time_from="2026-01-01 00:00:00")

			mock_enqueue.assert_called_once()
			kwargs = mock_enqueue.call_args.kwargs
			self.assertEqual(kwargs["resource_type"], "product_variation")
			self.assertEqual(kwargs["parent_woocommerce_id"], "44")
			self.assertEqual(kwargs["parent_id"], "44")
			self.assertEqual(kwargs["woocommerce_id"], "99")
			self.assertEqual(kwargs["item_code"], "99")
