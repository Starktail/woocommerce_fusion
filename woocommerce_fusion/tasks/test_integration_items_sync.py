from contextlib import contextmanager
from unittest.mock import patch

import frappe
from erpnext import get_default_company
from erpnext.stock.doctype.item.test_item import create_item
from frappe.utils import add_to_date, now
from frappe.utils.data import cstr
from parameterized import parameterized

from woocommerce_fusion.tasks.field_transforms import SKIP, TO_WOOCOMMERCE
from woocommerce_fusion.tasks.sync import get_variation_parent_woocommerce_id
from woocommerce_fusion.tasks.sync_items import (
	clear_sync_hash,
	get_list_of_wc_products,
	run_item_sync,
)
from woocommerce_fusion.tasks.test_integration_helpers import (
	TestIntegrationWooCommerce,
	default_warehouse,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

BATCH_MODES = [("single_call", False), ("batch_api", True)]

BARCODES_META_KEY = "_test_barcodes"
BARCODES_JSONPATH = f"$.meta_data[?key='{BARCODES_META_KEY}'].value"
BARCODES_REGISTRY = {"barcodes": "woocommerce_fusion.tasks.test_integration_items_sync._barcodes_transform"}


def _barcodes_transform(value, *, direction, item, woocommerce_product, row):
	"""Outbound-only transform of Item > Barcodes into a list of objects"""
	if direction != TO_WOOCOMMERCE:
		return SKIP

	return [{"code": barcode.barcode, "kind": barcode.barcode_type} for barcode in value or []]


@contextmanager
def registered_barcodes_transform():
	"""
	Make the transform selectable. `woocommerce_server` imports the lookup by name, so the mapping
	row's validation reads it from there rather than from `field_transforms`.
	"""
	with (
		patch(
			"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
			return_value=BARCODES_REGISTRY,
		),
		patch(
			"woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server.get_registered_transforms",
			return_value=BARCODES_REGISTRY,
		),
	):
		yield


@patch("woocommerce_fusion.tasks.sync_items.frappe.log_error")
class TestIntegrationWooCommerceItemsSync(TestIntegrationWooCommerce):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_item_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates new Items when there are new
		WooCommerce products.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce
		wc_product_id = self.post_woocommerce_product(product_name="SOME_ITEM")

		# Run synchronisation
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Item in ERPNext
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		self.assertIsNotNone(item)

		# Expect correct item code and name in item
		self.assertEqual(item.item_code, str(wc_product_id))
		self.assertEqual(item.item_name, "SOME_ITEM")

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_item_with_image_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates a new Item with image when there are new
		WooCommerce products.
		"""
		self._set_batch_mode(batch_enabled)

		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_image_sync = 1
		wc_server.save()

		# Create a new product in WooCommerce
		wc_product_id = self.post_woocommerce_product(
			product_name="SOME_ITEM",
			image_url="https://woocommerce.com/wp-content/uploads/2023/02/chrislema-hat.png",
		)

		# Run synchronisation
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Item in ERPNext
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		self.assertIsNotNone(item)

		# Expect correct image in item
		self.assertTrue("chrislema-hat" in item.image)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_item_with_custom_metadata_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates a new ERPNext Item with mapped custom fields.
		"""
		self._set_batch_mode(batch_enabled)

		dummy_meta_data = [
			{"id": 52824, "key": "_short_description_1", "value": "Test 1"},
			{"id": 52825, "key": "_short_description_2", "value": "Test 2"},
		]
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		# Map Erpnext Item description to WC Product Meta Data with key '_short_description_2'
		wc_server.item_field_map = []
		row = wc_server.append("item_field_map")
		row.erpnext_field_name = "description | Description"
		row.woocommerce_field_name = "$.meta_data[?(@.key=='_short_description_2')].value"
		wc_server.save()

		# Create a new product in WooCommerce
		wc_product_id = self.post_woocommerce_product(
			product_name="SOME_ITEM",
			image_url="https://woocommerce.com/wp-content/uploads/2023/02/chrislema-hat.png",
			meta_data=dummy_meta_data,
		)

		# Run synchronisation
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Item in ERPNext
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		self.assertIsNotNone(item)

		# Expect value in mapped field in Item
		self.assertEqual(item.description, "Test 2")

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_template_item_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates new Template Item from a WooCommerce Product with Variations
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce
		wc_product_id = self.post_woocommerce_product(
			product_name="T-SHIRT", type="variable", attributes=["Material Type", "Volume"]
		)

		# Run synchronisation
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Item in ERPNext
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		self.assertIsNotNone(item)

		# Expect template item in ERPNext
		self.assertEqual(item.has_variants, 1)

		# Expect same attributes
		self.assertEqual(len(item.attributes), 2)
		self.assertEqual(item.attributes[0].attribute, "Material Type")
		self.assertEqual(item.attributes[1].attribute, "Volume")

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_variant_item_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates new Item Variant from a
		WooCommerce Product Variant
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new product in WooCommerce
		wc_product_id = self.post_woocommerce_product(
			product_name="T-SHIRT", type="variation", attributes=["Material Type"]
		)

		# Run synchronisation
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Expect newly created Item in ERPNext
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		self.assertIsNotNone(item)

		# Expect variant item in ERPNext
		self.assertIsNotNone(item.variant_of)
		self.assertEqual(item.has_variants, 0)

		# Expect same attribute
		self.assertEqual(len(item.attributes), 1)
		self.assertEqual(item.attributes[0].attribute, "Material Type")
		self.assertIsNotNone(item.attributes[0].attribute_value)
		self.assertEqual(item.item_name, "T-SHIRT parent - Option 1")

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_wc_product_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates a new WooCommerce product when there are new
		Items.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new item in ERPNext and set a WooCommerce server but not a product ID
		item = create_item("ITEM101", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()

		# Run synchronisation
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Get the updated item
		item.reload()

		# Expect a row in WooCommerce Servers child table and that WooCommerceID is set
		self.assertEqual(len(item.woocommerce_servers), 1)
		self.assertIsNotNone(item.woocommerce_servers[0].woocommerce_id)

		# Expect newly created WooCommerce Product
		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)

		# Expect correct item name in item
		self.assertEqual(wc_product["name"], item.item_name)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_variable_wc_product_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates a new Variable WooCommerce product
		when there is a new Template Item in ERPNext
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new item in ERPNext and set a WooCommerce server but not a product ID
		item = create_item("ITEM100", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name

		# Make this item a Template item with Attributes
		item.has_variants = 1
		for attr in ["Material Type", "Volume"]:
			create_item_attribute(attr)
			row = item.append("attributes")
			row.attribute = attr

		item.save()

		# Run synchronisation
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Get the updated item
		item.reload()

		# Expect newly created WooCommerce Product
		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)
		self.assertEqual(wc_product["type"], "variable")

		# Expect attributes to be set
		self.assertEqual(len(wc_product["attributes"]), 2)
		self.assertEqual(wc_product["attributes"][0]["name"], "Material Type")
		self.assertEqual(wc_product["attributes"][0]["variation"], True)
		self.assertEqual(wc_product["attributes"][1]["name"], "Volume")
		self.assertEqual(wc_product["attributes"][1]["variation"], True)

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_wc_product_variant_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method creates a new WooCommerce product variant
		when there is a new Item Variant in ERPNext
		"""
		self._set_batch_mode(batch_enabled)

		# Create a new parent item in ERPNext and set a WooCommerce server but not a product ID
		parent_item = create_item("ITEM200-Parent", valuation_rate=10)
		row = parent_item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		parent_item.has_variants = 1
		for attr in ["Material Type", "Volume"]:
			create_item_attribute(attr)
			row = parent_item.append("attributes")
			row.attribute = attr
		parent_item.save()

		# Create a new item in ERPNext and set a WooCommerce server but not a product ID
		# Make this item a Variant Item with Attributes
		item = create_variant_item(
			"ITEM200-Variant",
			valuation_rate=10,
			variant_of=parent_item.name,
			attributes=[("Material Type", "Option 2")],
		)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()

		# Run synchronisation
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Get the updated items
		parent_item.reload()
		item.reload()

		# Expect newly created WooCommerce Product
		wc_product = self.get_woocommerce_product(
			product_id=item.woocommerce_servers[0].woocommerce_id,
			parent_id=parent_item.woocommerce_servers[0].woocommerce_id,
		)
		self.assertIn("id", wc_product)
		self.assertIsNotNone(wc_product["id"])

		# Expect attributes to be set
		self.assertEqual(len(wc_product["attributes"]), 1)
		self.assertEqual(wc_product["attributes"][0]["name"], "Material Type")
		self.assertEqual(wc_product["attributes"][0]["option"], "Option 2")

	@parameterized.expand(BATCH_MODES)
	def test_sync_create_new_wc_product_with_custom_fields_when_synchronising_with_woocommerce(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that the Item Synchronisation method syncs the mapped custom fields between
		a WooCommerce product and ERPNext Item.
		"""
		self._set_batch_mode(batch_enabled)

		dummy_meta_data = [{"id": 52824, "key": "_short_description_1", "value": "Test 1"}]
		# Setup
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		# Map Erpnext Item description to WC Product Meta Data with key '_short_description_1'
		wc_server.item_field_map = []
		row = wc_server.append("item_field_map")
		row.erpnext_field_name = "description | Description"
		row.woocommerce_field_name = "$.meta_data[?(@.key=='_short_description_1')].value"
		wc_server.save()
		# Create a new item in ERPNext and set a WooCommerce server but not a product ID
		item = create_item("ITEM102", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()

		# Run synchronisation
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect no errors logged
		mock_log_error.assert_not_called()

		# Get the updated item
		item.reload()

		# Expect newly created WooCommerce Product
		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)

		# Preset the WooCommerce Product's Metadata field and sync again
		self.update_woocommerce_product_metadata(wc_product["id"], dummy_meta_data)
		item.description = "Description from ERPNext"
		item.save()
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect correct custom mapped field values
		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)
		self.assertEqual(wc_product["meta_data"][0]["key"], "_short_description_1")
		self.assertEqual(wc_product["meta_data"][0]["value"], "Description from ERPNext")

		# Now we update the WooCommerce meta data and sync again
		new_meta_data = [
			{"id": 52824, "key": "_short_description_1", "value": "Final description from WooCommerce"}
		]
		self.update_woocommerce_product_metadata(wc_product["id"], new_meta_data)
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Get the updated item
		item.reload()

		# Expect correct custom mapped field values
		self.assertEqual(item.description, "Final description from WooCommerce")

	@parameterized.expand(BATCH_MODES)
	def test_sync_updates_variable_wc_product_that_has_no_price_of_its_own(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Test that pushing an Item update to a variable WooCommerce Product succeeds. A variable
		product carries no regular_price of its own - its price is derived from its variations.
		"""
		self._set_batch_mode(batch_enabled)

		# Create a variable product in WooCommerce and sync it inbound
		wc_product_id = self.post_woocommerce_product(
			product_name="ITEM103", type="variable", attributes=["Material Type"]
		)
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]

		# Make the ERPNext Item the newer of the two and clear the sync hash, which is what the
		# Item hook does before syncing, so that the change is pushed outbound
		item.item_name = "ITEM103 renamed"
		item.save()

		clear_sync_hash(item.name)
		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		# Expect no errors logged, and the change to have reached WooCommerce
		mock_log_error.assert_not_called()
		wc_product = self.get_woocommerce_product(product_id=wc_product_id)
		self.assertEqual(wc_product["name"], "ITEM103 renamed")

	@parameterized.expand(BATCH_MODES)
	def test_sync_variable_wc_product_without_attributes(self, mock_log_error, _name, batch_enabled):
		"""
		Test that a variable WooCommerce Product with no attributes still creates an Item.

		ERPNext requires an attribute on a template Item, and such a product has no variations,
		so it is created as a plain Item.
		"""
		self._set_batch_mode(batch_enabled)

		wc_product_id = self.post_woocommerce_product(product_name="ITEM104", type="variable", attributes=[])
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].has_variants, 0)
		self.assertEqual(len(items[0].attributes), 0)

	@parameterized.expand(BATCH_MODES)
	def test_sync_variation_inbound_with_image_sync_enabled(self, mock_log_error, _name, batch_enabled):
		"""
		Test that pulling a changed variation into ERPNext works while image sync is enabled.

		The scheduled inbound sync lists variations through their parent's variations endpoint,
		which reports a single `image` rather than the `images` array a product carries.
		"""
		self._set_batch_mode(batch_enabled)
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_image_sync = 1
		wc_server.save()

		wc_variation_id = self.post_woocommerce_product(
			product_name="ITEM105", type="variation", attributes=["Material Type"]
		)
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_variation_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		items = get_items_for_wc_product(wc_variation_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]
		wc_parent_id = get_variation_parent_woocommerce_id(self.wc_server.name, item.name)

		# Change the variation in WooCommerce, so that it is the newer of the two
		self.update_woocommerce_variation(
			wc_parent_id, wc_variation_id, {"description": "Changed in WooCommerce"}
		)
		clear_sync_hash(item.name)

		# Sync it the way the scheduled task does: list recently modified products, which walks
		# each variable product's variations, and sync the variation record that comes back
		wc_products = get_list_of_wc_products(date_time_from=add_to_date(now(), hours=-1))
		variation = next(
			product for product in wc_products if str(product.woocommerce_id) == str(wc_variation_id)
		)
		run_item_sync(woocommerce_product=variation)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

	@parameterized.expand(BATCH_MODES)
	def test_sync_variant_item_from_the_item_side(self, mock_log_error, _name, batch_enabled):
		"""
		Test that syncing a variant Item finds its WooCommerce variation.

		A variation is not listed by the products endpoint, so it can only be reached through
		its parent product.
		"""
		self._set_batch_mode(batch_enabled)

		wc_variation_id = self.post_woocommerce_product(
			product_name="ITEM106", type="variation", attributes=["Material Type"]
		)
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_variation_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		items = get_items_for_wc_product(wc_variation_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		item = items[0]

		# Sync from the Item side, the way the Item hook does
		clear_sync_hash(item.name)
		_item, wc_product = run_item_sync(item_code=item.name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

		# Expect the variation to have been found, and not its parent
		self.assertIsNotNone(wc_product)
		self.assertEqual(str(wc_product.woocommerce_id), str(wc_variation_id))
		self.assertEqual(wc_product.type, "variation")

	@parameterized.expand(BATCH_MODES)
	def test_sync_item_group_containing_an_ampersand(self, mock_log_error, _name, batch_enabled):
		"""
		Test that a mapped field whose WooCommerce value contains an HTML entity resolves.

		WooCommerce reports a category named "Sleeves & Toploader" as "Sleeves &amp; Toploader",
		which does not match the ERPNext Item Group of that name.
		"""
		self._set_batch_mode(batch_enabled)

		item_group = "Sleeves & Toploader"
		if not frappe.db.exists("Item Group", item_group):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": item_group,
					"parent_item_group": "All Item Groups",
				}
			).insert()

		# Map the ERPNext Item Group to the WooCommerce product's category
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.item_field_map = []
		row = wc_server.append("item_field_map")
		row.erpnext_field_name = "item_group | Item Group"
		row.woocommerce_field_name = "$.categories[0].name"
		wc_server.save()

		category_id = self.post_product_category(item_group)
		wc_product_id = self.post_woocommerce_product(product_name="ITEM107", category_ids=[category_id])
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].item_group, item_group)

	@parameterized.expand(BATCH_MODES)
	def test_sync_links_existing_item_by_sku(self, mock_log_error, _name, batch_enabled):
		"""
		Test that a product whose SKU matches an existing Item links to it instead of creating
		a second Item, when the server has "Match Items by SKU" enabled.
		"""
		self._set_batch_mode(batch_enabled)
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.match_items_by_sku = 1
		wc_server.save()

		item_code = f"SKU-MATCH-{frappe.generate_hash(length=6)}"
		create_item(item_code, valuation_rate=10, warehouse=default_warehouse, company=get_default_company())

		wc_product_id = self.post_woocommerce_product(product_name="ITEM108", sku=item_code)
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

		# Expect the existing Item to be linked, and no second Item created for this product
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].name, item_code)

	@parameterized.expand(BATCH_MODES)
	def test_sync_does_not_create_a_duplicate_item(self, mock_log_error, _name, batch_enabled):
		"""
		Test that syncing a product whose Item Code is already taken reuses that Item.

		With "Product SKU" naming the Item Code comes from the product's SKU, so a second sync of
		a product whose Item was created earlier would otherwise fail on a duplicate insert.
		"""
		self._set_batch_mode(batch_enabled)
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.name_by = "Product SKU"
		wc_server.save()

		item_code = f"SKU-DUP-{frappe.generate_hash(length=6)}"
		create_item(item_code, valuation_rate=10, warehouse=default_warehouse, company=get_default_company())

		wc_product_id = self.post_woocommerce_product(product_name="ITEM109", sku=item_code)
		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.wc_server.name, wc_product_id
		)
		run_item_sync(woocommerce_product_name=woocommerce_product_name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()
		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].name, item_code)

	@parameterized.expand(BATCH_MODES)
	def test_sync_sets_sku_on_a_new_wc_product(self, mock_log_error, _name, batch_enabled):
		"""
		Test that a WooCommerce Product created from an Item carries the Item Code as its SKU
		when the server names Items by SKU, so that it can be matched back later.
		"""
		self._set_batch_mode(batch_enabled)
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.name_by = "Product SKU"
		wc_server.save()

		item_code = f"SKU-OUT-{frappe.generate_hash(length=6)}"
		item = create_item(
			item_code, valuation_rate=10, warehouse=default_warehouse, company=get_default_company()
		)
		item.woocommerce_servers = []
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()

		run_item_sync(item_code=item.name)
		self._flush_if_batch()

		mock_log_error.assert_not_called()

		item.reload()
		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)
		self.assertEqual(wc_product["sku"], item_code)

	def _map_barcodes_with_transform(self):
		"""Map Item > Barcodes to the test meta key through the 'barcodes' Value Transform"""
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.item_field_map = []
		row = wc_server.append("item_field_map")
		row.erpnext_field_name = "barcodes | Barcodes"
		row.woocommerce_field_name = BARCODES_JSONPATH
		row.value_transform_method = "barcodes"
		wc_server.save()

	def _create_linked_item_with_barcode(self, item_code: str, barcode: str):
		"""An Item with a barcode and a WooCommerce Product created for it by the sync"""
		item = create_item(item_code, valuation_rate=10)
		item.set("barcodes", [])
		item.append("barcodes", {"barcode": barcode, "barcode_type": "EAN-13"})
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()

		run_item_sync(item_code=item.name)
		self._flush_if_batch()
		item.reload()
		return item

	def _barcodes_meta_on(self, product_id):
		product = self.get_woocommerce_product(product_id=product_id)
		return next(
			(meta["value"] for meta in product["meta_data"] if meta["key"] == BARCODES_META_KEY), None
		)

	@parameterized.expand(BATCH_MODES)
	def test_sync_creates_a_mapped_meta_row_for_a_child_table(self, mock_log_error, _name, batch_enabled):
		"""
		A transformed child table is pushed to a meta key that WordPress has never written. The filter
		matches nothing until the row exists, so the sync has to create it.
		"""
		self._set_batch_mode(batch_enabled)

		with registered_barcodes_transform():
			self._map_barcodes_with_transform()
			item = self._create_linked_item_with_barcode("ITEM120", "4006381333931")
			product_id = item.woocommerce_servers[0].woocommerce_id

			# The product the sync just created holds no meta at all
			self.assertIsNone(self._barcodes_meta_on(product_id))

			# Touch the Item so that it is the newer of the two, and sync again
			item.save()
			clear_sync_hash(item.name)
			run_item_sync(item_code=item.name)
			self._flush_if_batch()

		mock_log_error.assert_not_called()
		self.assertEqual(self._barcodes_meta_on(product_id), [{"code": "4006381333931", "kind": "EAN-13"}])

	@parameterized.expand(BATCH_MODES)
	def test_sync_updates_an_existing_mapped_meta_row_for_a_child_table(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		Once the meta row exists, a changed child table overwrites its value
		"""
		self._set_batch_mode(batch_enabled)

		with registered_barcodes_transform():
			self._map_barcodes_with_transform()
			item = self._create_linked_item_with_barcode("ITEM121", "5901234123457")
			product_id = item.woocommerce_servers[0].woocommerce_id

			# Seed the meta row with a stale value
			self.update_woocommerce_product_metadata(
				product_id, [{"key": BARCODES_META_KEY, "value": [{"code": "stale", "kind": "EAN-13"}]}]
			)

			item.save()
			clear_sync_hash(item.name)
			run_item_sync(item_code=item.name)
			self._flush_if_batch()

		mock_log_error.assert_not_called()
		self.assertEqual(self._barcodes_meta_on(product_id), [{"code": "5901234123457", "kind": "EAN-13"}])

	@parameterized.expand(BATCH_MODES)
	def test_sync_does_not_push_a_mapped_child_table_that_already_matches(
		self, mock_log_error, _name, batch_enabled
	):
		"""
		The transformed value has to compare equal to what WooCommerce echoes back, or every run would
		PATCH the product. WooCommerce bumps date_modified on any write, so that is what proves it.
		"""
		self._set_batch_mode(batch_enabled)

		with registered_barcodes_transform():
			self._map_barcodes_with_transform()
			item = self._create_linked_item_with_barcode("ITEM122", "4006381333948")
			product_id = item.woocommerce_servers[0].woocommerce_id

			# First push, which writes the meta row
			item.save()
			clear_sync_hash(item.name)
			run_item_sync(item_code=item.name)
			self._flush_if_batch()
			date_modified_after_push = self.get_woocommerce_product(product_id=product_id)["date_modified"]

			# Second push, with nothing changed on either side
			item.save()
			clear_sync_hash(item.name)
			run_item_sync(item_code=item.name)
			self._flush_if_batch()

		mock_log_error.assert_not_called()
		self.assertEqual(
			self.get_woocommerce_product(product_id=product_id)["date_modified"],
			date_modified_after_push,
		)
		# The value the first push wrote is what the comparison settled against
		self.assertEqual(self._barcodes_meta_on(product_id), [{"code": "4006381333948", "kind": "EAN-13"}])


def get_items_for_wc_product(woocommerce_id: str, woocommerce_server: str):
	"""
	Get ERPNext Item for a given WooCommerce Product and Server
	"""
	iws = frappe.qb.DocType("Item WooCommerce Server")
	itm = frappe.qb.DocType("Item")
	item_codes = (
		frappe.qb.from_(iws)
		.join(itm)
		.on(iws.parent == itm.name)
		.where(
			(iws.woocommerce_id == cstr(woocommerce_id))
			& (iws.woocommerce_server == woocommerce_server)
			& (itm.disabled == 0)
		)
		.select(iws.parent)
		.limit(1)
	).run(as_dict=True)

	return [frappe.get_doc("Item", item_code.parent) for item_code in item_codes]


def create_item_attribute(attribute_name: str):
	"""
	Create an Item Attribute
	"""
	if not frappe.db.exists("Item Attribute", attribute_name):
		# Create a Item Attribute
		item_attribute = frappe.get_doc({"doctype": "Item Attribute", "attribute_name": attribute_name})
		options = ["Option 1", "Option 2", "Option 3"]
		for option in options:
			row = item_attribute.append("item_attribute_values")
			row.attribute_value = option
			row.abbr = option.replace(" ", "")

		item_attribute.flags.ignore_mandatory = True
		item_attribute.insert()


def create_variant_item(
	item_code,
	is_stock_item=1,
	valuation_rate=0,
	stock_uom="Nos",
	warehouse="_Test Warehouse - _TC",
	is_customer_provided_item=None,
	customer=None,
	is_purchase_item=None,
	opening_stock=0,
	is_fixed_asset=0,
	asset_category=None,
	company="_Test Company",
	variant_of=None,
	attributes=None,
):
	if not frappe.db.exists("Item", item_code):
		item = frappe.new_doc("Item")
		item.item_code = item_code
		item.item_name = item_code
		item.description = item_code
		item.item_group = "All Item Groups"
		item.stock_uom = stock_uom
		item.is_stock_item = is_stock_item
		item.is_fixed_asset = is_fixed_asset
		item.asset_category = asset_category
		item.opening_stock = opening_stock
		item.valuation_rate = valuation_rate
		item.is_purchase_item = is_purchase_item
		item.is_customer_provided_item = is_customer_provided_item
		item.customer = customer or ""
		item.append("item_defaults", {"default_warehouse": warehouse, "company": company})
		item.variant_of = variant_of
		if attributes:
			for attribute, attribute_value in attributes:
				row = item.append("attributes")
				row.attribute = attribute
				row.attribute_value = attribute_value
		item.save()
	else:
		item = frappe.get_doc("Item", item_code)
	return item
