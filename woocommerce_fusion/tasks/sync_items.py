import json
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
    WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
    WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
    generate_woocommerce_record_name_from_domain_and_id,
)


def run_item_sync_from_hook(doc, method):
    """
    Intended to be triggered by a Document Controller hook from Item
    """
    if (
        doc.doctype == "Item"
        and not doc.flags.get("created_by_sync", None)
        and len(doc.woocommerce_servers) > 0
    ):
        frappe.msgprint(
            _("Background sync to WooCommerce triggered for {0} {1}").format(
                frappe.bold(doc.name), method
            ),
            indicator="blue",
            alert=True,
        )
        frappe.enqueue(clear_sync_hash_and_run_item_sync, item_code=doc.name)


@frappe.whitelist()
def run_item_sync(
    item_code: Optional[str] = None,
    item: Optional[Item] = None,
    woocommerce_product_name: Optional[str] = None,
    woocommerce_product: Optional[WooCommerceProduct] = None,
    enqueue=False,
    sync_variants: bool = True,
) -> Tuple[Item, WooCommerceProduct]:
    """
    Helper funtion that prepares arguments for item sync

    Args:
        sync_variants: If True, when syncing a variable product/template item,
                      also sync all its variants. Default is True.
    """
    # Validate inputs, at least one of the parameters should be provided
    if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
        raise ValueError(
            (
                "At least one of item_code, item, woocommerce_product_name, woocommerce_product parameters required"
            )
        )

    # Initialize sync to None - it may not be assigned if all servers have sync disabled
    sync = None

    # Get ERPNext Item and WooCommerce product if they exist
    if woocommerce_product or woocommerce_product_name:
        if not woocommerce_product:
            woocommerce_product = frappe.get_doc(
                {"doctype": "WooCommerce Product", "name": woocommerce_product_name}
            )
            woocommerce_product.load_from_db()

        # Check if sync is enabled on the server before triggering sync
        wc_server_doc = frappe.get_cached_doc(
            "WooCommerce Server", woocommerce_product.woocommerce_server
        )
        if not wc_server_doc.enable_sync:
            frappe.logger().info(
                f"Skipping sync for product {woocommerce_product.name} on disabled server {wc_server_doc.name}"
            )
            return (None, None)

        # Trigger sync
        sync = SynchroniseItem(woocommerce_product=woocommerce_product)
        if enqueue:
            frappe.enqueue(sync.run)
        else:
            sync.run()

        # If this is a variable product, sync all its variants
        if sync_variants and woocommerce_product.type == "variable":
            sync_all_variants_for_product(woocommerce_product, enqueue=enqueue)

    elif item or item_code:
        if not item:
            item = frappe.get_doc("Item", item_code)
        if not item.woocommerce_servers:
            frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(item_code))
        for wc_server in item.woocommerce_servers:
            # Check if sync is enabled on the server before triggering sync
            wc_server_doc = frappe.get_cached_doc(
                "WooCommerce Server", wc_server.woocommerce_server
            )
            if not wc_server_doc.enable_sync:
                frappe.logger().info(
                    f"Skipping sync for item {item.name} on disabled server {wc_server_doc.name}"
                )
                continue

            # Trigger sync for every linked server
            sync = SynchroniseItem(
                item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server.idx)
            )
            if enqueue:
                frappe.enqueue(sync.run)
            else:
                sync.run()

            # If this is a template item (has variants), sync all its variants
            if sync_variants and item.has_variants:
                sync_all_variants_for_item(item, wc_server, enqueue=enqueue)

    return (
        sync.item.item if sync and sync.item else None,
        sync.woocommerce_product if sync else None,
    )


def sync_all_variants_for_product(
    woocommerce_product: WooCommerceProduct, enqueue: bool = False
) -> None:
    """
    Sync all variants for a given WooCommerce variable product

    Args:
        woocommerce_product: The parent/variable WooCommerce product
        enqueue: Whether to enqueue the sync tasks
    """
    if woocommerce_product.type != "variable":
        return

    try:
        # Fetch all variants for this product using the same pattern as get_list_of_wc_products
        wc_product_doc = frappe.get_doc({"doctype": "WooCommerce Product"})
        variants = wc_product_doc.get_list(
            args={
                "endpoint": f"products/{woocommerce_product.woocommerce_id}/variations",
                "metadata": {"parent_woocommerce_name": woocommerce_product.woocommerce_name},
                "servers": [woocommerce_product.woocommerce_server],
                "as_doc": True,
            }
        )

        # Sync each variant
        for variant in variants:
            try:
                # Sync the variant (disable recursive variant syncing to avoid infinite loops)
                run_item_sync(woocommerce_product=variant, enqueue=enqueue, sync_variants=False)
            except Exception as e:
                frappe.log_error(
                    f"Error syncing variant {variant.woocommerce_id} for product {woocommerce_product.woocommerce_id}",
                    str(e),
                )
    except Exception as e:
        frappe.log_error(
            f"Error fetching variants for product {woocommerce_product.woocommerce_id}", str(e)
        )


def sync_all_variants_for_item(item: Item, wc_server_link, enqueue: bool = False) -> None:
    """
    Sync all variants for a given ERPNext template item

    Args:
        item: The parent/template Item
        wc_server_link: The WooCommerce server link from item.woocommerce_servers
        enqueue: Whether to enqueue the sync tasks
    """
    if not item.has_variants:
        return

    try:
        # Get all variant items for this template
        variant_items = frappe.get_all("Item", filters={"variant_of": item.item_code}, fields=["name"])

        # Sync each variant
        for variant_item_data in variant_items:
            try:
                variant_item = frappe.get_doc("Item", variant_item_data.name)
                # Check if this variant is linked to the same WooCommerce server
                variant_wc_server = next(
                    (
                        ws
                        for ws in variant_item.woocommerce_servers
                        if ws.woocommerce_server == wc_server_link.woocommerce_server
                    ),
                    None,
                )
                if variant_wc_server:
                    # Sync the variant (disable recursive variant syncing to avoid infinite loops)
                    run_item_sync(item=variant_item, enqueue=enqueue, sync_variants=False)
            except Exception as e:
                frappe.log_error(
                    f"Error syncing variant item {variant_item_data.name} for template {item.item_code}",
                    str(e),
                )
    except Exception as e:
        frappe.log_error(
            f"Error fetching variant items for template {item.item_code}",
            str(e),
        )


def sync_woocommerce_products_modified_since(date_time_from=None):
    """
    Get list of WooCommerce products modified since date_time_from
    """
    wc_settings = frappe.get_doc("WooCommerce Integration Settings")

    if not date_time_from:
        date_time_from = wc_settings.wc_last_sync_date_items

    # Validate
    if not date_time_from:
        error_text = _(
            "'Last Items Syncronisation Date' field on 'WooCommerce Integration Settings' is missing"
        )
        frappe.log_error(
            "WooCommerce Items Sync Task Error",
            error_text,
        )
        raise ValueError(error_text)

    wc_products = get_list_of_wc_products(date_time_from=date_time_from)
    for wc_product in wc_products:
        try:
            run_item_sync(woocommerce_product=wc_product, enqueue=True)
        # Skip items with errors, as these exceptions will be logged
        except Exception:
            pass

    frappe.db.set_single_value(
        "WooCommerce Integration Settings", "wc_last_sync_date_items", now()
    )


@dataclass
class ERPNextItemToSync:
    """Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

    item: Item
    item_woocommerce_server_idx: int

    @property
    def item_woocommerce_server(self):
        return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]


class SynchroniseItem(SynchroniseWooCommerce):
    """
    Class for managing synchronisation of WooCommerce Product with ERPNext Item
    """

    def __init__(
        self,
        servers: List[WooCommerceServer | _dict] = None,
        item: Optional[ERPNextItemToSync] = None,
        woocommerce_product: Optional[WooCommerceProduct] = None,
    ) -> None:
        super().__init__(servers)
        self.item = item
        self.woocommerce_product = woocommerce_product
        self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")

    def run(self):
        """
        Run synchronisation
        """
        try:
            self.get_corresponding_item_or_product()
            self.sync_wc_product_with_erpnext_item()
        except Exception as err:
            try:
                woocommerce_product_dict = (
                    self.woocommerce_product.as_dict()
                    if isinstance(self.woocommerce_product, WooCommerceProduct)
                    else self.woocommerce_product
                )
            except ValidationError as e:
                woocommerce_product_dict = self.woocommerce_product
            error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(self.item) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
            frappe.log_error("WooCommerce Error", error_message)
            raise err

    def get_corresponding_item_or_product(self):
        """
        If we have an ERPNext Item, get the corresponding WooCommerce Product
        If we have a WooCommerce Product, get the corresponding ERPNext Item
        """
        if (
            self.item
            and not self.woocommerce_product
            and self.item.item_woocommerce_server.woocommerce_id
        ):
            # Validate that this Item's WooCommerce Server has sync enabled
            wc_server = frappe.get_cached_doc(
                "WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
            )
            if not wc_server.enable_sync:
                raise SyncDisabledError(wc_server)

            wc_products = get_list_of_wc_products(item=self.item)
            if len(wc_products) == 0:
                raise ValueError(
                    f"No WooCommerce Product found with ID {self.item.item_woocommerce_server.woocommerce_id} on {self.item.item_woocommerce_server.woocommerce_server}"
                )
            self.woocommerce_product = wc_products[0]

        if self.woocommerce_product and not self.item:
            self.get_erpnext_item()

    def get_erpnext_item(self):
        """
        Get erpnext item for a WooCommerce Product
        """
        if not all(
            [self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]
        ):
            raise ValueError("Both woocommerce_server and woocommerce_id required")

        iws = frappe.qb.DocType("Item WooCommerce Server")
        itm = frappe.qb.DocType("Item")

        and_conditions = [
            iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
            iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
        ]

        item_codes = (
            frappe.qb.from_(iws)
            .join(itm)
            .on(iws.parent == itm.name)
            .where(Criterion.all(and_conditions))
            .select(iws.parent, iws.name)
            .limit(1)
        ).run(as_dict=True)

        found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None
        if found_item:
            self.item = ERPNextItemToSync(
                item=found_item,
                item_woocommerce_server_idx=next(
                    server.idx
                    for server in found_item.woocommerce_servers
                    if server.name == item_codes[0].name
                ),
            )

    def sync_wc_product_with_erpnext_item(self):
        """
        Syncronise Item between ERPNext and WooCommerce
        Respects sync direction setting
        """
        # Get sync direction from WooCommerce Server
        wc_server_name = (
            self.woocommerce_product.woocommerce_server
            if self.woocommerce_product
            else self.item.item_woocommerce_server.woocommerce_server
        )
        wc_server = frappe.get_cached_doc("WooCommerce Server", wc_server_name)
        sync_direction = getattr(wc_server, "sync_direction", "Bidirectional")

        if self.item and not self.woocommerce_product:
            # create missing product in WooCommerce
            if sync_direction in ["Bidirectional", "ERP to WooCommerce Only"]:
                self.create_woocommerce_product(self.item)
        elif self.woocommerce_product and not self.item:
            # create missing item in ERPNext
            if sync_direction in ["Bidirectional", "WooCommerce to ERP Only"]:
                self.create_item(self.woocommerce_product)
        elif self.item and self.woocommerce_product:
            # both exist, check sync hash
            if (
                self.woocommerce_product.woocommerce_date_modified
                != self.item.item_woocommerce_server.woocommerce_last_sync_hash
            ):
                if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(
                    self.item.item.modified
                ):
                    # WooCommerce changed more recently - update ERPNext
                    if sync_direction in ["Bidirectional", "WooCommerce to ERP Only"]:
                        self.update_item(self.woocommerce_product, self.item)
                if get_datetime(self.woocommerce_product.woocommerce_date_modified) < get_datetime(
                    self.item.item.modified
                ):
                    # ERPNext changed more recently - update WooCommerce
                    if sync_direction in ["Bidirectional", "ERP to WooCommerce Only"]:
                        self.update_woocommerce_product(self.woocommerce_product, self.item)

    def update_item(self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync):
        """
        Update the ERPNext Item with fields from it's corresponding WooCommerce Product
        """
        item_dirty = False
        # Don't sync variant names back to ERP as variant naming differs between WooCommerce and ERPNext
        # WooCommerce formats as "Parent - Attr1, Attr2" while ERPNext uses template-based naming
        if woocommerce_product.type != "variation":
            if item.item.item_name != woocommerce_product.woocommerce_name:
                item.item.item_name = woocommerce_product.woocommerce_name
                item_dirty = True

        fields_updated, item.item = self.set_item_fields(item=item.item)

        wc_server = frappe.get_cached_doc(
            "WooCommerce Server", woocommerce_product.woocommerce_server
        )
        if wc_server.enable_image_sync:
            wc_product_images = json.loads(woocommerce_product.images)
            if len(wc_product_images) > 0:
                if item.item.image != wc_product_images[0]["src"]:
                    item.item.image = wc_product_images[0]["src"]
                    item_dirty = True

        if item_dirty or fields_updated:
            item.item.flags.created_by_sync = True
            item.item.save()

        self.set_sync_hash()

    def update_woocommerce_product(
        self, wc_product: WooCommerceProduct, item: ERPNextItemToSync
    ) -> None:
        """
        Update the WooCommerce Product with fields from it's corresponding ERPNext Item
        """
        wc_product_dirty = False

        # Update properties
        if wc_product.woocommerce_name != item.item.item_name:
            wc_product.woocommerce_name = item.item.item_name
            wc_product_dirty = True

        desired_status = item.item_woocommerce_server.product_status or "publish"
        if wc_product.status != desired_status:
            wc_product.status = desired_status
            wc_product_dirty = True

        product_fields_changed, wc_product = self.set_product_fields(wc_product, item)
        if product_fields_changed:
            wc_product_dirty = True

        if wc_product_dirty:
            wc_product.save()

        self.woocommerce_product = wc_product
        self.set_sync_hash()

    def create_woocommerce_product(self, item: ERPNextItemToSync) -> None:
        """
        Create the WooCommerce Product with fields from it's corresponding ERPNext Item
        """
        if (
            item.item_woocommerce_server.woocommerce_server
            and item.item_woocommerce_server.enabled
            and not item.item_woocommerce_server.woocommerce_id
        ):
            # Create a new WooCommerce Product doc
            wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})

            wc_product.type = "simple"

            # Handle variants
            if item.item.has_variants:
                wc_product.type = "variable"
                wc_product_attributes = []

                # Handle attributes
                for row in item.item.attributes:
                    item_attribute = frappe.get_doc("Item Attribute", row.attribute)
                    wc_product_attributes.append(
                        {
                            "name": row.attribute,
                            "slug": row.attribute.lower().replace(" ", "_"),
                            "visible": True,
                            "variation": True,
                            "options": [
                                option.attribute_value
                                for option in item_attribute.item_attribute_values
                            ],
                        }
                    )

                wc_product.attributes = json.dumps(wc_product_attributes)

            if item.item.variant_of:
                # Validate parent exists and is a template item
                if not frappe.db.exists("Item", item.item.variant_of):
                    error_msg = f"Parent item {item.item.variant_of} does not exist for variant {item.item.item_code}"
                    frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                    raise ValueError(error_msg)

                parent_item = frappe.get_doc("Item", item.item.variant_of)

                # Validate parent is a template item
                if not parent_item.has_variants:
                    error_msg = f"Parent item {item.item.variant_of} is not a template item (has_variants=0) for variant {item.item.item_code}"
                    frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                    raise ValueError(error_msg)

                # Sync parent to get WooCommerce product, disable variant syncing to avoid recursion
                parent_item, parent_wc_product = run_item_sync(
                    item_code=parent_item.item_code, sync_variants=False
                )

                # Validate parent WooCommerce product exists and is variable type
                if not parent_wc_product:
                    error_msg = f"Parent WooCommerce product not found for parent item {item.item.variant_of}"
                    frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                    raise ValueError(error_msg)

                if parent_wc_product.type != "variable":
                    error_msg = f"Parent WooCommerce product {parent_wc_product.woocommerce_id} is not a variable product (type={parent_wc_product.type})"
                    frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                    raise ValueError(error_msg)

                wc_product.parent_id = parent_wc_product.woocommerce_id
                wc_product.type = "variation"

                # Handle attributes
                wc_product_attributes = [
                    {
                        "name": row.attribute,
                        "slug": row.attribute.lower().replace(" ", "_"),
                        "option": row.attribute_value,
                    }
                    for row in item.item.attributes
                ]

                wc_product.attributes = json.dumps(wc_product_attributes)

            # Set properties
            wc_product.woocommerce_server = item.item_woocommerce_server.woocommerce_server
            wc_product.woocommerce_name = item.item.item_name
            wc_product.regular_price = get_item_price_rate(item) or "0"
            wc_product.status = item.item_woocommerce_server.product_status or "publish"

            self.set_product_fields(wc_product, item)

            wc_product.insert()
            self.woocommerce_product = wc_product

            # Reload ERPNext Item
            item.item.reload()
            item.item_woocommerce_server.woocommerce_id = wc_product.woocommerce_id
            item.item.flags.created_by_sync = True
            item.item.save()

            self.set_sync_hash()

    def create_item(self, wc_product: WooCommerceProduct) -> None:
        """
        Create an ERPNext Item from the given WooCommerce Product
        """
        wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

        # Create Item
        item = frappe.new_doc("Item")

        # Handle variants' attributes
        wc_attributes = []
        if wc_product.type in ["variable", "variation"]:
            self.create_or_update_item_attributes(wc_product)
            if wc_product.attributes:
                wc_attributes = json.loads(wc_product.attributes)
            for wc_attribute in wc_attributes:
                row = item.append("attributes")
                row.attribute = wc_attribute["name"]
                if wc_product.type == "variation":
                    row.attribute_value = wc_attribute["option"]

        # Handle variants - only set has_variants if attributes exist
        # ERPNext requires attributes for template items
        if wc_product.type == "variable":
            if wc_attributes:
                item.has_variants = 1
            else:
                # Variable product without attributes - treat as simple product
                frappe.log_error(
                    title="WooCommerce Variable Product Without Attributes",
                    message=f"WooCommerce product {wc_product.woocommerce_id} ({wc_product.woocommerce_name}) "
                    f"is marked as 'variable' but has no attributes defined. "
                    f"Creating as a simple item without variants. "
                    f"Please add attributes in WooCommerce if variants are needed.",
                )

        if wc_product.type == "variation":
            # Validate that the variant has a parent_id
            if not wc_product.parent_id:
                error_msg = (
                    f"WooCommerce variation {wc_product.woocommerce_id} is missing parent_id"
                )
                frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                raise ValueError(error_msg)

            # Fetch or sync parent product
            woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
                wc_product.woocommerce_server, wc_product.parent_id
            )
            parent_item, parent_wc_product = run_item_sync(
                woocommerce_product_name=woocommerce_product_name,
                sync_variants=False,  # Disable variant syncing to avoid recursion
            )

            # Validate parent item was created/found
            if not parent_item:
                error_msg = f"Failed to sync parent WooCommerce product {wc_product.parent_id} for variation {wc_product.woocommerce_id}"
                frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                raise ValueError(error_msg)

            # Validate parent is a template item
            if not parent_item.has_variants:
                error_msg = f"Parent item {parent_item.item_code} is not a template item (has_variants=0) for WooCommerce variation {wc_product.woocommerce_id}"
                frappe.log_error("WooCommerce Variant Sync Error", error_msg)
                raise ValueError(error_msg)

            item.variant_of = parent_item.item_code

        # Determine base item code from SKU or WooCommerce ID
        base_item_code = (
            wc_product.sku
            if wc_server.name_by == "Product SKU" and wc_product.sku
            else str(wc_product.woocommerce_id)
        )

        # Add server abbreviation prefix if configured
        server_abbr = getattr(wc_server, "server_abbreviation", None)
        if server_abbr:
            item.item_code = f"{server_abbr}-{base_item_code}"
        else:
            item.item_code = base_item_code
        item.stock_uom = wc_server.uom or _("Nos")
        item.item_group = wc_server.item_group
        item.item_name = wc_product.woocommerce_name
        row = item.append("woocommerce_servers")
        row.woocommerce_id = wc_product.woocommerce_id
        row.woocommerce_server = wc_server.name
        item.flags.ignore_mandatory = True
        item.flags.created_by_sync = True

        if wc_server.enable_image_sync:
            wc_product_images = json.loads(wc_product.images)
            if len(wc_product_images) > 0:
                item.image = wc_product_images[0]["src"]

        modified, item = self.set_item_fields(item=item)
        item.flags.created_by_sync = True

        item.insert()

        self.item = ERPNextItemToSync(
            item=item,
            item_woocommerce_server_idx=next(
                iws.idx
                for iws in item.woocommerce_servers
                if iws.woocommerce_server == wc_product.woocommerce_server
            ),
        )

        self.set_sync_hash()

    def create_or_update_item_attributes(self, wc_product: WooCommerceProduct):
        """
        Create or update an Item Attribute.

        For variable products: Can add missing attribute values (they contain the complete list)
        For variations: Only ADD missing attribute values to avoid removing values used by other items

        Respects sync direction settings - only modifies ERPNext attributes when sync direction allows.

        Note: Attribute values (e.g., "White", "Black") are the same in both systems.
        The difference is in item naming - ERPNext uses "Parent Name - Attribute Value" format
        while WooCommerce variations just use the attribute value.
        """
        if not wc_product.attributes:
            return

        # Get sync direction from WooCommerce Server
        wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)
        sync_direction = getattr(wc_server, "sync_direction", "Bidirectional")

        # Check if we should update ERPNext based on sync direction
        # For "ERP to WooCommerce Only", we should not modify ERPNext attributes from WooCommerce data
        can_update_erp = sync_direction in ["Bidirectional", "WooCommerce to ERP Only"]

        wc_attributes = json.loads(wc_product.attributes)
        for wc_attribute in wc_attributes:
            attribute_exists = frappe.db.exists("Item Attribute", wc_attribute["name"])

            if attribute_exists:
                # Get existing Item Attribute
                item_attribute = frappe.get_doc("Item Attribute", wc_attribute["name"])
            else:
                # Create a new Item Attribute (always allowed - we need the attribute to exist)
                item_attribute = frappe.get_doc(
                    {"doctype": "Item Attribute", "attribute_name": wc_attribute["name"]}
                )

            # Get list of attribute options.
            # In variable WooCommerce Products, it's a list with key "options"
            # In a WooCommerce Product variant, it's a single value with key "option"
            options = (
                wc_attribute["options"]
                if wc_product.type == "variable"
                else [wc_attribute["option"]]
            )

            # Get existing attribute values
            existing_values = set(
                val.attribute_value for val in item_attribute.item_attribute_values
            )

            attribute_modified = False

            if not attribute_exists:
                # New attribute - add all options
                for option in options:
                    row = item_attribute.append("item_attribute_values")
                    row.attribute_value = option
                    row.abbr = option.replace(" ", "")
                attribute_modified = True
            elif can_update_erp:
                # Existing attribute - respect sync direction and product type
                # For both variable products and variations: Only ADD missing values
                # This prevents the InvalidItemAttributeValueError when other items use different values
                # We never remove existing values to avoid breaking items that depend on them
                for option in options:
                    if option not in existing_values:
                        row = item_attribute.append("item_attribute_values")
                        row.attribute_value = option
                        row.abbr = option.replace(" ", "")
                        attribute_modified = True

            item_attribute.flags.ignore_mandatory = True
            if not item_attribute.name:
                item_attribute.insert()
            elif attribute_modified:
                item_attribute.save()

    def set_item_fields(self, item: Item) -> Tuple[bool, Item]:
        """
        If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
        WooCommerce to ERPNext
        """
        item_dirty = False
        if item and self.woocommerce_product:
            wc_server = frappe.get_cached_doc(
                "WooCommerce Server", self.woocommerce_product.woocommerce_server
            )
            if wc_server.item_field_map:
                woocommerce_product_dict = (
                    self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
                        self.woocommerce_product.to_dict()
                    )
                )
                for map in wc_server.item_field_map:
                    # Skip description and short_description syncing for variants
                    # Variants should not have their own description - only the parent product should
                    if self.woocommerce_product.type == "variation":
                        wc_field = map.woocommerce_field_name.lower()
                        if "description" in wc_field or "short_description" in wc_field:
                            frappe.log_error(
                                "WooCommerce Variant Field Sync Skipped",
                                f"Skipping {map.woocommerce_field_name} sync for variant {self.woocommerce_product.name}. "
                                "Descriptions should only be set on the parent product, not variants.",
                            )
                            continue

                    erpnext_item_field_name = map.erpnext_field_name.split(" | ")

                    # We expect woocommerce_field_name to be valid JSONPath
                    jsonpath_expr = parse(map.woocommerce_field_name)
                    woocommerce_product_field_matches = jsonpath_expr.find(
                        woocommerce_product_dict
                    )

                    setattr(
                        item,
                        erpnext_item_field_name[0],
                        woocommerce_product_field_matches[0].value,
                    )
                    item_dirty = True
        return item_dirty, item

    def set_product_fields(
        self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync
    ) -> Tuple[bool, WooCommerceProduct]:
        """
        If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
        ERPNext to WooCommerce

        Returns true if woocommerce_product was changed
        """
        wc_product_dirty = False
        if item and woocommerce_product:
            wc_server = frappe.get_cached_doc(
                "WooCommerce Server", woocommerce_product.woocommerce_server
            )
            if wc_server.item_field_map:

                # Deserialize the WooCommerce Product's list and dict fields because we want to potentially perform
                # in-place updates on the whole dict using jsonpath-ng. Use the existing class method for this.
                wc_product_with_deserialised_fields = (
                    woocommerce_product.deserialize_attributes_of_type_dict_or_list(
                        woocommerce_product
                    )
                )

                for map in wc_server.item_field_map:
                    # Skip description and short_description syncing for variants
                    # Variants should not have their own description - only the parent product should
                    if woocommerce_product.type == "variation":
                        wc_field = map.woocommerce_field_name.lower()
                        if "description" in wc_field or "short_description" in wc_field:
                            frappe.log_error(
                                "WooCommerce Variant Field Sync Skipped",
                                f"Skipping {map.woocommerce_field_name} sync for variant {woocommerce_product.name}. "
                                "Descriptions should only be set on the parent product, not variants.",
                            )
                            continue

                    erpnext_item_field_name = map.erpnext_field_name.split(" | ")
                    erpnext_item_field_value = getattr(item.item, erpnext_item_field_name[0])

                    # We expect woocommerce_field_name to be valid JSONPath
                    jsonpath_expr = parse(map.woocommerce_field_name)
                    woocommerce_product_field_matches = jsonpath_expr.find(
                        wc_product_with_deserialised_fields
                    )

                    if len(woocommerce_product_field_matches) == 0:
                        if woocommerce_product.name:
                            # We're strict about existing WooCommerce Products, the field should exist
                            raise ValueError(
                                _(
                                    "Field <code>{0}</code> not found in WooCommerce Product {1}"
                                ).format(map.woocommerce_field_name, woocommerce_product.name)
                            )
                        else:
                            # For new WooCommerce Products, the nested field may not exist yet, so don't stop the sync
                            continue

                    # JSONPath parsing typically returns a list, we'll only take the first value
                    woocommerce_product_field_value = woocommerce_product_field_matches[0].value

                    if erpnext_item_field_value != woocommerce_product_field_value:
                        jsonpath_expr.update(
                            wc_product_with_deserialised_fields, erpnext_item_field_value
                        )
                        wc_product_dirty = True

                if wc_product_dirty:
                    # Re-serialize the WooCommerce Product's list and dict fields, because we deserialized earlier
                    woocommerce_product = (
                        woocommerce_product.serialize_attributes_of_type_dict_or_list(
                            wc_product_with_deserialised_fields
                        )
                    )

        return wc_product_dirty, woocommerce_product

    def set_sync_hash(self):
        """
        Set the last sync hash value using db.set_value, as it does not call the ORM triggers
        and it does not update the modified timestamp (by using the update_modified parameter)
        """
        frappe.db.set_value(
            "Item WooCommerce Server",
            self.item.item_woocommerce_server.name,
            "woocommerce_last_sync_hash",
            self.woocommerce_product.woocommerce_date_modified,
            update_modified=False,
        )

        # If item was synchronised but the item is set not to sync, turn on the enabled flag
        # Items that are disabled for sync will still be synced if it is ordered on WooCommerce
        frappe.db.set_value(
            "Item WooCommerce Server",
            self.item.item_woocommerce_server.name,
            "enabled",
            1,
            update_modified=False,
        )


def get_list_of_wc_products(
    item: Optional[ERPNextItemToSync] = None, date_time_from: Optional[datetime] = None
) -> List[WooCommerceProduct]:
    """
    Fetches a list of WooCommerce Products within a specified date range or linked with an Item, using pagination.

    At least one of date_time_from, item parameters are required.

    For variant items (those with variant_of set), this function uses the WooCommerce variations endpoint
    /products/{parent_id}/variations to properly fetch the variation data.
    """
    if not any([date_time_from, item]):
        raise ValueError("At least one of date_time_from or item parameters are required")

    wc_records_per_page_limit = 100
    page_length = wc_records_per_page_limit
    new_results = True
    start = 0
    filters = []
    wc_products = []
    servers = None
    endpoint = None  # Custom endpoint for variations
    metadata = None  # Metadata for variation naming

    # Build filters
    if date_time_from:
        filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])
    if item:
        filters.append(
            ["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id]
        )
        servers = [item.item_woocommerce_server.woocommerce_server]

        # Check if this is a variant item - if so, we need to use the variations endpoint
        if item.item.variant_of:
            # Get the parent item to find its WooCommerce ID
            parent_item = frappe.get_doc("Item", item.item.variant_of)

            # Find the parent's WooCommerce server link for the same server
            parent_wc_server = next(
                (
                    ws
                    for ws in parent_item.woocommerce_servers
                    if ws.woocommerce_server == item.item_woocommerce_server.woocommerce_server
                ),
                None,
            )

            if parent_wc_server and parent_wc_server.woocommerce_id:
                # Use the variations endpoint with the parent's WooCommerce ID
                endpoint = f"products/{parent_wc_server.woocommerce_id}/variations"
                metadata = {"parent_woocommerce_name": parent_item.item_name}
            else:
                # Parent doesn't have a WooCommerce ID for this server - this is an error state
                frappe.log_error(
                    "WooCommerce Variant Sync Error",
                    f"Cannot sync variant {item.item.item_code} - parent item {item.item.variant_of} "
                    f"does not have a WooCommerce ID for server {item.item_woocommerce_server.woocommerce_server}",
                )
                return []

    while new_results:
        woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
        args = {
            "filters": filters,
            "page_length": page_length,
            "start": start,
            "servers": servers,
            "as_doc": True,
        }
        # Add custom endpoint for variations
        if endpoint:
            args["endpoint"] = endpoint
        if metadata:
            args["metadata"] = metadata

        new_results = woocommerce_product.get_list(args=args)
        for wc_product in new_results:
            wc_products.append(wc_product)
        start += page_length
        if len(new_results) < page_length:
            new_results = []

    return wc_products


def get_item_price_rate(item: ERPNextItemToSync):
    """
    Get the Item Price if Item Price sync is enabled
    """
    # Check if the Item Price sync is enabled
    wc_server = frappe.get_cached_doc(
        "WooCommerce Server", item.item_woocommerce_server.woocommerce_server
    )
    if wc_server.enable_price_list_sync:
        item_prices = frappe.get_all(
            "Item Price",
            filters={"item_code": item.item.item_code, "price_list": wc_server.price_list},
            fields=["price_list_rate", "valid_upto"],
        )
        return next(
            (
                price.price_list_rate
                for price in item_prices
                if not price.valid_upto or price.valid_upto > now()
            ),
            None,
        )


def clear_sync_hash_and_run_item_sync(item_code: str):
    """
    Clear the last sync hash value using db.set_value, as it does not call the ORM triggers
    and it does not update the modified timestamp (by using the update_modified parameter)
    """

    iws = frappe.qb.DocType("Item WooCommerce Server")

    iwss = (
        frappe.qb.from_(iws)
        .where(iws.enabled == 1)
        .where(iws.parent == item_code)
        .select(iws.name, iws.woocommerce_server)
    ).run(as_dict=True)

    # Filter out servers with sync disabled
    iwss_to_sync = []
    for iws_row in iwss:
        wc_server = frappe.get_cached_doc("WooCommerce Server", iws_row.woocommerce_server)
        if wc_server.enable_sync:
            iwss_to_sync.append(iws_row)
        else:
            frappe.logger().info(
                f"Skipping sync for item {item_code} on disabled server {wc_server.name}"
            )

    for iws in iwss_to_sync:
        frappe.db.set_value(
            "Item WooCommerce Server",
            iws.name,
            "woocommerce_last_sync_hash",
            None,
            update_modified=False,
        )

    if len(iwss_to_sync) > 0:
        run_item_sync(item_code=item_code, enqueue=True)
