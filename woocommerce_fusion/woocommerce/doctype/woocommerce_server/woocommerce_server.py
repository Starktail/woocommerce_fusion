# Copyright (c) 2023, Dirk van der Laarse and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.caching import redis_cache
from jsonpath_ng.ext import parse
from woocommerce import API

from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_STATUS_MAPPING,
)
from woocommerce_fusion.woocommerce.woocommerce_api import parse_domain_from_url

verify_ssl = not frappe._dev_server


class WooCommerceServer(Document):
	def autoname(self):
		"""
		Derive name from woocommerce_server_url field
		"""
		self.name = parse_domain_from_url(self.woocommerce_server_url)

	def validate(self):
		# Validate URL
		result = urlparse(self.woocommerce_server_url)
		if not all([result.scheme, result.netloc]):
			frappe.throw(_("Please enter a valid WooCommerce Server URL"))

		# Get Shipment Providers if the "Advanced Shipment Tracking" woocommerce plugin is used
		if self.enable_sync and self.wc_plugin_advanced_shipment_tracking:
			self.get_shipment_providers()

		if not self.secret:
			self.secret = frappe.generate_hash()

		self.validate_so_status_map()
		self.validate_item_map()
		self.validate_reserved_stock_setting()

	def validate_so_status_map(self):
		"""
		Validate Sales Order Status Map to have unique mappings
		"""
		erpnext_so_statuses = [map.erpnext_sales_order_status for map in self.sales_order_status_map]
		if len(erpnext_so_statuses) != len(set(erpnext_so_statuses)):
			frappe.throw(_("Duplicate ERPNext Sales Order Statuses found in Sales Order Status Map"))
		wc_so_statuses = [map.woocommerce_sales_order_status for map in self.sales_order_status_map]
		if len(wc_so_statuses) != len(set(wc_so_statuses)):
			frappe.throw(_("Duplicate WooCommerce Sales Order Statuses found in Sales Order Status Map"))

	def validate_item_map(self):
		"""
		Validate Item Map to have valid JSONPath expressions
		"""
		disallowed_fields = ["attributes"]

		# If the built-in image sync is enabled, disallow the image field in the item field map to avoid unexpected behavior
		if self.enable_image_sync:
			disallowed_fields.append("images")

		if self.item_field_map:
			for map in self.item_field_map:
				jsonpath_expr = map.woocommerce_field_name
				try:
					parse(jsonpath_expr)
				except Exception as e:
					frappe.throw(
						_("Invalid JSONPath syntax in Item Field Map Row {0}:<br><br><pre>{1}</pre>").format(
							map.idx, e
						)
					)

				for field in disallowed_fields:
					if field in jsonpath_expr:
						frappe.throw(_("Field '{0}' is not allowed in JSONPath expression").format(field))

	def validate_reserved_stock_setting(self):
		"""
		If 'Reserved Stock Adjustment' is enabled, make sure that 'Reserve Stock' in ERPNext is enabled
		"""
		if self.subtract_reserved_stock:
			if not frappe.db.get_single_value("Stock Settings", "enable_stock_reservation"):
				frappe.throw(
					_(
						"In order to enable 'Reserved Stock Adjustment', please enable 'Enable Stock Reservation' in 'ERPNext > Stock Settings > Stock Reservation'"
					)
				)

	def get_shipment_providers(self):
		"""
		Fetches the names of all shipment providers from a given WooCommerce server.

		This function uses the WooCommerce API to get a list of shipment tracking
		providers. If the request is successful and providers are found, the function
		returns a newline-separated string of all provider names.
		"""

		wc_api = API(
			url=self.woocommerce_server_url,
			consumer_key=self.api_consumer_key,
			consumer_secret=self.api_consumer_secret,
			version="wc/v3",
			timeout=40,
			verify_ssl=verify_ssl,
		)
		all_providers = wc_api.get("orders/1/shipment-trackings/providers").json()
		if all_providers:
			provider_names = [provider for country in all_providers for provider in all_providers[country]]
			self.wc_ast_shipment_providers = "\n".join(provider_names)

	@frappe.whitelist()
	@redis_cache(ttl=600)
	def get_item_docfields(self, doctype: str) -> list[dict]:
		"""
		Get a list of DocFields for the Item Doctype
		"""
		invalid_field_types = [
			"Column Break",
			"Fold",
			"Heading",
			"Read Only",
			"Section Break",
			"Tab Break",
			"Table",
			"Table MultiSelect",
		]
		docfields = frappe.get_all(
			"DocField",
			fields=["label", "name", "fieldname"],
			filters=[["fieldtype", "not in", invalid_field_types], ["parent", "=", doctype]],
		)
		custom_fields = frappe.get_all(
			"Custom Field",
			fields=["label", "name", "fieldname"],
			filters=[["fieldtype", "not in", invalid_field_types], ["dt", "=", doctype]],
		)
		return docfields + custom_fields

	@frappe.whitelist()
	@redis_cache(ttl=86400)
	def get_woocommerce_order_status_list(self) -> list[str]:
		"""
		Retrieve list of WooCommerce Order Statuses
		"""
		return [key for key in WC_ORDER_STATUS_MAPPING.keys()]


@frappe.whitelist()
def get_woocommerce_shipment_providers(woocommerce_server: str):
	"""
	Return the Shipment Providers for a given WooCommerce Server domain
	"""
	wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)
	return wc_server.wc_ast_shipment_providers
