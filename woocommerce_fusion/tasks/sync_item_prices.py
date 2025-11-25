from time import sleep, time
from typing import List, Optional

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
    WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
    generate_woocommerce_record_name_from_domain_and_id,
)


def update_item_price_for_woocommerce_item_from_hook(doc, method):
    if not frappe.flags.in_test:
        if doc.doctype == "Item Price":
            frappe.enqueue(
                "woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
                enqueue_after_commit=True,
                item_code=doc.item_code,
                item_price_doc=doc,
            )


@frappe.whitelist()
def run_item_price_sync_in_background():
    frappe.enqueue(run_item_price_sync, queue="long", timeout=3600)


@frappe.whitelist()
def run_item_price_sync(
    item_code: Optional[str] = None, item_price_doc: Optional[ItemPrice] = None
):
    sync = SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc)
    sync.run()
    return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
    """
    Class for managing synchronisation of ERPNext Items with WooCommerce Products
    """

    item_code: Optional[str]
    item_price_list: List

    def __init__(
        self,
        servers: List[WooCommerceServer | frappe._dict] = None,
        item_code: Optional[str] = None,
        item_price_doc: Optional[ItemPrice] = None,
    ) -> None:
        super().__init__(servers)
        self.item_code = item_code
        self.item_price_doc = item_price_doc
        self.wc_server = None
        self.item_price_list = []

    def run(self) -> None:
        """
        Run synchornisation
        """
        sync_start_time = time()
        frappe.logger().info(f"Starting item price sync for {len(self.servers)} server(s)")

        for server in self.servers:
            self.wc_server = server
            server_start_time = time()
            frappe.logger().info(
                f"Starting sync for server: {server.name} ({server.woocommerce_server_url})"
            )

            self.get_erpnext_item_prices()
            frappe.logger().info(
                f"Found {len(self.item_price_list)} items to sync for server {server.name}"
            )

            self.sync_items_with_woocommerce_products()

            server_elapsed = time() - server_start_time
            frappe.logger().info(
                f"Completed sync for server {server.name} in {server_elapsed:.2f} seconds"
            )

        total_elapsed = time() - sync_start_time
        frappe.logger().info(
            f"Item price sync completed in {total_elapsed:.2f} seconds ({total_elapsed/60:.2f} minutes)"
        )

    def get_erpnext_item_prices(self) -> None:
        """
        Get list of ERPNext Item Prices to synchronise,
        """
        self.item_price_list = []
        if (
            self.wc_server.enable_sync
            and self.wc_server.enable_price_list_sync
            and self.wc_server.price_list
        ):
            ip = qb.DocType("Item Price")
            iwc = qb.DocType("Item WooCommerce Server")
            item = qb.DocType("Item")
            and_conditions = []
            and_conditions.append(ip.price_list == self.wc_server.price_list)
            and_conditions.append(iwc.woocommerce_server == self.wc_server.name)
            and_conditions.append(item.disabled == 0)
            and_conditions.append(iwc.woocommerce_id.isnotnull())
            and_conditions.append(iwc.enabled == 1)
            if self.item_code:
                and_conditions.append(ip.item_code == self.item_code)

            self.item_price_list = (
                qb.from_(ip)
                .inner_join(iwc)
                .on(iwc.parent == ip.item_code)
                .inner_join(item)
                .on(item.name == ip.item_code)
                .select(
                    ip.name,
                    ip.item_code,
                    ip.price_list_rate,
                    iwc.woocommerce_server,
                    iwc.woocommerce_id,
                )
                .where(Criterion.all(and_conditions))
                .run(as_dict=True)
            )

    def sync_items_with_woocommerce_products(self) -> None:
        """
        Synchronise Item Prices with WooCommerce Products
        """
        total_items = len(self.item_price_list)
        processed_count = 0
        updated_count = 0
        error_count = 0
        sync_start_time = time()

        # Log progress every N items
        log_interval = max(1, total_items // 20)  # Log ~20 times throughout the process

        for idx, item_price in enumerate(self.item_price_list, start=1):
            item_start_time = time()
            # Get the WooCommerce Product doc
            wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
                domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
            )
            wc_product = frappe.get_doc(
                {"doctype": "WooCommerce Product", "name": wc_product_name}
            )

            try:
                load_start = time()
                wc_product.load_from_db()
                load_time = time() - load_start

                # If self.item_price_doc is set, set the price_list_rate accordingly, else use the price_list_rate from the price list
                price_list_rate = (
                    self.item_price_doc.price_list_rate
                    if self.item_price_doc
                    and self.item_price_doc.price_list == self.wc_server.price_list
                    else item_price.price_list_rate
                )
                # Handle blank string for regular_price
                if not wc_product.regular_price:
                    wc_product.regular_price = 0
                # When the price is set, the WooCommerce API returns a string value, when the price is not set, it returns a float value of 0.0
                wc_product_regular_price = (
                    float(wc_product.regular_price)
                    if isinstance(wc_product.regular_price, str)
                    else wc_product.regular_price
                )
                if wc_product_regular_price != price_list_rate:
                    save_start = time()
                    wc_product.regular_price = price_list_rate
                    wc_product.save()
                    save_time = time() - save_start
                    updated_count += 1

                    if load_time > 5 or save_time > 5:
                        frappe.logger().warning(
                            f"Slow operation for item {item_price.item_code} (WC ID: {item_price.woocommerce_id}): "
                            f"load={load_time:.2f}s, save={save_time:.2f}s"
                        )

                processed_count += 1

            except Exception:
                error_count += 1
                error_message = (
                    f"Item: {item_price.item_code} (WC ID: {item_price.woocommerce_id})\n"
                    f"Progress: {idx}/{total_items}\n"
                    f"{frappe.get_traceback()}\n\n Product Data: \n{str(wc_product.as_dict())}"
                )
                frappe.log_error("WooCommerce Error: Price List Sync", error_message)

            # Progress logging
            if idx % log_interval == 0 or idx == total_items:
                elapsed = time() - sync_start_time
                items_per_second = idx / elapsed if elapsed > 0 else 0
                remaining_items = total_items - idx
                estimated_remaining = remaining_items / items_per_second if items_per_second > 0 else 0

                frappe.logger().info(
                    f"Progress: {idx}/{total_items} items ({(idx/total_items*100):.1f}%) | "
                    f"Updated: {updated_count} | Errors: {error_count} | "
                    f"Speed: {items_per_second:.2f} items/sec | "
                    f"Elapsed: {elapsed:.1f}s | ETA: {estimated_remaining:.1f}s"
                )

                # Warning if approaching timeout (3600 seconds)
                if elapsed > 3000 and remaining_items > 0:
                    frappe.logger().warning(
                        f"Sync has been running for {elapsed/60:.1f} minutes. "
                        f"Approaching job timeout (60 minutes). "
                        f"Consider reducing item count or increasing timeout."
                    )

            sleep(self.wc_server.price_list_delay_per_item)

        # Final summary
        total_time = time() - sync_start_time
        frappe.logger().info(
            f"Sync completed: {processed_count}/{total_items} processed, "
            f"{updated_count} updated, {error_count} errors in {total_time:.2f}s ({total_time/60:.2f} minutes)"
        )
