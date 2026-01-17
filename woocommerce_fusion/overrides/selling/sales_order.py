import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from frappe import _
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
        If this is a WooCommerce-linked order, use the naming series defined in "WooCommerce Server",
        or server abbreviation if set, or default to WEB[idx]-[WooCommerce Order ID], e.g. WEB1-012142.
        Else, name it normally.
        """
        if self.woocommerce_id and self.woocommerce_server:
            wc_server = frappe.get_cached_doc("WooCommerce Server", self.woocommerce_server)
            if wc_server.sales_order_series:
                self.name = make_autoname(key=wc_server.sales_order_series)
            elif getattr(wc_server, "server_abbreviation", None):
                # Use server abbreviation if set
                self.name = "{}-{:06}".format(
                    wc_server.server_abbreviation, int(self.woocommerce_id)
                )
            else:
                # Get idx of site
                wc_servers = frappe.get_all("WooCommerce Server", fields=["name", "creation"])
                sorted_list = sorted(wc_servers, key=lambda server: server.creation)
                idx = next(
                    (
                        index
                        for (index, d) in enumerate(sorted_list)
                        if d["name"] == self.woocommerce_server
                    ),
                    None,
                )
                self.name = "WEB{}-{:06}".format(
                    idx + 1, int(self.woocommerce_id)
                )  # Format with leading zeros to make it 6 digits
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
                        frappe.enqueue(
                            run_sales_order_sync, queue="long", sales_order_name=self.name
                        )


@frappe.whitelist()
def get_woocommerce_order_shipment_trackings(doc):
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
def get_woocommerce_order_payment_info(doc):
    """
    Fetches payment information from a WooCommerce order.
    Returns payment method, status (captured/refunded/no payment), and transaction details.
    """
    doc = frappe._dict(json.loads(doc))
    if not (doc.woocommerce_server and doc.woocommerce_id):
        return None

    wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)

    # Determine payment status
    payment_status = "No Payment"
    status_color = "orange"

    # Check for refunds
    has_refunds = False
    if wc_order.refunds:
        refunds_data = json.loads(wc_order.refunds) if isinstance(wc_order.refunds, str) else wc_order.refunds
        if refunds_data and len(refunds_data) > 0:
            has_refunds = True

    # Check WooCommerce order status for refunded
    if wc_order.status == "refunded" or has_refunds:
        payment_status = "Refunded"
        status_color = "red"
    elif wc_order.date_paid:
        payment_status = "Captured"
        status_color = "green"

    return {
        "payment_method": wc_order.payment_method_title or wc_order.payment_method or "",
        "payment_status": payment_status,
        "status_color": status_color,
        "transaction_id": wc_order.transaction_id or "",
        "date_paid": str(wc_order.date_paid) if wc_order.date_paid else "",
    }


@frappe.whitelist()
def update_woocommerce_order_shipment_trackings(doc, shipment_trackings):
    """
    Updates the shipment tracking details of a specific WooCommerce order.
    """
    doc = frappe._dict(json.loads(doc))
    if doc.woocommerce_server and doc.woocommerce_id:
        wc_order = get_woocommerce_order(doc.woocommerce_server, doc.woocommerce_id)
    wc_order.shipment_trackings = shipment_trackings
    wc_order.save()
    return wc_order.shipment_trackings


def get_woocommerce_order(woocommerce_server, woocommerce_id):
    """
    Retrieves a specific WooCommerce order based on its site and ID.
    """
    # First verify if the WooCommerce site exits, and it sync is enabled
    wc_order_name = generate_woocommerce_record_name_from_domain_and_id(
        woocommerce_server, woocommerce_id
    )
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
