import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import get_default_naming_series, make_autoname

from woocommerce_fusion.tasks.sync_sales_orders import run_sales_order_sync
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


class CustomSalesOrder(SalesOrder):
	"""
	This class extends ERPNext's Sales Order doctype to override the autoname method
	This allows us to name the Sales Order conditionally.

	We also add logic to set the WooCommerce Status field on validate.
	"""

	def autoname(self):
		"""
		If this is a WooCommerce-linked order, use the naming series defined in "WooCommerce Server"
		or default to WEB[WooCommerce Order ID], e.g. WEB012142.
		Else, name it normally.
		"""
		if self.woocommerce_id and self.woocommerce_server:
			wc_server = frappe.get_cached_doc("WooCommerce Server", self.woocommerce_server)
			if wc_server.sales_order_series:
				self.name = make_autoname(key=wc_server.sales_order_series)
			else:
				# Get idx of site
				wc_servers = frappe.get_all("WooCommerce Server", fields=["name", "creation"])
				sorted_list = sorted(wc_servers, key=lambda server: server.creation)
				idx = next(
					(index for (index, d) in enumerate(sorted_list) if d["name"] == self.woocommerce_server),
					None,
				)
				self.name = f"WEB{idx + 1}-{int(self.woocommerce_id):06}"  # Format with leading zeros to make it 6 digits
		else:
			naming_series = get_default_naming_series("Sales Order")
			self.name = make_autoname(key=naming_series)

	def on_change(self):
		"""
		This is called when a document's values has been changed (including db_set).
		"""
		# If Sales Order Status Sync is enabled, update the WooCommerce status of the Sales Order
		if self.woocommerce_id and self.woocommerce_server:
			wc_server = frappe.get_cached_doc("WooCommerce Server", self.woocommerce_server)
			if wc_server.enable_so_status_sync:
				mapping = next(
					(
						row
						for row in wc_server.sales_order_status_map
						if row.erpnext_sales_order_status == self.status
					),
					None,
				)
				if mapping:
					if self.woocommerce_status != mapping.woocommerce_sales_order_status:
						frappe.db.set_value(
							"Sales Order",
							self.name,
							"woocommerce_status",
							mapping.woocommerce_sales_order_status,
						)
						frappe.enqueue(run_sales_order_sync, queue="long", sales_order_name=self.name)


@frappe.whitelist()
def get_woocommerce_order_shipment_trackings(doc: str):
	"""
	Fetches shipment tracking details from a WooCommerce order.
	"""
	doc = frappe._dict(json.loads(doc))
	if doc.woocommerce_server and doc.woocommerce_id:
		wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)
		if wc_order.shipment_trackings:
			return json.loads(wc_order.shipment_trackings)

	return []


@frappe.whitelist()
def update_woocommerce_order_shipment_trackings(doc: str, shipment_trackings: list):
	"""
	Updates the shipment tracking details of a specific WooCommerce order.
	"""
	doc = frappe._dict(json.loads(doc))
	if doc.woocommerce_server and doc.woocommerce_id:
		wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)
	wc_order.shipment_trackings = json.dumps(shipment_trackings)
	wc_order.save()
	return wc_order.shipment_trackings


def get_woocommerce_order(woocommerce_server, woocommerce_id):
	"""
	Retrieves a specific WooCommerce order based on its site and ID.
	"""
	# First verify if the WooCommerce site exits, and it sync is enabled
	wc_order_name = generate_woocommerce_record_name_from_domain_and_id(woocommerce_server, woocommerce_id)
	wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)

	if not wc_server:
		frappe.throw(
			_(
				"This Sales Order is linked to WooCommerce site '{0}', but this site can not be found in 'WooCommerce Servers'"
			).format(woocommerce_server)
		)

	if not wc_server.enable_sync:
		frappe.throw(
			_(
				"This Sales Order is linked to WooCommerce site '{0}', but Synchronisation for this site is disabled in 'WooCommerce Server'"
			).format(woocommerce_server)
		)

	wc_order = frappe.get_doc({"doctype": "WooCommerce Order", "name": wc_order_name})
	wc_order.load_from_db()
	return wc_order
