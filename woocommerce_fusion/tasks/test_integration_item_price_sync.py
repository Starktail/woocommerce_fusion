from urllib.parse import urlparse

import frappe
from erpnext import get_default_company
from erpnext.stock.doctype.item.test_item import create_item

from woocommerce_fusion.tasks.sync_item_prices import run_item_price_sync
from woocommerce_fusion.tasks.test_integration_helpers import (
    TestIntegrationWooCommerce,
    get_woocommerce_server,
)


class TestIntegrationWooCommerceItemPriceSync(TestIntegrationWooCommerce):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()  # important to call super() methods when extending TestCase.

    def test_item_price_sync_when_synchronising_with_woocommerce(self):
        """
        Test that the Item Price Synchronisation method posts the correct price to a WooCommerce website.
        """
        # Create a new product in WooCommerce, set regular price to 10
        wc_product_id = self.post_woocommerce_product(product_name="ITEM002", regular_price=10)

        # Create the same product in ERPNext (with opening stock of 5, not 1) and link it
        item = create_item(
            "ITEM002", valuation_rate=10, warehouse=None, company=get_default_company()
        )
        item.woocommerce_servers = []
        row = item.append("woocommerce_servers")
        row.woocommerce_id = wc_product_id
        row.woocommerce_server = get_woocommerce_server(self.wc_url).name
        item.save()

        # Add an Item Price
        item_price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": "ITEM002",
                "price_list": "_Test Price List",
                "price_list_rate": 5000,
            }
        )
        item_price.insert()

        # Run synchronisation
        stock_update_result = run_item_price_sync(item_code=item.name)

        # Expect successful update
        self.assertEqual(stock_update_result, True)

        # Expect correct price of 5000 in WooCommerce
        wc_price = self.get_woocommerce_product_price(product_id=wc_product_id)
        self.assertEqual(float(wc_price), 5000)

    def test_item_price_sync_ignored_if_item_disabled_when_synchronising_with_woocommerce(self):
        """
        Test that the Item Price Synchronisation method does not post a price to a WooCommerce website when the item is disabled.
        """
        # Create a new product in WooCommerce, set regular price to 10
        wc_product_id = self.post_woocommerce_product(product_name="ITEM003", regular_price=10)

        # Create the same product in ERPNext (with opening stock of 5, not 1) and link it
        item = create_item(
            "ITEM003", valuation_rate=10, warehouse=None, company=get_default_company()
        )
        item.woocommerce_servers = []
        row = item.append("woocommerce_servers")
        row.woocommerce_id = wc_product_id
        row.woocommerce_server = get_woocommerce_server(self.wc_url).name

        # Disable the item
        item.disabled = 1
        item.save()

        # Add an Item Price
        item_price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": "ITEM003",
                "price_list": "_Test Price List",
                "price_list_rate": 6000,
            }
        )
        item_price.insert()

        # Run synchronisation
        stock_update_result = run_item_price_sync(item_code=item.name)

        # Expect successful update
        self.assertEqual(stock_update_result, True)

        # Expect correct unchanged price of 10 in WooCommerce
        wc_price = self.get_woocommerce_product_price(product_id=wc_product_id)
        self.assertEqual(float(wc_price), 10)
