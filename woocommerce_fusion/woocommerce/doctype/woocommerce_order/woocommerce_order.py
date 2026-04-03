# Copyright (c) 2023, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from dataclasses import dataclass
from datetime import datetime

import frappe

from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.woocommerce.woocommerce_api import (
	WooCommerceAPI,
	WooCommerceResource,
	get_domain_and_id_from_woocommerce_record_name,
	log_and_raise_error,
)

WC_ORDER_DELIMITER = "~"

WC_ORDER_STATUS_MAPPING = {
	"Pending Payment": "pending",
	"On hold": "on-hold",
	"Failed": "failed",
	"Cancelled": "cancelled",
	"Processing": "processing",
	"Refunded": "refunded",
	"Shipped": "completed",
	"Ready for Pickup": "ready-pickup",
	"Picked up": "pickup",
	"Delivered": "delivered",
	"Processing LP": "processing-lp",
	"Draft": "checkout-draft",
	"Quote Sent": "gplsquote-req",
	"Trash": "trash",
	"Partially Shipped": "partial-shipped",
	"Dispatched Pickup": "dispatched-pickup",
}
WC_ORDER_STATUS_MAPPING_REVERSE = {v: k for k, v in WC_ORDER_STATUS_MAPPING.items()}

verify_ssl = not frappe._dev_server


@dataclass
class WooCommerceOrderAPI(WooCommerceAPI):
	"""Class for keeping track of a WooCommerce site."""

	wc_plugin_advanced_shipment_tracking: bool = False


class WooCommerceOrder(WooCommerceResource):
	"""
	Virtual doctype for WooCommerce Orders
	"""

	doctype = "WooCommerce Order"
	resource: str = "orders"

	@staticmethod
	def _init_api() -> list[WooCommerceAPI]:
		"""
		Initialise the WooCommerce API
		"""
		wc_servers = frappe.get_all("WooCommerce Server")
		wc_servers = [frappe.get_doc("WooCommerce Server", server.name) for server in wc_servers]

		wc_api_list = [
			WooCommerceOrderAPI(
				api=APIWithRequestLogging(
					url=server.woocommerce_server_url,
					consumer_key=server.api_consumer_key,
					consumer_secret=server.api_consumer_secret,
					version="wc/v3",
					timeout=40,
					verify_ssl=verify_ssl,
				),
				woocommerce_server_url=server.woocommerce_server_url,
				woocommerce_server=server.name,
				wc_plugin_advanced_shipment_tracking=server.wc_plugin_advanced_shipment_tracking,
			)
			for server in wc_servers
			if server.enable_sync == 1
		]

		return wc_api_list

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_list(args):
		return WooCommerceOrder.get_list_of_records(args)

	def after_load_from_db(self, order: dict):
		return self.get_additional_order_attributes(order)

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_count(args) -> int:
		return WooCommerceOrder.get_count_of_records(args)

	def before_db_update(self, order: dict):
		# Drop all fields except for 'status', 'shipment_trackings' and 'line_items'
		keys_to_pop = [
			key for key in order.keys() if key not in ("status", "shipment_trackings", "line_items")
		]
		for key in keys_to_pop:
			order.pop(key)

		return order

	def after_db_update(self):
		self.update_shipment_tracking()

	def get_additional_order_attributes(self, order: dict):
		"""
		Make API calls to WC to get additional order attributes, such as Tracking Data
		managed by an additional WooCommerce plugin
		"""
		# Verify that the WC API has been initialised
		if self.current_wc_api:
			# If the "Advanced Shipment Tracking" WooCommerce Plugin is enabled, make an additional
			# API call to get the tracking information
			if self.current_wc_api.wc_plugin_advanced_shipment_tracking:
				_wc_server_domain, order_id = get_domain_and_id_from_woocommerce_record_name(self.name)
				try:
					order["shipment_trackings"] = self.current_wc_api.api.get(
						f"orders/{order_id}/shipment-trackings"
					).json()

					# Attempt to fix broken date in date_shipped field from /shipment-trackings endpoint
					if "meta_data" in order:
						shipment_trackings_meta_data = next(
							(
								entry
								for entry in json.loads(order["meta_data"])
								if entry["key"] == "_wc_shipment_tracking_items"
							),
							None,
						)
						if shipment_trackings_meta_data:
							for shipment_tracking in order["shipment_trackings"]:
								shipment_tracking_meta_data = next(
									(
										entry
										for entry in shipment_trackings_meta_data["value"]
										if entry["tracking_id"] == shipment_tracking["tracking_id"]
									),
									None,
								)
								if shipment_tracking_meta_data:
									date_shipped = datetime.fromtimestamp(
										int(shipment_tracking_meta_data["date_shipped"])
									)
									shipment_tracking["date_shipped"] = date_shipped.strftime("%Y-%m-%d")

					order["shipment_trackings"] = json.dumps(order["shipment_trackings"])

				except Exception as err:
					log_and_raise_error(err)

		return order

	def update_shipment_tracking(self):
		"""
		Handle fields from "Advanced Shipment Tracking" WooCommerce Plugin
		Replace the current shipment_trackings with shipment_tracking.

		See https://docs.zorem.com/docs/ast-free/add-tracking-to-orders/shipment-tracking-api/#shipment-tracking-properties
		"""
		# Verify that the WC API has been initialised
		if not self.wc_api_list:
			self.init_api()

		# Parse the server domain and order_id from the Document name
		wc_server_domain, order_id = get_domain_and_id_from_woocommerce_record_name(self.name)

		# Select the relevant WooCommerce server
		self.current_wc_api = next(
			(api for api in self.wc_api_list if wc_server_domain in api.woocommerce_server_url), None
		)

		if self.current_wc_api.wc_plugin_advanced_shipment_tracking and self.shipment_trackings:
			# Verify if the 'shipment_trackings' field changed
			if self.shipment_trackings != self._doc_before_save.shipment_trackings:
				# Parse JSON
				new_shipment_tracking = json.loads(self.shipment_trackings)

				# Remove the tracking_id key-value pair
				for item in new_shipment_tracking:
					if "tracking_id" in item:
						item.pop("tracking_id")

				# Only the first shipment_tracking will be used
				tracking_info = new_shipment_tracking[0]
				tracking_info["replace_tracking"] = 1

				# Make the API Call
				try:
					response = self.current_wc_api.api.post(
						f"orders/{order_id}/shipment-trackings/", data=tracking_info
					)
				except Exception as err:
					log_and_raise_error(err, error_text="update_shipment_tracking failed")
				if response.status_code != 201:
					log_and_raise_error(error_text="update_shipment_tracking failed", response=response)
