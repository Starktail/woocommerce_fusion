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
		self.validate_batch_settings()
		self.validate_tax_account_uniqueness()

	def validate_tax_account_uniqueness(self):
		"""
		On Frappe v16+, ERPNext builds the per-item tax map keyed by account head with
		last-write-wins semantics (erpnext.stock.get_item_details.get_item_tax_map). When a
		"Sales Taxes and Charges Template" with a calculated ("On Net Total") VAT row is used,
		an additional "Actual" tax line (e.g. shipping or fee tax) re-using the same account head
		overwrites the VAT rate with the Actual line's (empty) rate, zeroing the calculated VAT on
		the synced Sales Order. Require distinct tax accounts to avoid this.

		Only the template path produces a calculated tax row; the "Actual" tax type is unaffected
		(no calculated row exists to be poisoned).
		"""
		if int(frappe.__version__.split(".")[0]) < 16:
			return

		if self.use_actual_tax_type or not self.enable_tax_lines_sync:
			return
		if not self.sales_taxes_and_charges_template:
			return

		template = frappe.get_cached_doc(
			"Sales Taxes and Charges Template", self.sales_taxes_and_charges_template
		)
		template_accounts = {row.account_head for row in template.taxes if row.account_head}

		for fieldname in ("f_n_f_tax_account", "tax_account_for_order_fee_lines"):
			account = self.get(fieldname)
			if account and account in template_accounts:
				frappe.throw(
					_(
						"On Frappe v16, '{0}' ({1}) may not re-use a tax account from the Sales Taxes "
						"and Charges Template '{2}'. Sharing a tax account zeroes the calculated tax on "
						"synced Sales Orders. Please configure a separate tax account."
					).format(
						_(self.meta.get_label(fieldname)), account, self.sales_taxes_and_charges_template
					)
				)

	def validate_batch_settings(self):
		"""
		Validate Batch API flush interval and batch size limits
		"""
		if self.enable_batch_api:
			if not 1 <= (self.batch_flush_interval_minutes or 1) <= 60:
				frappe.throw(_("Flush Interval must be between 1 and 60 minutes"))
			if not 1 <= (self.batch_size_limit or 100) <= 100:
				frappe.throw(_("Batch Size Limit must be between 1 and 100"))

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
							map.idx, str(e)
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
