# Copyright (c) 2024, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from dataclasses import dataclass
from typing import ClassVar

from woocommerce_fusion.woocommerce.woocommerce_api import WooCommerceAPI, WooCommerceResource


@dataclass
class WooCommerceProductAPI(WooCommerceAPI):
	"""Class for keeping track of a WooCommerce site."""

	pass


class WooCommerceProduct(WooCommerceResource):
	"""
	Virtual doctype for WooCommerce Products
	"""

	doctype = "WooCommerce Product"
	resource: str = "products"
	child_resource: str = "variations"
	field_setter_map: ClassVar[dict[str, str]] = {
		"woocommerce_name": "name",
		"woocommerce_id": "id",
	}

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_list(args):
		products = WooCommerceProduct.get_list_of_records(args)

		# Extend the list with product variants
		products_with_variants = [
			(product.get("id"), product.get("woocommerce_name"))
			for product in products
			if product.get("type") == "variable"
		]
		for id, woocommerce_name in products_with_variants:
			args["endpoint"] = f"products/{id}/variations"
			args["metadata"] = {"parent_woocommerce_name": woocommerce_name}
			variants = WooCommerceProduct.get_list_of_records(args)
			products.extend(variants)

		return products

	def after_load_from_db(self, product: dict):
		product.pop("name")
		product = self.set_title(product)
		return product

	@classmethod
	def during_get_list_of_records(cls, product: dict, args):
		# In the case of variations
		if product["parent_id"]:
			# Woocommerce product variantions endpoint results doesn't return the type, so set it manually
			product["type"] = "variation"

			if variation_name := cls.get_variation_name(product, args):
				# Set the name in args, for use by set_title()
				args["metadata"]["woocommerce_name"] = variation_name

				# Override the woocommerce_name field
				product = cls.override_woocommerce_name(product, variation_name)

		product = cls.set_title(product, args)
		return product

	@staticmethod
	def set_title(product: dict, args=None):
		if args and (metadata := args.get("metadata")) and (set_name := metadata.get("woocommerce_name")):
			product["title"] = set_name
		elif wc_name := product.get("woocommerce_name"):
			if sku := product.get("sku"):
				product["title"] = f"{sku} - {wc_name}"
			else:
				product["title"] = wc_name
		else:
			product["title"] = product["woocommerce_id"]

		return product

	@staticmethod
	def override_woocommerce_name(product: dict, name: str):
		product["woocommerce_name"] = name
		return product

	@staticmethod
	def get_variation_name(product: dict, args):
		# If this is a variation, we expect the variation's parent name in the metadata, then we can
		# build an item name in the format of {parent_name}, {attribute 1}, {attribute n}
		if (
			(product["type"] == "variation")
			and (metadata := args.get("metadata"))
			and (attributes := product.get("attributes"))
			and (parent_wc_name := metadata.get("parent_woocommerce_name"))
		):
			attr_values = [attr["option"] for attr in json.loads(attributes)]
			return parent_wc_name + " - " + ", ".join(attr_values)
		return None

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_count(args) -> int:
		return WooCommerceProduct.get_count_of_records(args)

	def before_db_insert(self, product: dict):
		return self.clean_up_product_before_write(product)

	def before_db_update(self, product: dict):
		return self.clean_up_product_before_write(product)

	def after_db_update(self):
		pass

	@staticmethod
	def clean_up_product_before_write(product: dict) -> dict:
		"""
		Perform some tasks to make sure that a product is in the correct format for the WC API
		"""

		if product.get("type") == "variation" or product.get("parent_id"):
			product["status"] = "publish"

		# Convert numeric fields back to strings required by WC API
		product["weight"] = str(product["weight"])
		product["regular_price"] = str(product["regular_price"])

		# Sale price: send empty string to clear an existing sale, or string value to set one.
		# Popping the field entirely has no effect on an existing WC sale price.
		if product.get("sale_price") and float(product["sale_price"]) > 0:
			product["sale_price"] = str(product["sale_price"])
		else:
			product["sale_price"] = ""

		# Sale date fields: normalise to ISO 8601 string, or None to clear via API
		for date_field in ("date_on_sale_from", "date_on_sale_to"):
			value = product.get(date_field)
			if value:
				product[date_field] = (
					value.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(value, "strftime") else str(value)
				)
			else:
				product[date_field] = None

		# Set corrected properties
		product["name"] = str(product["woocommerce_name"])

		# Drop 'related_ids' field
		product.pop("related_ids")

		return product
