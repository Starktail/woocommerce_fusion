# Copyright (c) 2023, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from typing import List
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
        self.update_unsupported_statuses_html()

    def validate_so_status_map(self):
        """
        Validate Sales Order Status Map to have unique mappings
        """
        erpnext_so_statuses = [
            map.erpnext_sales_order_status for map in self.sales_order_status_map
        ]
        if len(erpnext_so_statuses) != len(set(erpnext_so_statuses)):
            frappe.throw(
                _("Duplicate ERPNext Sales Order Statuses found in Sales Order Status Map")
            )
        wc_so_statuses = [
            map.woocommerce_sales_order_status for map in self.sales_order_status_map
        ]
        if len(wc_so_statuses) != len(set(wc_so_statuses)):
            frappe.throw(
                _("Duplicate WooCommerce Sales Order Statuses found in Sales Order Status Map")
            )

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
                        _(
                            "Invalid JSONPath syntax in Item Field Map Row {0}:<br><br><pre>{1}</pre>"
                        ).format(map.idx, e)
                    )

                for field in disallowed_fields:
                    if field in jsonpath_expr:
                        frappe.throw(
                            _("Field '{0}' is not allowed in JSONPath expression").format(field)
                        )

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

    def update_unsupported_statuses_html(self):
        """
        Update the HTML field to show unsupported order statuses
        """
        unsupported_statuses = json.loads(self.unsupported_order_statuses or "{}")

        if unsupported_statuses:
            status_list = ", ".join(
                [f"<strong>{status}</strong>" for status in unsupported_statuses.keys()]
            )
            self.unsupported_statuses_html = (
                f'<div class="alert alert-warning">'
                f"<strong>Warning:</strong> This WooCommerce server does not support the following order statuses: {status_list}. "
                f"These statuses will be automatically skipped during synchronization. "
                f"To use these statuses, please register them as custom order statuses in your WooCommerce site."
                f"</div>"
            )
        else:
            self.unsupported_statuses_html = ""

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
            timeout=(10, 40),  # (connect_timeout, read_timeout) - prevents SSL handshake hangs
            verify_ssl=verify_ssl,
        )
        all_providers = wc_api.get("orders/1/shipment-trackings/providers").json()
        if all_providers:
            provider_names = [
                provider for country in all_providers for provider in all_providers[country]
            ]
            self.wc_ast_shipment_providers = "\n".join(provider_names)

    @frappe.whitelist()
    @redis_cache(ttl=600)
    def get_item_docfields(self, doctype: str) -> List[dict]:
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
    def get_woocommerce_order_status_list(self) -> List[str]:
        """
        Retrieve list of WooCommerce Order Statuses
        """
        return [key for key in WC_ORDER_STATUS_MAPPING.keys()]

    @frappe.whitelist()
    def test_connection(self):
        """
        Test connection to WooCommerce server
        """
        # Show initial toast notification
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("Testing connection to WooCommerce server..."),
                "indicator": "blue",
            },
            user=frappe.session.user,
        )

        try:
            wc_api = API(
                url=self.woocommerce_server_url,
                consumer_key=self.api_consumer_key,
                consumer_secret=self.api_consumer_secret,
                version="wc/v3",
                timeout=(10, 40),  # (connect_timeout, read_timeout) - prevents SSL handshake hangs
                verify_ssl=verify_ssl,
            )
            # Try to get system status
            response = wc_api.get("system_status")
            if response.status_code == 200:
                # Show success toast
                frappe.publish_realtime(
                    "show_alert",
                    {
                        "message": _("Connection successful! WooCommerce server is reachable."),
                        "indicator": "green",
                    },
                    user=frappe.session.user,
                )
                frappe.msgprint(
                    _(
                        "Connection successful! WooCommerce server is reachable and API credentials are valid."
                    ),
                    title=_("Success"),
                    indicator="green",
                )
            else:
                # Show error toast
                frappe.publish_realtime(
                    "show_alert",
                    {
                        "message": _("Connection failed. Status code: {0}").format(
                            response.status_code
                        ),
                        "indicator": "red",
                    },
                    user=frappe.session.user,
                )
                frappe.msgprint(
                    _(
                        "Connection failed. Status code: {0}<br><br>Please check your API credentials."
                    ).format(response.status_code),
                    title=_("Error"),
                    indicator="red",
                )
        except Exception as e:
            # Show error toast
            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("Connection failed. Please check your settings."),
                    "indicator": "red",
                },
                user=frappe.session.user,
            )
            frappe.msgprint(
                _(
                    "Connection failed: {0}<br><br>Please verify:<br>1. WooCommerce Server URL is correct<br>2. API credentials are valid<br>3. WooCommerce REST API is enabled"
                ).format(str(e)),
                title=_("Error"),
                indicator="red",
            )

    @frappe.whitelist()
    def sync_all_items_now(self):
        """
        Sync all items/products immediately respecting sync direction
        """
        from woocommerce_fusion.tasks.sync_items import run_item_sync

        sync_direction = getattr(self, "sync_direction", "Bidirectional")

        # Show initial toast notification
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("Starting item sync... This will run in the background."),
                "indicator": "blue",
            },
            user=frappe.session.user,
        )

        # Count items to sync
        items_synced = 0
        errors = []

        if sync_direction in ["Bidirectional", "ERP to WooCommerce Only"]:
            # Sync items from ERPNext to WooCommerce
            items = frappe.get_all(
                "Item",
                filters={"disabled": 0},
                fields=["name"],
            )

            for item in items:
                try:
                    frappe.enqueue(
                        run_item_sync,
                        queue="long",
                        item_code=item.name,
                        enqueue_after_commit=True,
                    )
                    items_synced += 1
                except Exception as e:
                    errors.append(f"Item {item.name}: {str(e)}")

        if sync_direction in ["Bidirectional", "WooCommerce to ERP Only"]:
            # Sync products from WooCommerce to ERPNext
            from woocommerce_fusion.tasks.sync_items import (
                sync_woocommerce_products_modified_since,
            )

            frappe.enqueue(
                sync_woocommerce_products_modified_since,
                queue="long",
                date_time_from=None,  # Sync all
            )

        # Show detailed results
        if errors:
            message = _(
                "<b>{0} items queued for sync</b> with {1} errors.<br><br>"
                "<b>Sync Direction:</b> {2}<br><br>"
                "<b>Check progress:</b><br>"
                "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                "• Monitor <b>Error Log</b> for any issues<br><br>"
                "Sync is running in the background and may take several minutes."
            ).format(items_synced, len(errors), sync_direction)
            frappe.msgprint(message, title=_("Sync Started"), indicator="orange")
        else:
            message = _(
                "<b>{0} items queued for sync!</b><br><br>"
                "<b>Sync Direction:</b> {2}<br><br>"
                "<b>Check progress:</b><br>"
                "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                "• Or check <b>RQ Console</b> for job status<br><br>"
                "Sync is running in the background and may take several minutes."
            ).format(items_synced, sync_direction, sync_direction)
            frappe.msgprint(message, title=_("Sync Started"), indicator="green")

        # Show final toast
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("{0} items queued. Check Background Jobs for progress.").format(
                    items_synced
                ),
                "indicator": "green",
            },
            user=frappe.session.user,
        )

    @frappe.whitelist()
    def clear_unsupported_statuses(self):
        """
        Clear the list of unsupported order statuses.
        Use this if you've added custom order status support to your WooCommerce site.
        """
        self.unsupported_order_statuses = "{}"
        self.save()

        frappe.msgprint(
            _(
                "Unsupported statuses list has been cleared. The system will attempt to sync all configured statuses on the next sync."
            ),
            title=_("Success"),
            indicator="green",
        )

    @frappe.whitelist()
    def push_all_erp_items_to_wc(self):
        """
        Push all ERPNext items to WooCommerce (creates/updates products)
        This will add the WooCommerce server to all items and then sync them
        """
        from woocommerce_fusion.tasks.sync_items import run_item_sync

        # Show initial toast notification
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("Starting to add WooCommerce server to items and sync..."),
                "indicator": "blue",
            },
            user=frappe.session.user,
        )

        # Get all active items
        items = frappe.get_all(
            "Item",
            filters={"disabled": 0, "is_stock_item": 1},
            fields=["name"],
        )

        items_updated = 0
        items_queued = 0
        items_already_linked = 0
        errors = []

        # First, add this WooCommerce server to all items
        for item in items:
            try:
                item_doc = frappe.get_doc("Item", item.name)

                # Check if this server is already in the woocommerce_servers table
                server_exists = False
                for server_row in item_doc.get("woocommerce_servers", []):
                    if server_row.woocommerce_server == self.name:
                        server_exists = True
                        items_already_linked += 1
                        break

                # Add the server if it doesn't exist
                if not server_exists:
                    item_doc.append(
                        "woocommerce_servers",
                        {
                            "woocommerce_server": self.name,
                            "enabled": 1,
                        },
                    )
                    item_doc.save(ignore_permissions=True)
                    items_updated += 1

                    # Show progress notification every 10 items
                    if items_updated % 10 == 0:
                        frappe.publish_realtime(
                            "show_alert",
                            {
                                "message": _("Added server to {0} items...").format(items_updated),
                                "indicator": "blue",
                            },
                            user=frappe.session.user,
                        )

            except Exception as e:
                errors.append(f"Item {item.name}: {str(e)}")
                frappe.log_error(
                    message=f"Failed to add WooCommerce server to item {item.name}: {str(e)}",
                    title="Add WooCommerce Server to Item Error",
                )

        # Show update completion notification
        if items_updated > 0:
            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("Added WooCommerce server to {0} items. Starting sync...").format(
                        items_updated
                    ),
                    "indicator": "blue",
                },
                user=frappe.session.user,
            )

        # Now queue all items for sync
        for item in items:
            try:
                frappe.enqueue(
                    run_item_sync,
                    queue="long",
                    item_code=item.name,
                    enqueue_after_commit=True,
                )
                items_queued += 1
            except Exception as e:
                errors.append(f"Queue {item.name}: {str(e)}")
                frappe.log_error(
                    message=f"Failed to queue item {item.name}: {str(e)}",
                    title="Push Items to WooCommerce Error",
                )

        # Show detailed results
        if errors:
            message = _(
                "<b>WooCommerce Server Added and Sync Started!</b><br><br>"
                "<b>Items with server added:</b> {0}<br>"
                "<b>Items already linked:</b> {1}<br>"
                "<b>Items queued for sync:</b> {2}<br>"
                "<b>Errors:</b> {3}<br><br>"
                "This will create new products or update existing ones in WooCommerce.<br><br>"
                "<b>Check progress:</b><br>"
                "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                "• Or check <b>RQ Console</b> for job status<br>"
                "• Monitor <b>Error Log</b> for any issues<br><br>"
                "Sync is running in the background and may take several minutes depending on the number of items."
            ).format(items_updated, items_already_linked, items_queued, len(errors))
            frappe.msgprint(message, title=_("Sync Started with Warnings"), indicator="orange")
        else:
            message = _(
                "<b>WooCommerce Server Added and Sync Started!</b><br><br>"
                "<b>Items with server added:</b> {0}<br>"
                "<b>Items already linked:</b> {1}<br>"
                "<b>Items queued for sync:</b> {2}<br><br>"
                "This will create new products or update existing ones in WooCommerce.<br><br>"
                "<b>Check progress:</b><br>"
                "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                "• Or check <b>RQ Console</b> for job status<br>"
                "• Monitor <b>Error Log</b> for any issues<br><br>"
                "Sync is running in the background and may take several minutes depending on the number of items."
            ).format(items_updated, items_already_linked, items_queued)
            frappe.msgprint(message, title=_("Sync Started Successfully"), indicator="green")

        # Show final toast
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("{0} items queued. Server added to {1} items.").format(
                    items_queued, items_updated
                ),
                "indicator": "green",
            },
            user=frappe.session.user,
        )

    @frappe.whitelist()
    def import_new_wc_products(self):
        """
        Import products from WooCommerce that are not already linked to ERPNext items
        """
        from woocommerce import API

        from woocommerce_fusion.tasks.sync_items import run_item_sync

        # Show initial toast notification
        frappe.publish_realtime(
            "show_alert",
            {
                "message": _("Fetching products from WooCommerce..."),
                "indicator": "blue",
            },
            user=frappe.session.user,
        )

        try:
            # Create WooCommerce API connection
            wc_api = API(
                url=self.woocommerce_server_url,
                consumer_key=self.api_consumer_key,
                consumer_secret=self.api_consumer_secret,
                version="wc/v3",
                timeout=(10, 40),  # (connect_timeout, read_timeout) - prevents SSL handshake hangs
                verify_ssl=verify_ssl,
            )

            # Get all products from WooCommerce (paginated)
            all_products = []
            page = 1
            per_page = 100

            while True:
                products = wc_api.get(
                    "products", params={"per_page": per_page, "page": page}
                ).json()
                if not products:
                    break
                all_products.extend(products)
                page += 1

                # Update progress every 100 products
                if len(all_products) % 100 == 0:
                    frappe.publish_realtime(
                        "show_alert",
                        {
                            "message": _("Fetched {0} products...").format(len(all_products)),
                            "indicator": "blue",
                        },
                        user=frappe.session.user,
                    )

            # Fetch variations for variable products
            variable_products = [p for p in all_products if p.get("type") == "variable"]
            if variable_products:
                frappe.publish_realtime(
                    "show_alert",
                    {
                        "message": _("Fetching variations for {0} variable products...").format(
                            len(variable_products)
                        ),
                        "indicator": "blue",
                    },
                    user=frappe.session.user,
                )

                variations_fetched = 0
                for variable_product in variable_products:
                    parent_id = variable_product.get("id")
                    parent_name = variable_product.get("name", "Unknown")
                    variation_page = 1

                    while True:
                        variations = wc_api.get(
                            f"products/{parent_id}/variations",
                            params={"per_page": per_page, "page": variation_page},
                        ).json()
                        if not variations:
                            break

                        # Add parent_id and type to each variation for proper identification
                        for variation in variations:
                            variation["parent_id"] = parent_id
                            variation["type"] = "variation"
                            variation["parent_name"] = parent_name

                        all_products.extend(variations)
                        variations_fetched += len(variations)
                        variation_page += 1

                if variations_fetched > 0:
                    frappe.publish_realtime(
                        "show_alert",
                        {
                            "message": _("Fetched {0} variations...").format(variations_fetched),
                            "indicator": "blue",
                        },
                        user=frappe.session.user,
                    )

            # Count products and variations separately for reporting
            main_products_count = len([p for p in all_products if p.get("type") != "variation"])
            variations_count = len([p for p in all_products if p.get("type") == "variation"])

            # Show total products fetched
            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("Fetched {0} products and {1} variations. Checking which are new...").format(
                        main_products_count, variations_count
                    ),
                    "indicator": "blue",
                },
                user=frappe.session.user,
            )

            # Get all existing product IDs for this server
            existing_product_ids = set()
            existing_links = frappe.get_all(
                "Item WooCommerce Server",
                filters={"woocommerce_server": self.name},
                fields=["woocommerce_id"],
            )
            for link in existing_links:
                if link.woocommerce_id:
                    existing_product_ids.add(str(link.woocommerce_id))

            # Find products that are not linked to items
            new_products = []
            for product in all_products:
                product_id = str(product.get("id"))
                if product_id not in existing_product_ids:
                    new_products.append(product)

            # Count new products and variations separately
            new_main_products = [p for p in new_products if p.get("type") != "variation"]
            new_variations = [p for p in new_products if p.get("type") == "variation"]

            # Show how many new products found
            if not new_products:
                frappe.msgprint(
                    _(
                        "No new products found to import. All {0} products and {1} variations from WooCommerce are already linked to items."
                    ).format(main_products_count, variations_count),
                    title=_("No New Products"),
                    indicator="blue",
                )
                frappe.publish_realtime(
                    "show_alert",
                    {
                        "message": _("No new products to import."),
                        "indicator": "blue",
                    },
                    user=frappe.session.user,
                )
                return

            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("Found {0} new products and {1} new variations. Starting import...").format(
                        len(new_main_products), len(new_variations)
                    ),
                    "indicator": "blue",
                },
                user=frappe.session.user,
            )

            # Queue the new products for sync
            from woocommerce_fusion.woocommerce.woocommerce_api import (
                generate_woocommerce_record_name_from_domain_and_id,
            )

            products_queued = 0
            errors = []

            for product in new_products:
                try:
                    # Generate WooCommerce Product virtual doctype name
                    wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
                        self.name, product.get("id")
                    )

                    # Queue for sync
                    frappe.enqueue(
                        run_item_sync,
                        queue="long",
                        woocommerce_product_name=wc_product_name,
                        enqueue_after_commit=True,
                    )
                    products_queued += 1

                    # Show progress notification every 10 products
                    if products_queued % 10 == 0:
                        frappe.publish_realtime(
                            "show_alert",
                            {
                                "message": _("Queued {0} products...").format(products_queued),
                                "indicator": "blue",
                            },
                            user=frappe.session.user,
                        )

                except Exception as e:
                    errors.append(
                        f"Product {product.get('id')} ({product.get('name', 'Unknown')}): {str(e)}"
                    )
                    frappe.log_error(
                        message=f"Failed to queue product {product.get('id')}: {str(e)}",
                        title="Import New WooCommerce Products Error",
                    )

            # Show detailed results
            if errors:
                message = _(
                    "<b>Import Started with Warnings!</b><br><br>"
                    "<b>Total products on WooCommerce:</b> {0} ({1} products + {2} variations)<br>"
                    "<b>Already linked to items:</b> {3}<br>"
                    "<b>New items found:</b> {4} ({5} products + {6} variations)<br>"
                    "<b>Items queued for import:</b> {7}<br>"
                    "<b>Errors:</b> {8}<br><br>"
                    "New products and variations will be created as items in ERPNext.<br><br>"
                    "<b>Check progress:</b><br>"
                    "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                    "• Or check <b>RQ Console</b> for job status<br>"
                    "• Monitor <b>Error Log</b> for any issues<br><br>"
                    "Import is running in the background and may take several minutes."
                ).format(
                    len(all_products),
                    main_products_count,
                    variations_count,
                    len(existing_product_ids),
                    len(new_products),
                    len(new_main_products),
                    len(new_variations),
                    products_queued,
                    len(errors),
                )
                frappe.msgprint(
                    message, title=_("Import Started with Warnings"), indicator="orange"
                )
            else:
                message = _(
                    "<b>Import Started Successfully!</b><br><br>"
                    "<b>Total products on WooCommerce:</b> {0} ({1} products + {2} variations)<br>"
                    "<b>Already linked to items:</b> {3}<br>"
                    "<b>New items found:</b> {4} ({5} products + {6} variations)<br>"
                    "<b>Items queued for import:</b> {7}<br><br>"
                    "New products and variations will be created as items in ERPNext.<br><br>"
                    "<b>Check progress:</b><br>"
                    "• Go to <b>Background Jobs</b> (search in awesome bar)<br>"
                    "• Or check <b>RQ Console</b> for job status<br>"
                    "• Monitor <b>Error Log</b> for any issues<br><br>"
                    "Import is running in the background and may take several minutes."
                ).format(
                    len(all_products),
                    main_products_count,
                    variations_count,
                    len(existing_product_ids),
                    len(new_products),
                    len(new_main_products),
                    len(new_variations),
                    products_queued,
                )
                frappe.msgprint(message, title=_("Import Started Successfully"), indicator="green")

            # Show final toast
            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("{0} new products queued for import.").format(products_queued),
                    "indicator": "green",
                },
                user=frappe.session.user,
            )

        except Exception as e:
            # Show error toast
            frappe.publish_realtime(
                "show_alert",
                {
                    "message": _("Failed to fetch products from WooCommerce."),
                    "indicator": "red",
                },
                user=frappe.session.user,
            )
            frappe.msgprint(
                _(
                    "Failed to fetch products from WooCommerce: {0}<br><br>Please check your WooCommerce server settings."
                ).format(str(e)),
                title=_("Error"),
                indicator="red",
            )
            frappe.log_error(
                message=f"Failed to import new WooCommerce products: {str(e)}",
                title="Import New WooCommerce Products Error",
            )


@frappe.whitelist()
def get_woocommerce_shipment_providers(woocommerce_server):
    """
    Return the Shipment Providers for a given WooCommerce Server domain
    """
    wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)
    return wc_server.wc_ast_shipment_providers
