import json
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from jsonpath_ng.ext import parse

from woocommerce_fusion.tasks.field_transforms import (
	SKIP,
	TO_ERPNEXT,
	TO_WOOCOMMERCE,
	apply_transform,
	get_registered_transforms,
	resolve_transform,
)
from woocommerce_fusion.tasks.sync_items import (
	ERPNextItemToSync,
	SynchroniseItem,
	create_filtered_jsonpath_target,
	normalise_child_rows,
	set_mapped_field_value,
)

DUMMY_HOOKS = {"dummy": ["woocommerce_fusion.tasks.test_field_transforms._dummy_transform"]}

# Patched over `get_registered_transforms` rather than over `frappe.get_hooks`: the sync legs run
# real queries, and a blanket `get_hooks` patch hands this dict to unrelated Frappe internals too
BARCODES_REGISTRY = {"barcodes": "woocommerce_fusion.tasks.test_field_transforms._barcodes_transform"}

META_KEY = "_test_barcodes"
BARCODES_JSONPATH = f"$.meta_data[?key='{META_KEY}'].value"


def _dummy_transform(value, *, direction, item, woocommerce_product, row):
	"""Registered under the name 'dummy' by the tests below"""
	if direction == TO_ERPNEXT:
		return SKIP
	return {"seen": value, "row": row.woocommerce_field_name}


def _barcodes_transform(value, *, direction, item, woocommerce_product, row):
	"""
	Outbound-only transform of Item > Barcodes into a differently-keyed list of dicts, standing in
	for a real child table mapping
	"""
	if direction != TO_WOOCOMMERCE:
		return SKIP

	return [{"code": barcode.barcode, "kind": barcode.barcode_type} for barcode in value or []]


class TestTransformRegistry(UnitTestCase):
	def test_get_registered_transforms_unwraps_frappes_listified_hook_values(self):
		"""
		Frappe listifies the values of dict-shaped hooks, so the registry has to unwrap them
		"""
		with patch("frappe.get_hooks", return_value={"one": ["app_a.transform"], "two": ["app_b.transform"]}):
			self.assertEqual(
				get_registered_transforms(), {"one": "app_a.transform", "two": "app_b.transform"}
			)

	def test_get_registered_transforms_lets_the_last_app_win(self):
		"""
		Where two apps register the same name, the last one loaded should win
		"""
		with patch("frappe.get_hooks", return_value={"one": ["app_a.transform", "app_b.transform"]}):
			self.assertEqual(get_registered_transforms(), {"one": "app_b.transform"})

	def test_resolve_transform_throws_for_an_unregistered_name(self):
		"""
		An unregistered dotted path must not be callable - that is the whole point of the registry
		"""
		with patch("frappe.get_hooks", return_value={}):
			with self.assertRaises(frappe.ValidationError):
				resolve_transform("frappe.utils.password.get_decrypted_password")

	def test_resolve_transform_returns_the_registered_callable(self):
		with patch("frappe.get_hooks", return_value=DUMMY_HOOKS):
			self.assertIs(resolve_transform("dummy"), _dummy_transform)


class TestApplyTransform(UnitTestCase):
	def test_apply_transform_returns_the_value_untouched_when_no_transform_is_set(self):
		row = frappe._dict({"value_transform_method": None, "woocommerce_field_name": "$.sku"})

		self.assertEqual(
			apply_transform(row, "ABC", direction=TO_WOOCOMMERCE, item=None, woocommerce_product=None),
			"ABC",
		)

	def test_apply_transform_passes_the_row_and_direction_through(self):
		row = frappe._dict(
			{"value_transform_method": "dummy", "woocommerce_field_name": "$.meta_data[0].value"}
		)

		with patch("frappe.get_hooks", return_value=DUMMY_HOOKS):
			self.assertEqual(
				apply_transform(row, "ABC", direction=TO_WOOCOMMERCE, item=None, woocommerce_product=None),
				{"seen": "ABC", "row": "$.meta_data[0].value"},
			)

	def test_apply_transform_can_decline_a_direction(self):
		row = frappe._dict({"value_transform_method": "dummy", "woocommerce_field_name": "$.sku"})

		with patch("frappe.get_hooks", return_value=DUMMY_HOOKS):
			self.assertIs(
				apply_transform(row, "ABC", direction=TO_ERPNEXT, item=None, woocommerce_product=None),
				SKIP,
			)


class TestSetMappedFieldValue(UnitTestCase):
	"""
	`set_mapped_field_value` decides whether the Item gets saved. A false positive bumps
	`Item.modified`, which makes the Item look newer than the WooCommerce Product on the next run and
	flips the sync direction, so "unchanged" has to mean unchanged.
	"""

	def test_unchanged_scalar_is_not_dirty(self):
		item = frappe.get_doc({"doctype": "Item", "description": "A widget"})

		self.assertFalse(set_mapped_field_value(item, "description", "A widget"))

	def test_changed_scalar_is_dirty_and_is_written(self):
		item = frappe.get_doc({"doctype": "Item", "description": "A widget"})

		self.assertTrue(set_mapped_field_value(item, "description", "A better widget"))
		self.assertEqual(item.description, "A better widget")

	def test_unchanged_child_table_is_not_dirty(self):
		"""
		Rows built by a transform have no name/idx/parent, so a naive comparison against the stored
		`Document` rows would always differ
		"""
		item = frappe.get_doc({"doctype": "Item"})
		item.append("barcodes", {"barcode": "123", "barcode_type": "EAN"})

		self.assertFalse(
			set_mapped_field_value(item, "barcodes", [{"barcode": "123", "barcode_type": "EAN"}])
		)

	def test_changed_child_table_is_dirty_and_replaces_the_rows(self):
		item = frappe.get_doc({"doctype": "Item"})
		item.append("barcodes", {"barcode": "123", "barcode_type": "EAN"})

		self.assertTrue(set_mapped_field_value(item, "barcodes", [{"barcode": "456", "barcode_type": "EAN"}]))
		self.assertEqual(len(item.barcodes), 1)
		self.assertEqual(item.barcodes[0].barcode, "456")

	def test_emptying_a_child_table_is_dirty(self):
		item = frappe.get_doc({"doctype": "Item"})
		item.append("barcodes", {"barcode": "123", "barcode_type": "EAN"})

		self.assertTrue(set_mapped_field_value(item, "barcodes", []))
		self.assertEqual(item.barcodes, [])

	def test_normalise_child_rows_collapses_blanks_and_drops_metadata(self):
		"""
		A stored row carries idx/parent and "" for unset fields; an incoming dict carries neither
		"""
		item = frappe.get_doc({"doctype": "Item"})
		item.append("barcodes", {"barcode": "123", "barcode_type": "EAN", "uom": ""})

		self.assertEqual(
			normalise_child_rows(item.barcodes, "Item Barcode"),
			normalise_child_rows([{"barcode": "123", "barcode_type": "EAN"}], "Item Barcode"),
		)


class TestChildTableFieldMapping(UnitTestCase):
	"""
	End-to-end coverage of both legs of a child table field map, using Item > Barcodes and an
	outbound-only transform
	"""

	def setUp(self):
		self.server = frappe.new_doc("WooCommerce Server")
		self.server.woocommerce_server_url = "https://example.com"
		self.server.name = "example.com"
		row = self.server.append("item_field_map")
		row.erpnext_field_name = "barcodes | Barcodes"
		row.woocommerce_field_name = BARCODES_JSONPATH
		row.value_transform_method = "barcodes"

		self.sync = SynchroniseItem(servers=[self.server])

	def make_item(self):
		item = frappe.get_doc({"doctype": "Item", "item_code": "TEST-BARCODES"})
		iws = item.append("woocommerce_servers")
		iws.woocommerce_server = "example.com"
		iws.woocommerce_id = "1"
		item.append("barcodes", {"barcode": "123", "barcode_type": "EAN"})
		return ERPNextItemToSync(item=item, item_woocommerce_server_idx=1)

	def make_product(self, value):
		product = frappe.get_doc({"doctype": "WooCommerce Product"})
		product.name = "example.com-1"
		product.woocommerce_server = "example.com"
		product.woocommerce_id = 1
		product.meta_data = json.dumps(
			[{"id": 1, "key": "_other", "value": "x"}, {"id": 2, "key": META_KEY, "value": value}]
		)
		return product

	def documents_on(self, product):
		return next(meta["value"] for meta in json.loads(product.meta_data) if meta["key"] == META_KEY)

	def test_outbound_writes_the_transformed_value_into_the_mapped_meta_row(self):
		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
		):
			dirty, product = self.sync.set_product_fields(self.make_product([]), self.make_item())

		self.assertTrue(dirty)
		self.assertEqual(self.documents_on(product), [{"code": "123", "kind": "EAN"}])
		# Unrelated meta rows have to survive, and the field has to go back out as a JSON string
		self.assertEqual(
			next(meta["value"] for meta in json.loads(product.meta_data) if meta["key"] == "_other"), "x"
		)
		self.assertIsInstance(product.meta_data, str)

	def test_outbound_is_not_dirty_when_woocommerce_already_matches(self):
		"""
		The comparison happens on the *transformed* value. Comparing the raw child rows against the
		WooCommerce payload would never settle, and every sync run would PATCH the product.
		"""
		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
		):
			_dirty, product = self.sync.set_product_fields(self.make_product([]), self.make_item())
			dirty_again, _product = self.sync.set_product_fields(
				self.make_product(self.documents_on(product)), self.make_item()
			)

		self.assertFalse(dirty_again)

	def make_product_without_the_mapped_meta_row(self):
		product = frappe.get_doc({"doctype": "WooCommerce Product"})
		product.name = "example.com-1"
		product.woocommerce_server = "example.com"
		product.woocommerce_id = 1
		product.meta_data = json.dumps([{"id": 1, "key": "_other", "value": "x"}])
		return product

	def test_outbound_creates_a_meta_row_that_wordpress_has_never_written(self):
		"""
		A filter can only select from what is already in the list, so a product whose meta key has
		never been written matched nothing and used to fail its whole sync with a ValueError
		"""
		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
		):
			dirty, product = self.sync.set_product_fields(
				self.make_product_without_the_mapped_meta_row(), self.make_item()
			)

		self.assertTrue(dirty)
		self.assertEqual(self.documents_on(product), [{"code": "123", "kind": "EAN"}])

		meta_data = json.loads(product.meta_data)
		# WooCommerce creates the row from a key/value pair, so the new entry carries no id of its own
		created = next(meta for meta in meta_data if meta["key"] == META_KEY)
		self.assertEqual(set(created.keys()), {"key", "value"})
		# and the rows that were already there survive
		self.assertEqual([meta["key"] for meta in meta_data], ["_other", META_KEY])

	def test_a_created_meta_row_is_not_dirty_on_the_next_run(self):
		"""
		The row is created with the value WooCommerce will echo back, so the run after the one that
		created it settles instead of PATCHing the product again
		"""
		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
		):
			_dirty, product = self.sync.set_product_fields(
				self.make_product_without_the_mapped_meta_row(), self.make_item()
			)
			dirty_again, _product = self.sync.set_product_fields(
				self.make_product(self.documents_on(product)), self.make_item()
			)

		self.assertFalse(dirty_again)

	def test_a_target_that_cannot_be_created_still_raises(self):
		"""
		Only a filtered entry can be created, since the filter says what the missing entry looks like.
		Everything else stays an error, so a mistyped JSONPath is still reported.
		"""
		self.server.item_field_map[0].woocommerce_field_name = "$.meta_data[7].value"

		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
			self.assertRaises(ValueError),
		):
			self.sync.set_product_fields(self.make_product([]), self.make_item())

	def test_inbound_leaves_the_item_alone_when_the_transform_declines(self):
		self.sync.woocommerce_product = self.make_product([{"code": "123", "kind": "EAN"}])
		item = self.make_item().item
		item.set("barcodes", [])

		with (
			patch(
				"woocommerce_fusion.tasks.field_transforms.get_registered_transforms",
				return_value=BARCODES_REGISTRY,
			),
			patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server),
		):
			item_dirty, item = self.sync.set_item_fields(item=item)

		self.assertFalse(item_dirty)
		self.assertEqual(list(item.get("barcodes")), [])


class TestScalarFieldMappingDirtiness(UnitTestCase):
	"""
	`set_item_fields` used to mark the Item dirty for every mapped field on every run, whether the
	value had changed or not. Each needless save bumps `Item.modified` and flips the sync direction on
	the next run.
	"""

	def setUp(self):
		self.server = frappe.new_doc("WooCommerce Server")
		self.server.woocommerce_server_url = "https://example.com"
		self.server.name = "example.com"
		self.row = self.server.append("item_field_map")
		self.row.erpnext_field_name = "description | Description"
		self.row.woocommerce_field_name = "$.short_description"

		self.sync = SynchroniseItem(servers=[self.server])
		self.sync.woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		self.sync.woocommerce_product.name = "example.com-1"
		self.sync.woocommerce_product.woocommerce_server = "example.com"
		self.sync.woocommerce_product.short_description = "A widget"

	def set_item_fields(self, item):
		with patch("woocommerce_fusion.tasks.sync_items.frappe.get_cached_doc", return_value=self.server):
			return self.sync.set_item_fields(item=item)

	def test_already_in_sync_is_not_dirty(self):
		item_dirty, _item = self.set_item_fields(
			frappe.get_doc({"doctype": "Item", "description": "A widget"})
		)

		self.assertFalse(item_dirty)

	def test_out_of_sync_is_dirty_and_is_written(self):
		item_dirty, item = self.set_item_fields(frappe.get_doc({"doctype": "Item", "description": "Stale"}))

		self.assertTrue(item_dirty)
		self.assertEqual(item.description, "A widget")

	def test_an_absent_jsonpath_match_leaves_the_item_alone(self):
		"""
		Previously this raised IndexError on the first mapped field that was missing on the product
		"""
		self.row.woocommerce_field_name = "$.meta_data[?key='_never_written'].value"
		self.sync.woocommerce_product.meta_data = json.dumps([])

		item_dirty, item = self.set_item_fields(frappe.get_doc({"doctype": "Item", "description": "Keep me"}))

		self.assertFalse(item_dirty)
		self.assertEqual(item.description, "Keep me")


class TestCreateFilteredJsonpathTarget(UnitTestCase):
	"""
	`$.meta_data[?key='x'].value` matches nothing until WordPress has written that meta row. The
	entry the filter selects is created so that a value can be written to it.
	"""

	@staticmethod
	def create(jsonpath: str, doc) -> bool:
		return create_filtered_jsonpath_target(parse(jsonpath), doc)

	def test_appends_the_entry_the_filter_is_looking_for(self):
		doc = {"meta_data": [{"key": "_other", "value": "x"}]}

		self.assertTrue(self.create("$.meta_data[?key='_new'].value", doc))
		self.assertEqual(doc["meta_data"], [{"key": "_other", "value": "x"}, {"key": "_new"}])

	def test_the_created_entry_can_then_be_written_to(self):
		doc = {"meta_data": []}
		jsonpath_expr = parse("$.meta_data[?key='_new'].value")

		create_filtered_jsonpath_target(jsonpath_expr, doc)
		jsonpath_expr.update_or_create(doc, ["written"])

		self.assertEqual([match.value for match in jsonpath_expr.find(doc)], [["written"]])

	def test_handles_a_filter_on_more_than_one_field(self):
		doc = {"attributes": []}

		self.assertTrue(self.create("$.attributes[?name='Colour' & position=0].options", doc))
		# The literals keep the type they were written with, so a number does not go out as a string
		self.assertEqual(doc["attributes"], [{"name": "Colour", "position": 0}])

	def test_declines_a_plain_field_path(self):
		"""
		A JSONPath with no filter says nothing about what a missing target should look like, so a
		mistyped field name keeps being reported as an error instead of being created
		"""
		self.assertFalse(self.create("$.short_descriptionx", {}))

	def test_declines_an_index_that_is_out_of_range(self):
		self.assertFalse(self.create("$.meta_data[7].value", {"meta_data": []}))

	def test_declines_a_filter_that_is_not_an_equality_test(self):
		doc = {"meta_data": [{"key": "_other", "id": 1}]}

		self.assertFalse(self.create("$.meta_data[?id>5].value", doc))
		# A bare existence filter does not say what value the field should hold
		self.assertFalse(self.create("$.meta_data[?key].value", doc))
		self.assertEqual(len(doc["meta_data"]), 1)

	def test_declines_a_container_that_is_not_a_list(self):
		"""
		A product that has not been created in WooCommerce yet holds no `meta_data` to append to
		"""
		self.assertFalse(self.create("$.meta_data[?key='_new'].value", {"meta_data": None}))
		self.assertFalse(self.create("$.meta_data[?key='_new'].value", {}))
