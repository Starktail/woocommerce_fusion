import json
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def run_item_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item
	"""
	if (
		doc.doctype == "Item"
		and not doc.flags.get("created_by_sync", None)
		and len(doc.woocommerce_servers) > 0
	):
		# Check if batch queue is enabled
		settings = frappe.get_cached_doc("WooCommerce Integration Settings")
		use_batch_queue = settings.enable_batch_queue

		if use_batch_queue:
			# Add to batch queue for processing
			from woocommerce_fusion.tasks.batch_queue import add_to_batch_queue

			for wc_server_row in doc.woocommerce_servers:
				if wc_server_row.enabled:
					clear_sync_hash(item_code=doc.name)
					add_to_batch_queue(doc.name, wc_server_row.woocommerce_server)

			frappe.msgprint(
				_("Item {0} added to WooCommerce sync queue").format(frappe.bold(doc.name)),
				indicator="blue",
				alert=True,
			)
		else:
			# Use legacy immediate sync
			frappe.msgprint(
				_("Background sync to WooCommerce triggered for {0} {1}").format(
					frappe.bold(doc.name), method
				),
				indicator="blue",
				alert=True,
			)
			frappe.enqueue(clear_sync_hash_and_run_item_sync, item_code=doc.name)


@frappe.whitelist()
def run_item_sync(
	item_code: Optional[str] = None,
	item: Optional[Item] = None,
	woocommerce_product_name: Optional[str] = None,
	woocommerce_product: Optional[WooCommerceProduct] = None,
	enqueue=False,
) -> Tuple[Item, WooCommerceProduct]:
	"""
	Helper funtion that prepares arguments for item sync
	"""
	# Validate inputs, at least one of the parameters should be provided
	if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
		raise ValueError(
			(
				"At least one of item_code, item, woocommerce_product_name, woocommerce_product parameters required"
			)
		)

	# Get ERPNext Item and WooCommerce product if they exist
	if woocommerce_product or woocommerce_product_name:
		if not woocommerce_product:
			woocommerce_product = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": woocommerce_product_name}
			)
			woocommerce_product.load_from_db()

		# Trigger sync
		sync = SynchroniseItem(woocommerce_product=woocommerce_product)
		if enqueue:
			frappe.enqueue(sync.run)
		else:
			sync.run()

	elif item or item_code:
		if not item:
			item = frappe.get_doc("Item", item_code)
		if not item.woocommerce_servers:
			frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(item_code))
		for wc_server in item.woocommerce_servers:
			# Trigger sync for every linked server
			sync = SynchroniseItem(
				item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server.idx)
			)
			if enqueue:
				frappe.enqueue(sync.run)
			else:
				sync.run()

	return (
		sync.item.item if sync and sync.item else None,
		sync.woocommerce_product if sync else None,
	)


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from
	"""
	wc_settings = frappe.get_doc("WooCommerce Integration Settings")

	if not date_time_from:
		date_time_from = wc_settings.wc_last_sync_date_items

	# Validate
	if not date_time_from:
		error_text = _(
			"'Last Items Syncronisation Date' field on 'WooCommerce Integration Settings' is missing"
		)
		frappe.log_error(
			"WooCommerce Items Sync Task Error",
			error_text,
		)
		raise ValueError(error_text)

	wc_products = get_list_of_wc_products(date_time_from=date_time_from)
	for wc_product in wc_products:
		try:
			run_item_sync(woocommerce_product=wc_product, enqueue=True)
		# Skip items with errors, as these exceptions will be logged
		except Exception:
			pass

	frappe.db.set_single_value("WooCommerce Settings", "wc_last_sync_date_items", now())


@dataclass
class ERPNextItemToSync:
	"""Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

	item: Item
	item_woocommerce_server_idx: int

	@property
	def item_woocommerce_server(self):
		return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]


class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: List[WooCommerceServer | _dict] = None,
		item: Optional[ERPNextItemToSync] = None,
		woocommerce_product: Optional[WooCommerceProduct] = None,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product
		self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")

	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_item_or_product()
			self.sync_wc_product_with_erpnext_item()
		except Exception as err:
			try:
				woocommerce_product_dict = (
					self.woocommerce_product.as_dict()
					if isinstance(self.woocommerce_product, WooCommerceProduct)
					else self.woocommerce_product
				)
			except ValidationError as e:
				woocommerce_product_dict = self.woocommerce_product
			error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(self.item) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_item_or_product(self):
		"""
		If we have an ERPNext Item, get the corresponding WooCommerce Product
		If we have a WooCommerce Product, get the corresponding ERPNext Item
		"""
		if (
			self.item and not self.woocommerce_product and self.item.item_woocommerce_server.woocommerce_id
		):
			# Validate that this Item's WooCommerce Server has sync enabled
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)
			if not wc_server.enable_sync:
				raise SyncDisabledError(wc_server)

			wc_products = get_list_of_wc_products(item=self.item)
			if len(wc_products) == 0:
				raise ValueError(
					f"No WooCommerce Product found with ID {self.item.item_woocommerce_server.woocommerce_id} on {self.item.item_woocommerce_server.woocommerce_server}"
				)
			self.woocommerce_product = wc_products[0]

		if self.woocommerce_product and not self.item:
			self.get_erpnext_item()

	def get_erpnext_item(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		if not all(
			[self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]
		):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		iws = frappe.qb.DocType("Item WooCommerce Server")
		itm = frappe.qb.DocType("Item")

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (
			frappe.qb.from_(iws)
			.join(itm)
			.on(iws.parent == itm.name)
			.where(Criterion.all(and_conditions))
			.select(iws.parent, iws.name)
			.limit(1)
		).run(as_dict=True)

		found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None
		if found_item:
			self.item = ERPNextItemToSync(
				item=found_item,
				item_woocommerce_server_idx=next(
					server.idx for server in found_item.woocommerce_servers if server.name == item_codes[0].name
				),
			)

	def sync_wc_product_with_erpnext_item(self):
		"""
		Syncronise Item between ERPNext and WooCommerce
		"""
		if self.item and not self.woocommerce_product:
			# create missing product in WooCommerce
			self.create_woocommerce_product(self.item)
		elif self.woocommerce_product and not self.item:
			# create missing item in ERPNext
			self.create_item(self.woocommerce_product)
		elif self.item and self.woocommerce_product:
			# both exist, check sync hash
			if (
				self.woocommerce_product.woocommerce_date_modified
				!= self.item.item_woocommerce_server.woocommerce_last_sync_hash
			):
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(
					self.item.item.modified
				):
					self.update_item(self.woocommerce_product, self.item)
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) < get_datetime(
					self.item.item.modified
				):
					self.update_woocommerce_product(self.woocommerce_product, self.item)

	def update_item(self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync):
		"""
		Update the ERPNext Item with fields from it's corresponding WooCommerce Product
		"""
		item_dirty = False
		if item.item.item_name != woocommerce_product.woocommerce_name:
			item.item.item_name = woocommerce_product.woocommerce_name
			item_dirty = True

		fields_updated, item.item = self.set_item_fields(item=item.item)

		wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
		if wc_server.enable_image_sync:
			wc_product_images = json.loads(woocommerce_product.images)
			if len(wc_product_images) > 0:
				if item.item.image != wc_product_images[0]["src"]:
					item.item.image = wc_product_images[0]["src"]
					item_dirty = True

		if item_dirty or fields_updated:
			item.item.flags.created_by_sync = True
			item.item.save()

		self.set_sync_hash()

	def update_woocommerce_product(
		self, wc_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> None:
		"""
		Update the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		wc_product_dirty = False

		# Update properties
		if wc_product.woocommerce_name != item.item.item_name:
			wc_product.woocommerce_name = item.item.item_name
			wc_product_dirty = True

		product_fields_changed, wc_product = self.set_product_fields(wc_product, item)
		if product_fields_changed:
			wc_product_dirty = True

		if wc_product_dirty:
			wc_product.save()

		self.woocommerce_product = wc_product
		self.set_sync_hash()

	def create_woocommerce_product(self, item: ERPNextItemToSync) -> None:
		"""
		Create the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		if (
			item.item_woocommerce_server.woocommerce_server
			and item.item_woocommerce_server.enabled
			and not item.item_woocommerce_server.woocommerce_id
		):
			# Create a new WooCommerce Product doc
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})

			wc_product.type = "simple"

			# Handle variants
			if item.item.has_variants:
				wc_product.type = "variable"
				wc_product_attributes = []

				# Handle attributes
				for row in item.item.attributes:
					item_attribute = frappe.get_doc("Item Attribute", row.attribute)
					wc_product_attributes.append(
						{
							"name": row.attribute,
							"slug": row.attribute.lower().replace(" ", "_"),
							"visible": True,
							"variation": True,
							"options": [option.attribute_value for option in item_attribute.item_attribute_values],
						}
					)

				wc_product.attributes = json.dumps(wc_product_attributes)

			if item.item.variant_of:
				# Check if parent exists
				parent_item = frappe.get_doc("Item", item.item.variant_of)
				parent_item, parent_wc_product = run_item_sync(item_code=parent_item.item_code)
				wc_product.parent_id = parent_wc_product.woocommerce_id
				wc_product.type = "variation"

				# Handle attributes
				wc_product_attributes = [
					{
						"name": row.attribute,
						"slug": row.attribute.lower().replace(" ", "_"),
						"option": row.attribute_value,
					}
					for row in item.item.attributes
				]

				wc_product.attributes = json.dumps(wc_product_attributes)

			# Set properties
			wc_product.woocommerce_server = item.item_woocommerce_server.woocommerce_server
			wc_product.woocommerce_name = item.item.item_name
			wc_product.regular_price = get_item_price_rate(item) or "0"

			self.set_product_fields(wc_product, item)

			wc_product.insert()
			self.woocommerce_product = wc_product

			# Reload ERPNext Item
			item.item.reload()
			item.item_woocommerce_server.woocommerce_id = wc_product.woocommerce_id
			item.item.flags.created_by_sync = True
			item.item.save()

			self.set_sync_hash()

	def create_item(self, wc_product: WooCommerceProduct) -> None:
		"""
		Create an ERPNext Item from the given WooCommerce Product
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

		# Create Item
		item = frappe.new_doc("Item")

		# Handle variants' attributes
		if wc_product.type in ["variable", "variation"]:
			self.create_or_update_item_attributes(wc_product)
			wc_attributes = json.loads(wc_product.attributes)
			for wc_attribute in wc_attributes:
				row = item.append("attributes")
				row.attribute = wc_attribute["name"]
				if wc_product.type == "variation":
					row.attribute_value = wc_attribute["option"]

		# Handle variants
		if wc_product.type == "variable":
			item.has_variants = 1

		if wc_product.type == "variation":
			# Check if parent exists
			woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
				wc_product.woocommerce_server, wc_product.parent_id
			)
			parent_item, parent_wc_product = run_item_sync(
				woocommerce_product_name=woocommerce_product_name
			)
			item.variant_of = parent_item.item_code

		item.item_code = (
			wc_product.sku
			if wc_server.name_by == "Product SKU" and wc_product.sku
			else str(wc_product.woocommerce_id)
		)
		item.stock_uom = wc_server.uom or _("Nos")
		item.item_group = wc_server.item_group
		item.item_name = wc_product.woocommerce_name
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product.woocommerce_id
		row.woocommerce_server = wc_server.name
		item.flags.ignore_mandatory = True
		item.flags.created_by_sync = True

		if wc_server.enable_image_sync:
			wc_product_images = json.loads(wc_product.images)
			if len(wc_product_images) > 0:
				item.image = wc_product_images[0]["src"]

		modified, item = self.set_item_fields(item=item)
		item.flags.created_by_sync = True

		item.insert()

		self.item = ERPNextItemToSync(
			item=item,
			item_woocommerce_server_idx=next(
				iws.idx
				for iws in item.woocommerce_servers
				if iws.woocommerce_server == wc_product.woocommerce_server
			),
		)

		self.set_sync_hash()

	def create_or_update_item_attributes(self, wc_product: WooCommerceProduct):
		"""
		Create or update an Item Attribute
		"""
		if wc_product.attributes:
			wc_attributes = json.loads(wc_product.attributes)
			for wc_attribute in wc_attributes:
				if frappe.db.exists("Item Attribute", wc_attribute["name"]):
					# Get existing Item Attribute
					item_attribute = frappe.get_doc("Item Attribute", wc_attribute["name"])
				else:
					# Create a Item Attribute
					item_attribute = frappe.get_doc(
						{"doctype": "Item Attribute", "attribute_name": wc_attribute["name"]}
					)

				# Get list of attribute options.
				# In variable WooCommerce Products, it's a list with key "options"
				# In a WooCommerce Product variant, it's a single value with key "option"
				options = (
					wc_attribute["options"] if wc_product.type == "variable" else [wc_attribute["option"]]
				)

				# If no attributes values exist, or attribute values exist already but are different, remove and update them
				if len(item_attribute.item_attribute_values) == 0 or (
					len(item_attribute.item_attribute_values) > 0
					and set(options) != set([val.attribute_value for val in item_attribute.item_attribute_values])
				):
					item_attribute.item_attribute_values = []
					for option in options:
						row = item_attribute.append("item_attribute_values")
						row.attribute_value = option
						row.abbr = option.replace(" ", "")

				item_attribute.flags.ignore_mandatory = True
				if not item_attribute.name:
					item_attribute.insert()
				else:
					item_attribute.save()

	def set_item_fields(self, item: Item) -> Tuple[bool, Item]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		WooCommerce to ERPNext
		"""
		item_dirty = False
		if item and self.woocommerce_product:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_product.woocommerce_server
			)
			if wc_server.item_field_map:
				woocommerce_product_dict = (
					self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
						self.woocommerce_product.to_dict()
					)
				)
				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(woocommerce_product_dict)

					setattr(item, erpnext_item_field_name[0], woocommerce_product_field_matches[0].value)
					item_dirty = True
		return item_dirty, item

	def set_product_fields(
		self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> Tuple[bool, WooCommerceProduct]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		ERPNext to WooCommerce

		Returns true if woocommerce_product was changed
		"""
		wc_product_dirty = False
		if item and woocommerce_product:
			wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
			if wc_server.item_field_map:

				# Deserialize the WooCommerce Product's list and dict fields because we want to potentially perform
				# in-place updates on the whole dict using jsonpath-ng. Use the existing class method for this.
				wc_product_with_deserialised_fields = (
					woocommerce_product.deserialize_attributes_of_type_dict_or_list(woocommerce_product)
				)

				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")
					erpnext_item_field_value = getattr(item.item, erpnext_item_field_name[0])

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(wc_product_with_deserialised_fields)

					if len(woocommerce_product_field_matches) == 0:
						if woocommerce_product.name:
							# We're strict about existing WooCommerce Products, the field should exist
							raise ValueError(
								_("Field <code>{0}</code> not found in WooCommerce Product {1}").format(
									map.woocommerce_field_name, woocommerce_product.name
								)
							)
						else:
							# For new WooCommerce Products, the nested field may not exist yet, so don't stop the sync
							continue

					# JSONPath parsing typically returns a list, we'll only take the first value
					woocommerce_product_field_value = woocommerce_product_field_matches[0].value

					if erpnext_item_field_value != woocommerce_product_field_value:
						jsonpath_expr.update(wc_product_with_deserialised_fields, erpnext_item_field_value)
						wc_product_dirty = True

				if wc_product_dirty:
					# Re-serialize the WooCommerce Product's list and dict fields, because we deserialized earlier
					woocommerce_product = woocommerce_product.serialize_attributes_of_type_dict_or_list(
						wc_product_with_deserialised_fields
					)

		return wc_product_dirty, woocommerce_product

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value, as it does not call the ORM triggers
		and it does not update the modified timestamp (by using the update_modified parameter)
		"""
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)

		# If item was synchronised but the item is set not to sync, turn on the enabled flag
		# Items that are disabled for sync will still be synced if it is ordered on WooCommerce
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"enabled",
			1,
			update_modified=False,
		)


def get_list_of_wc_products(
	item: Optional[ERPNextItemToSync] = None, date_time_from: Optional[datetime] = None
) -> List[WooCommerceProduct]:
	"""
	Fetches a list of WooCommerce Products within a specified date range or linked with an Item, using pagination.

	At least one of date_time_from, item parameters are required
	"""
	if not any([date_time_from, item]):
		raise ValueError("At least one of date_time_from or item parameters are required")

	wc_records_per_page_limit = 100
	page_length = wc_records_per_page_limit
	new_results = True
	start = 0
	filters = []
	wc_products = []
	servers = None

	# Build filters
	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])
	if item:
		filters.append(["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id])
		servers = [item.item_woocommerce_server.woocommerce_server]

	while new_results:
		woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		new_results = woocommerce_product.get_list(
			args={
				"filters": filters,
				"page_lenth": page_length,
				"start": start,
				"servers": servers,
				"as_doc": True,
			}
		)
		for wc_product in new_results:
			wc_products.append(wc_product)
		start += page_length
		if len(new_results) < page_length:
			new_results = []

	return wc_products


def get_item_price_rate(item: ERPNextItemToSync):
	"""
	Get the Item Price if Item Price sync is enabled
	"""
	# Check if the Item Price sync is enabled
	wc_server = frappe.get_cached_doc(
		"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
	)
	if wc_server.enable_price_list_sync:
		item_prices = frappe.get_all(
			"Item Price",
			filters={"item_code": item.item.item_name, "price_list": wc_server.price_list},
			fields=["price_list_rate", "valid_upto"],
		)
		return next(
			(
				price.price_list_rate
				for price in item_prices
				if not price.valid_upto or price.valid_upto > now()
			),
			None,
		)


def clear_sync_hash_and_run_item_sync(item_code: str):
	if clear_sync_hash(item_code=item_code) > 0:
		run_item_sync(item_code=item_code, enqueue=True)


def clear_sync_hash(item_code: str) -> int:
	"""
	Clear the last sync hash value using db.set_value, as it does not call the ORM triggers
	and it does not update the modified timestamp (by using the update_modified parameter)
	"""
	iws = frappe.qb.DocType("Item WooCommerce Server")

	iwss = (
		frappe.qb.from_(iws).where(iws.enabled == 1).where(iws.parent == item_code).select(iws.name)
	).run(as_dict=True)

	for iws in iwss:
		frappe.db.set_value(
			"Item WooCommerce Server",
			iws.name,
			"woocommerce_last_sync_hash",
			None,
			update_modified=False,
		)

	return len(iwss)


@frappe.whitelist()
def batch_update_woocommerce_products(item_codes: Optional[List[str]] = None) -> Dict[str, any]:
	"""
	Batch create/update multiple WooCommerce products from ERPNext items.

	This function collects multiple items that need to be synced to WooCommerce,
	auto-detects whether each needs to be created or updated based on woocommerce_id,
	groups them by WooCommerce server, and sends batch requests to reduce API calls.

	Logic:
	- If item has no woocommerce_id → CREATE in WooCommerce
	- If item has woocommerce_id → UPDATE in WooCommerce (if changed)

	Args:
	        item_codes: List of ERPNext Item codes to sync. If None, will sync all items
	                           that need syncing based on their sync hash.

	Returns:
	        Dict containing summary of batch operations per server, including:
	        - created_count: Number of products created
	        - updated_count: Number of products updated
	        - items: Dict with lists of created and updated IDs

	Example:
	        batch_update_woocommerce_products(["ITEM-001", "ITEM-002", "ITEM-003"])
	"""
	if isinstance(item_codes, str):
		import json as json_lib

		item_codes = json_lib.loads(item_codes)

	# Get items to sync
	if item_codes:
		items_to_sync = [frappe.get_doc("Item", code) for code in item_codes]
	else:
		# Get all items that have WooCommerce servers configured
		items_to_sync = frappe.get_all("Item", filters={"disabled": 0}, fields=["name"])
		items_to_sync = [
			frappe.get_doc("Item", item.name)
			for item in items_to_sync
			if frappe.get_doc("Item", item.name).woocommerce_servers
		]

	# Group items by server and collect WooCommerce IDs to fetch
	items_by_server = {}  # {server: [(item, wc_server_row), ...]}
	wc_ids_by_server = {}  # {server: [wc_id, ...]}
	items_to_create_by_server = {}  # {server: [(item, wc_server_row), ...]}

	# Auto-detect operation type based on woocommerce_id
	for item in items_to_sync:
		for wc_server_row in item.woocommerce_servers:
			if not wc_server_row.enabled:
				continue

			server_name = wc_server_row.woocommerce_server

			if not wc_server_row.woocommerce_id:
				# No WooCommerce ID = CREATE operation
				if server_name not in items_to_create_by_server:
					items_to_create_by_server[server_name] = []
				items_to_create_by_server[server_name].append((item, wc_server_row))
			else:
				# Has WooCommerce ID = UPDATE operation
				if server_name not in items_by_server:
					items_by_server[server_name] = []
					wc_ids_by_server[server_name] = []
				items_by_server[server_name].append((item, wc_server_row))
				wc_ids_by_server[server_name].append(str(wc_server_row.woocommerce_id))

	# Now process items for batch operations
	updates_by_server = {}
	creates_by_server = {}
	items_processed = []

	# First, handle CREATE operations (items without woocommerce_id)
	for server_name, item_tuples in items_to_create_by_server.items():
		for item, wc_server_row in item_tuples:
			# Prepare create data for this product
			item_for_sync = ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server_row.idx)
			sync = SynchroniseItem(item=item_for_sync, woocommerce_product=None)

			# Build the create data
			create_data = {
				"type": "simple",
				"name": item.item_name,
				"regular_price": get_item_price_rate(item_for_sync) or "0",
			}

			# Handle variants
			if item.has_variants:
				create_data["type"] = "variable"
				# TODO: Handle attributes for variable products
			elif item.variant_of:
				create_data["type"] = "variation"
				# TODO: Handle parent_id and attributes for variations

			# Get field mappings
			product_fields_changed, temp_wc_product = sync.set_product_fields(
				frappe.get_doc({"doctype": "WooCommerce Product", "woocommerce_server": server_name}),
				item_for_sync,
			)

			if product_fields_changed:
				temp_wc_product_dict = temp_wc_product.to_dict()
				for key, value in temp_wc_product_dict.items():
					if key not in ["name", "modified", "doctype", "woocommerce_id"] and value:
						create_data[key] = value

			if server_name not in creates_by_server:
				creates_by_server[server_name] = []

			# Deserialize the create data
			create_data_deserialized = WooCommerceProduct.deserialize_attributes_of_type_dict_or_list(
				create_data
			)
			creates_by_server[server_name].append(create_data_deserialized)
			items_processed.append(
				{"item_code": item.name, "woocommerce_id": None, "server": server_name, "operation": "create"}
			)

	# Second, handle UPDATE operations (items with woocommerce_id)
	for server_name, item_tuples in items_by_server.items():
		# Fetch all WooCommerce products for this server using filter
		wc_ids = wc_ids_by_server[server_name]
		if not wc_ids:
			continue

		# Build filters - use "in" operator for multiple IDs
		filters = [["WooCommerce Product", "id", "in", wc_ids]]
		servers = [server_name]

		try:
			# Fetch products in bulk
			woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
			wc_products = woocommerce_product.get_list(
				args={
					"filters": filters,
					"page_length": 100,
					"start": 0,
					"servers": servers,
					"as_doc": True,
				}
			)
		except Exception:
			frappe.log_error("Batch Fetch WooCommerce Products Error", frappe.get_traceback())
			continue

		# Create a mapping of wc_id -> wc_product for quick lookup
		# Convert to int to ensure type consistency
		wc_products_map = {int(wc_product.woocommerce_id): wc_product for wc_product in wc_products}

		# Process each item with its corresponding WooCommerce product
		for item, wc_server_row in item_tuples:
			wc_product = wc_products_map.get(int(wc_server_row.woocommerce_id))
			if not wc_product:
				# Product doesn't exist in WooCommerce, skip
				continue

			# Check sync hash - skip if already in sync
			if wc_product.woocommerce_date_modified == wc_server_row.woocommerce_last_sync_hash:
				# Already in sync, skip
				continue

			# Check if update is needed based on modified timestamps
			# Only sync TO WooCommerce if ERPNext item is newer
			if get_datetime(wc_product.woocommerce_date_modified) >= get_datetime(item.modified):
				# WooCommerce is same age or newer, skip (should sync FROM WooCommerce, not TO)
				continue

			# Prepare update data for this product
			item_for_sync = ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server_row.idx)
			sync = SynchroniseItem(item=item_for_sync, woocommerce_product=wc_product)

			# Build the update data
			update_data = {"id": wc_product.woocommerce_id}

			# Update name if changed
			if wc_product.woocommerce_name != item.item_name:
				update_data["name"] = item.item_name

			# Get field mappings
			product_fields_changed, temp_wc_product = sync.set_product_fields(wc_product, item_for_sync)

			if product_fields_changed:
				# Get the changed fields by comparing
				wc_product_dict = wc_product.to_dict()
				temp_wc_product_dict = temp_wc_product.to_dict()

				for key, value in temp_wc_product_dict.items():
					if wc_product_dict.get(key) != value and key not in ["name", "modified", "doctype"]:
						update_data[key] = value

			# Only add to batch if there are actual changes
			if len(update_data) > 1:  # More than just the 'id'
				if server_name not in updates_by_server:
					updates_by_server[server_name] = []

				# Deserialize the update data
				update_data_deserialized = WooCommerceProduct.deserialize_attributes_of_type_dict_or_list(
					update_data
				)
				updates_by_server[server_name].append(update_data_deserialized)
				items_processed.append(
					{
						"item_code": item.name,
						"woocommerce_id": wc_product.woocommerce_id,
						"server": server_name,
						"operation": "update",
					}
				)

	# Execute batch operations for each server
	results = {}
	all_servers = set(list(creates_by_server.keys()) + list(updates_by_server.keys()))

	for server_name in all_servers:
		creates = creates_by_server.get(server_name, [])
		updates = updates_by_server.get(server_name, [])

		if not creates and not updates:
			continue

		try:
			result = WooCommerceProduct.db_batch(
				woocommerce_server=server_name,
				create=creates if creates else None,
				update=updates if updates else None,
			)

			results[server_name] = {
				"success": True,
				"created_count": len(result.get("create", [])),
				"updated_count": len(result.get("update", [])),
				"items": {
					"created": [c.get("id") for c in result.get("create", [])],
					"updated": [u.get("id") for u in result.get("update", [])],
				},
			}

			# Update sync hashes and woocommerce_ids for successfully synced items
			# Handle created items
			for create_record in result.get("create", []):
				# Find the corresponding item
				item_info = next(
					(
						i
						for i in items_processed
						if i["operation"] == "create" and i["server"] == server_name and i["woocommerce_id"] is None
					),
					None,
				)
				if item_info:
					# Update the woocommerce_id and sync hash
					iws = frappe.qb.DocType("Item WooCommerce Server")
					iws_records = (
						frappe.qb.from_(iws)
						.where(iws.parent == item_info["item_code"])
						.where(iws.woocommerce_server == server_name)
						.select(iws.name)
					).run(as_dict=True)

					if iws_records:
						frappe.db.set_value(
							"Item WooCommerce Server",
							iws_records[0].name,
							{
								"woocommerce_id": create_record["id"],
								"woocommerce_last_sync_hash": create_record.get("date_modified"),
							},
							update_modified=False,
						)
					# Mark as processed so we don't match it again
					item_info["woocommerce_id"] = create_record["id"]

			# Handle updated items
			for update_record in result.get("update", []):
				# Find the corresponding item
				item_info = next(
					(
						i
						for i in items_processed
						if i["operation"] == "update"
						and i["woocommerce_id"] == update_record["id"]
						and i["server"] == server_name
					),
					None,
				)
				if item_info:
					# Update the sync hash
					iws = frappe.qb.DocType("Item WooCommerce Server")
					iws_records = (
						frappe.qb.from_(iws)
						.where(iws.parent == item_info["item_code"])
						.where(iws.woocommerce_server == server_name)
						.where(iws.woocommerce_id == update_record["id"])
						.select(iws.name)
					).run(as_dict=True)

					if iws_records:
						frappe.db.set_value(
							"Item WooCommerce Server",
							iws_records[0].name,
							"woocommerce_last_sync_hash",
							update_record.get("date_modified"),
							update_modified=False,
						)

		except Exception as err:
			results[server_name] = {
				"success": False,
				"error": str(err),
				"items": {
					"created": [c.get("name", "") for c in creates],
					"updated": [u.get("id", "") for u in updates],
				},
			}
			frappe.log_error("WooCommerce Batch Operation Error", frappe.get_traceback())

	return {
		"total_items_processed": len(items_processed),
		"servers": results,
		"items": items_processed,
	}
