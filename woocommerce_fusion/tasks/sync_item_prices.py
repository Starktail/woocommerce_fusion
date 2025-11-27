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

# Default batch size for item price sync to avoid job timeouts
# With ~2 seconds per item, 500 items takes ~17 minutes, well under the 60 min timeout
DEFAULT_BATCH_SIZE = 500


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
    """Start item price sync as a background job with batch processing."""
    frappe.enqueue(
        run_item_price_sync,
        queue="long",
        timeout=3600,
        offset=0,
        batch_size=DEFAULT_BATCH_SIZE,
    )


@frappe.whitelist()
def run_item_price_sync(
    item_code: Optional[str] = None,
    item_price_doc: Optional[ItemPrice] = None,
    offset: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
):
    """
    Run item price sync with batch processing support.

    Args:
        item_code: Optional specific item code to sync
        item_price_doc: Optional specific item price document
        offset: Starting offset for batch processing (0-indexed)
        batch_size: Number of items to process per batch
    """
    sync = SynchroniseItemPrice(
        item_code=item_code,
        item_price_doc=item_price_doc,
        offset=offset,
        batch_size=batch_size,
    )
    sync.run()
    return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
    """
    Class for managing synchronisation of ERPNext Items with WooCommerce Products.

    Supports batch processing to handle large datasets without job timeouts.
    """

    item_code: Optional[str]
    item_price_list: List

    def __init__(
        self,
        servers: List[WooCommerceServer | frappe._dict] = None,
        item_code: Optional[str] = None,
        item_price_doc: Optional[ItemPrice] = None,
        offset: int = 0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        super().__init__(servers)
        self.item_code = item_code
        self.item_price_doc = item_price_doc
        self.wc_server = None
        self.item_price_list = []
        self.offset = offset
        self.batch_size = batch_size
        self.total_items_count = 0  # Total items across all servers (for logging)

    def run(self) -> None:
        """
        Run synchronisation with batch processing support.

        Processes items in batches to avoid job timeouts. If there are more items
        to process after the current batch, enqueues the next batch automatically.
        """
        sync_start_time = time()

        # Log batch info
        if self.offset == 0:
            frappe.logger().info(
                f"Starting item price sync for {len(self.servers)} server(s) "
                f"(batch_size={self.batch_size})"
            )
        else:
            frappe.logger().info(
                f"Continuing item price sync from offset {self.offset} "
                f"(batch_size={self.batch_size})"
            )

        total_processed_this_batch = 0

        for server in self.servers:
            self.wc_server = server
            server_start_time = time()

            # Get total count first (for logging purposes)
            self.get_erpnext_item_prices_count()

            frappe.logger().info(
                f"Server {server.name}: {self.total_items_count} total items, "
                f"processing from offset {self.offset} (batch_size={self.batch_size})"
            )

            # Get the batch of items to process
            self.get_erpnext_item_prices()

            if not self.item_price_list:
                frappe.logger().info(
                    f"No items to process for server {server.name} at offset {self.offset}"
                )
                continue

            frappe.logger().info(
                f"Processing {len(self.item_price_list)} items for server {server.name} "
                f"(items {self.offset + 1} to {self.offset + len(self.item_price_list)} "
                f"of {self.total_items_count})"
            )

            self.sync_items_with_woocommerce_products()
            total_processed_this_batch += len(self.item_price_list)

            server_elapsed = time() - server_start_time
            frappe.logger().info(
                f"Completed batch for server {server.name} in {server_elapsed:.2f} seconds"
            )

            # Check if there are more items to process for this server
            next_offset = self.offset + len(self.item_price_list)
            if next_offset < self.total_items_count:
                frappe.logger().info(
                    f"Enqueuing next batch: offset={next_offset}, "
                    f"remaining={self.total_items_count - next_offset} items"
                )
                frappe.enqueue(
                    "woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
                    queue="long",
                    timeout=3600,
                    offset=next_offset,
                    batch_size=self.batch_size,
                )

        total_elapsed = time() - sync_start_time
        frappe.logger().info(
            f"Batch completed: processed {total_processed_this_batch} items "
            f"in {total_elapsed:.2f} seconds ({total_elapsed/60:.2f} minutes)"
        )

    def _build_item_price_query_conditions(self):
        """
        Build the common query conditions for item price queries.

        Returns:
            Tuple of (ip, iwc, item, and_conditions) for use in queries.
        """
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
        return ip, iwc, item, and_conditions

    def get_erpnext_item_prices_count(self) -> None:
        """
        Get total count of ERPNext Item Prices to synchronise (for progress logging).
        """
        self.total_items_count = 0
        if (
            self.wc_server.enable_sync
            and self.wc_server.enable_price_list_sync
            and self.wc_server.price_list
        ):
            ip, iwc, item, and_conditions = self._build_item_price_query_conditions()

            from pypika import functions as fn

            result = (
                qb.from_(ip)
                .inner_join(iwc)
                .on(iwc.parent == ip.item_code)
                .inner_join(item)
                .on(item.name == ip.item_code)
                .select(fn.Count(ip.name).as_("count"))
                .where(Criterion.all(and_conditions))
                .run(as_dict=True)
            )
            self.total_items_count = result[0].count if result else 0

    def get_erpnext_item_prices(self) -> None:
        """
        Get list of ERPNext Item Prices to synchronise with LIMIT and OFFSET for batch processing.
        """
        self.item_price_list = []
        if (
            self.wc_server.enable_sync
            and self.wc_server.enable_price_list_sync
            and self.wc_server.price_list
        ):
            ip, iwc, item, and_conditions = self._build_item_price_query_conditions()

            query = (
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
                .orderby(ip.name)  # Ensure consistent ordering for pagination
            )

            # Apply LIMIT and OFFSET for batch processing
            if self.batch_size > 0:
                query = query.limit(self.batch_size).offset(self.offset)

            self.item_price_list = query.run(as_dict=True)

    def sync_items_with_woocommerce_products(self) -> None:
        """
        Synchronise Item Prices with WooCommerce Products
        """
        batch_items = len(self.item_price_list)
        processed_count = 0
        updated_count = 0
        error_count = 0
        sync_start_time = time()

        # Log progress every N items
        log_interval = max(1, batch_items // 20)  # Log ~20 times throughout the process

        for idx, item_price in enumerate(self.item_price_list, start=1):
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
                # Calculate overall position for error message
                overall_position = self.offset + idx
                error_message = (
                    f"Item: {item_price.item_code} (WC ID: {item_price.woocommerce_id})\n"
                    f"Progress: {overall_position}/{self.total_items_count} (batch item {idx}/{batch_items})\n"
                    f"{frappe.get_traceback()}\n\n Product Data: \n{str(wc_product.as_dict())}"
                )
                frappe.log_error("WooCommerce Error: Price List Sync", error_message)

            # Progress logging
            if idx % log_interval == 0 or idx == batch_items:
                elapsed = time() - sync_start_time
                items_per_second = idx / elapsed if elapsed > 0 else 0
                remaining_in_batch = batch_items - idx
                estimated_remaining = (
                    remaining_in_batch / items_per_second if items_per_second > 0 else 0
                )

                # Calculate overall progress
                overall_position = self.offset + idx
                overall_percent = (
                    (overall_position / self.total_items_count * 100)
                    if self.total_items_count > 0
                    else 0
                )

                frappe.logger().info(
                    f"Batch progress: {idx}/{batch_items} | "
                    f"Overall: {overall_position}/{self.total_items_count} ({overall_percent:.1f}%) | "
                    f"Updated: {updated_count} | Errors: {error_count} | "
                    f"Speed: {items_per_second:.2f} items/sec | "
                    f"Batch ETA: {estimated_remaining:.1f}s"
                )

            sleep(self.wc_server.price_list_delay_per_item)

        # Final summary for this batch
        total_time = time() - sync_start_time
        frappe.logger().info(
            f"Batch completed: {processed_count}/{batch_items} processed, "
            f"{updated_count} updated, {error_count} errors in {total_time:.2f}s ({total_time/60:.2f} minutes)"
        )
