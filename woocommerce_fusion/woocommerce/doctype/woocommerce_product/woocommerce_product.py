# Copyright (c) 2024, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from dataclasses import dataclass
from typing import Dict

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
    field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}

    # use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
    # nosemgrep
    @staticmethod
    def get_list(args):
        products = WooCommerceProduct.get_list_of_records(args)

        # Track which parent IDs we've already fetched variants for
        processed_parent_ids = set()

        # Extend the list with product variants for variable products
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
            processed_parent_ids.add(id)

        # Check if any products are variations (have a parent_id)
        # For each variation, ensure we fetch all sibling variations from the parent
        variations_with_parents = [
            (product.get("parent_id"), product.get("id"))
            for product in products
            if product.get("parent_id") and product.get("parent_id") not in processed_parent_ids
        ]

        # Group by parent_id to avoid duplicate API calls
        parent_ids_to_fetch = {}
        for parent_id, variant_id in variations_with_parents:
            if parent_id not in parent_ids_to_fetch:
                parent_ids_to_fetch[parent_id] = variant_id

        # Fetch all variations for each parent that wasn't already processed
        for parent_id in parent_ids_to_fetch.keys():
            # First, fetch the parent product to get its name
            parent_endpoint = f"products/{parent_id}"
            parent_args = args.copy()
            parent_args["endpoint"] = parent_endpoint
            parent_products = WooCommerceProduct.get_list_of_records(parent_args)

            if parent_products:
                parent_product = parent_products[0]
                parent_woocommerce_name = parent_product.get("woocommerce_name") or parent_product.get(
                    "name"
                )

                # Now fetch all variations for this parent
                args["endpoint"] = f"products/{parent_id}/variations"
                args["metadata"] = {"parent_woocommerce_name": parent_woocommerce_name}
                variants = WooCommerceProduct.get_list_of_records(args)
                products.extend(variants)
                processed_parent_ids.add(parent_id)

        return products

    def after_load_from_db(self, product: Dict):
        product.pop("name")
        product = self.set_title(product)
        return product

    @classmethod
    def during_get_list_of_records(cls, product: Dict, args):
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
        if (
            args
            and (metadata := args.get("metadata"))
            and (set_name := metadata.get("woocommerce_name"))
        ):
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
    def override_woocommerce_name(product: Dict, name: str):
        product["woocommerce_name"] = name
        return product

    @staticmethod
    def get_variation_name(product: Dict, args):
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

    def before_db_insert(self, product: Dict):
        return self.clean_up_product_before_write(product)

    def before_db_update(self, product: Dict):
        return self.clean_up_product_before_write(product)

    def after_db_update(self):
        pass

    @staticmethod
    def clean_up_product_before_write(product):
        """
        Perform some tasks to make sure that an product is in the correct format for the WC API
        """

        # Convert back to string
        product["weight"] = str(product["weight"])
        product["regular_price"] = str(product["regular_price"])

        # Do not post Sale Price if it is 0
        if product["sale_price"] and float(product["sale_price"]) > 0:
            product["sale_price"] = str(product["sale_price"])
        else:
            product.pop("sale_price")

        # Set corrected properties
        product["name"] = str(product["woocommerce_name"])

        # Drop 'related_ids' field
        product.pop("related_ids")

        return product
