import math

import frappe

from woocommerce_fusion.tasks.utils import APIWithRequestLogging

verify_ssl = not frappe._dev_server


def update_stock_levels_for_woocommerce_item(doc, method):
    if not frappe.flags.in_test:
        if doc.doctype in (
            "Stock Entry",
            "Stock Reconciliation",
            "Sales Invoice",
            "Delivery Note",
        ):
            # Check if there are any enabled WooCommerce Servers with stock sync enabled
            if (
                len(
                    frappe.get_list(
                        "WooCommerce Server",
                        filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1},
                    )
                )
                > 0
            ):
                if doc.doctype == "Sales Invoice":
                    if doc.update_stock == 0:
                        return
                item_codes = [row.item_code for row in doc.items]
                for item_code in item_codes:
                    frappe.enqueue(
                        "woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
                        enqueue_after_commit=True,
                        item_code=item_code,
                    )


def update_stock_levels_for_all_enabled_items_in_background():
    """
    Get all enabled ERPNext Items and post stock updates to WooCommerce
    """
    erpnext_items = []
    current_page_length = 500
    start = 0

    # Get all items, 500 records at a time
    while current_page_length == 500:
        items = frappe.db.get_all(
            doctype="Item",
            filters={"disabled": 0},
            fields=["name"],
            start=start,
            page_length=500,
        )
        erpnext_items.extend(items)
        current_page_length = len(items)
        start += current_page_length

    for item in erpnext_items:
        frappe.enqueue(
            "woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
            item_code=item.name,
        )


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code):
    """
    Updates stock levels of an item on all its associated WooCommerce sites.

    This function fetches the item from the database, then for each associated
    WooCommerce site, it retrieves the current inventory, calculates the new stock quantity,
    and posts the updated stock levels back to the WooCommerce site.

    Behavior:
    - Parent items (has_variants=1): Sets manage_stock=False on WooCommerce (no stock quantity sync)
      because variable products should manage stock at the variation level, not parent level.
    - Variants and simple products: Sets manage_stock=True and syncs stock_quantity.
    """
    item = frappe.get_doc("Item", item_code)

    # Skip if:
    # - No WooCommerce servers linked
    # - Item is disabled
    if len(item.woocommerce_servers) == 0 or item.disabled:
        return False

    # Get bins for stock calculation (only needed for non-template items)
    bins = []
    if not item.has_variants and item.is_stock_item:
        bins = frappe.get_list(
            "Bin", {"item_code": item_code}, ["name", "warehouse", "reserved_qty", "actual_qty"]
        )

    for wc_site in item.woocommerce_servers:
        if wc_site.woocommerce_id:
            woocommerce_id = wc_site.woocommerce_id
            woocommerce_server = wc_site.woocommerce_server
            wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)

            if (
                not wc_server
                or not wc_server.enable_sync
                or not wc_site.enabled
                or not wc_server.enable_stock_level_synchronisation
            ):
                continue

            wc_api = APIWithRequestLogging(
                url=wc_server.woocommerce_server_url,
                consumer_key=wc_server.api_consumer_key,
                consumer_secret=wc_server.api_consumer_secret,
                version="wc/v3",
                timeout=40,
                verify_ssl=verify_ssl,
            )

            # Handle parent items (templates with variants) vs regular items differently
            if item.has_variants:
                # Parent items should NOT manage stock at the parent level
                # Stock is managed at the variation level in WooCommerce
                data_to_post = {
                    "manage_stock": False,
                }
            else:
                # For variants and simple products, sync stock levels normally
                # Sum all quantities from select warehouses and round the total down (WooCommerce API doesn't accept float values)
                stock_quantity = math.floor(
                    sum(
                        (
                            bin.actual_qty
                            if not wc_server.subtract_reserved_stock
                            else bin.actual_qty - bin.reserved_qty
                        )
                        for bin in bins
                        if bin.warehouse in [row.warehouse for row in wc_server.warehouses]
                    )
                )

                # Determine backorders setting based on EOL status
                # If item is not EOL (end_of_life is None or in the future), allow backorders
                is_eol = False
                if item.end_of_life:
                    from frappe.utils import getdate, nowdate

                    is_eol = getdate(item.end_of_life) <= getdate(nowdate())

                data_to_post = {
                    "stock_quantity": stock_quantity,
                    "manage_stock": True,
                    "backorders": "no" if is_eol else "yes",
                }

            try:
                parent_item_id = item.variant_of
                parent_woocommerce_id = None
                if parent_item_id:
                    parent_item = frappe.get_doc("Item", parent_item_id)
                    # Get the parent item's woocommerce_id
                    for parent_wc_site in parent_item.woocommerce_servers:
                        if parent_wc_site.woocommerce_server == woocommerce_server:
                            parent_woocommerce_id = parent_wc_site.woocommerce_id
                            break
                    if not parent_woocommerce_id:
                        continue
                    endpoint = f"products/{parent_woocommerce_id}/variations/{woocommerce_id}"
                else:
                    endpoint = f"products/{woocommerce_id}"
                response = wc_api.put(endpoint=endpoint, data=data_to_post)
            except Exception as err:
                error_message = (
                    f"{frappe.get_traceback()}\n\nData in PUT request: \n{str(data_to_post)}"
                )
                frappe.log_error("WooCommerce Error", error_message)
                raise err
            if response.status_code != 200:
                # Check for invalid variation ID error (404 with specific error code)
                if response.status_code == 404 and parent_item_id:
                    try:
                        response_data = response.json()
                        error_code = response_data.get("code", "")
                        if error_code == "woocommerce_rest_product_variation_invalid_id":
                            # The variation ID stored in ERPNext doesn't exist in WooCommerce
                            # or doesn't belong to the specified parent product
                            error_message = (
                                f"Invalid WooCommerce variation ID for item '{item_code}'.\n\n"
                                f"Details:\n"
                                f"- ERPNext Item: {item_code}\n"
                                f"- Parent ERPNext Item: {parent_item_id}\n"
                                f"- Parent WooCommerce Product ID: {parent_woocommerce_id}\n"
                                f"- Variation WooCommerce ID (stored in ERPNext): {woocommerce_id}\n"
                                f"- WooCommerce Server: {woocommerce_server}\n\n"
                                f"This error occurs when:\n"
                                f"1. The variation was deleted from WooCommerce but ERPNext still has the old ID\n"
                                f"2. The variation ID belongs to a different parent product in WooCommerce\n"
                                f"3. The parent-child relationship changed in WooCommerce\n\n"
                                f"To fix this, re-sync the item from WooCommerce or manually update the "
                                f"WooCommerce ID in the Item's 'WooCommerce Servers' table."
                            )
                            frappe.log_error("WooCommerce Invalid Variation ID", error_message)
                            # Don't raise an exception - just log and continue to next server
                            # This allows stock updates to proceed for other valid items
                            continue
                    except (ValueError, KeyError):
                        # If we can't parse the JSON response, fall through to generic error handling
                        pass

                error_message = (
                    f"Status Code not 200\n\nData in PUT request: \n{str(data_to_post)}"
                )
                error_message += (
                    f"\n\nResponse: \n{response.status_code}\nResponse Text: {response.text}\nRequest URL: {response.request.url}\nRequest Body: {response.request.body}"
                    if response is not None
                    else ""
                )
                frappe.log_error("WooCommerce Error", error_message)
                raise ValueError(error_message)

    return True
