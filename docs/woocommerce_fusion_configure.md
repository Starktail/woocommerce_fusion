# Configure WooCommerce Fusion

---

The first step is to create a **WooCommerce Server** document, representing your WooCommerce website.

![click on Add WooCommerce Server](images/add-wc-server.png)

Complete the "WooCommerce Server URL", "API consumer key" and "API consumer secret" fields. To find your API consumer key and secret, go to your WordPress admin panel and navigate to WooCommerce > Settings > Advanced > REST API, and click on "Add key". Make sure to add Read/Write permissions to the API key.

![WooCommerce API Settings](images/wc-api-settings.png)

![New WooCommerce Server](images/new-wc-server.png)

---

Click on the "Sales Orders" tab and complete the mandatory fields

!["Sales Orders" tab](images/so-tab-mandatory.png)

**Settings**:
- Synchronise Sales Order Line changes back

When set, adding/removing/changing Sales Order **Lines** will be synchronised back to the WooCommerce Order (Note: Sales Orders will always be synchronised, this setting is for sync'ing changed Sales Order **Lines** *back* to WooCommerce)

- Enable Payments Sync

Let the app create Payment Entries for paid Sales Orders. A mapping of Payment Method to Bank Account is required:

A **Payment Entry** will only be created if the following conditions are true:

- `WooCommerce Order` > `Payment Method` is set 
**and**
- `WooCommerce Order` > `Date Paid` is set (unless `Ignore empty 'Date Paid' field on WooCommerce Orders` is set on `WooCommerce Server`

*When the payment method is "Cash on Delivery" (cod), the `Date Paid` field would usually be blank, so creation of a *Payment Entry* won't happen. If you want to be sure, you can add `cod` in the mapping:

```json
{
   "bacs": "1000-000 Bank Account",
   "cheque": "1000-100 Other Bank Account",
   "cod": ""
}
```

---

Click on the "Items" tab if you want to turn on Stock Level Synchronisation

!["Items" tab](images/items-tab.png)

**Settings**:
-  Default Item Code Naming Basis
   -  How the item code should be determined when an item is created, either "WooCommerce ID" or "Product SKU".
-  Enable Stock Level Synchronisation
   -  Turns on Syncrhonisation of Item Stock Levels to WooCommerce
-  Warehouses
   -  Select the Warehouses that should be taken into account when synchronising Item Stock Levels

---

---

Click on the "Price List" tab to configure price synchronisation

**Settings**:
- Enable Price List Sync
  - Turns on synchronisation of Item Prices from ERPNext to WooCommerce
- Price List
  - Prices from this list are pushed to the WooCommerce *Regular Price* field
- Delay per POST Request
  - Seconds to wait between each product price update (increase if your WooCommerce server is rate-limiting requests)
- Enable Sales Price List Sync
  - When enabled, a second price list can be synced to the WooCommerce *Sale Price* field
- Sales Price List
  - Prices from this list are pushed to the WooCommerce *Sale Price* field. If an Item Price record in this list has *Valid From* or *Valid Upto* dates, these are synced to WooCommerce as sale start/end dates.

See [Sync Item Prices](woocommerce_fusion_item-prices.md) for full details on how price and sale price sync works.

---

Click on the "Save" - and you are ready to go!
