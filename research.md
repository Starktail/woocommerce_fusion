# WooCommerce Fusion — Item & Item Price Sync: Deep Technical Research

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture & Key Data Structures](#2-architecture--key-data-structures)
3. [Item Sync — Trigger Points](#3-item-sync--trigger-points)
4. [Item Sync — Core Logic: SynchroniseItem](#4-item-sync--core-logic-synchroniseitem)
5. [Sync Direction & Conflict Resolution](#5-sync-direction--conflict-resolution)
6. [ERPNext → WooCommerce: Create & Update](#6-erpnext--woocommerce-create--update)
7. [WooCommerce → ERPNext: Create & Update](#7-woocommerce--erpnext-create--update)
8. [Scheduled (Hourly) Item Sync](#8-scheduled-hourly-item-sync)
9. [Item Price Sync — Architecture](#9-item-price-sync--architecture)
10. [Item Price Sync — Core Logic: SynchroniseItemPrice](#10-item-price-sync--core-logic-synchroniseitemprice)
11. [Field Mapping & JSONPath Integration](#11-field-mapping--jsonpath-integration)
12. [WooCommerce API Layer](#12-woocommerce-api-layer)
13. [Variant / Attribute Handling](#13-variant--attribute-handling)
14. [Stock Level Sync (Related Context)](#14-stock-level-sync-related-context)
15. [Configuration Reference](#15-configuration-reference)
16. [Custom Fields Added to Standard DocTypes](#16-custom-fields-added-to-standard-doctypes)
17. [Error Handling & Logging](#17-error-handling--logging)
18. [UI Integration](#18-ui-integration)
19. [End-to-End Sync Flow Diagrams](#19-end-to-end-sync-flow-diagrams)
20. [Key Gotchas & Implementation Nuances](#20-key-gotchas--implementation-nuances)
21. [File Location Reference](#21-file-location-reference)

---

## 1. Overview

WooCommerce Fusion is a bidirectional synchronisation connector between **Frappe/ERPNext v15+** and **WooCommerce**. It supports syncing:

- **Items / Products** — bidirectional, timestamp-based conflict resolution
- **Item Prices** — ERPNext → WooCommerce (one-directional), price-list-based
- **Stock Levels** — ERPNext → WooCommerce, triggered on stock document submissions
- **Sales Orders** — WooCommerce → ERPNext (out of scope here)

The connector supports **multiple WooCommerce servers** per ERPNext instance. A single ERPNext Item can be linked to several WooCommerce sites simultaneously, each tracked independently.

---

## 2. Architecture & Key Data Structures

### 2.1 High-Level Component Map

```
hooks.py
  ├── Item.on_update / after_insert  →  run_item_sync_from_hook()
  ├── Item Price.on_update           →  update_item_price_for_woocommerce_item_from_hook()
  ├── Scheduled Hourly               →  sync_woocommerce_products_modified_since()
  └── Scheduled Daily                →  run_item_price_sync_in_background()

tasks/sync_items.py
  ├── run_item_sync()                 [orchestrator]
  └── SynchroniseItem                [core sync class]
       ├── get_corresponding_item_or_product()
       ├── sync_wc_product_with_erpnext_item()  [direction decision]
       ├── create_item()             [WC → ERPNext]
       ├── update_item()             [WC → ERPNext]
       ├── create_woocommerce_product()  [ERPNext → WC]
       └── update_woocommerce_product()  [ERPNext → WC]

tasks/sync_item_prices.py
  ├── run_item_price_sync()           [orchestrator]
  └── SynchroniseItemPrice           [core price sync class]
       ├── get_erpnext_item_prices()
       └── sync_items_with_woocommerce_products()

woocommerce/woocommerce_api.py
  └── WooCommerceResource            [virtual doctype base, wraps WC REST API]

woocommerce/doctype/woocommerce_product/
  └── WooCommerceProduct             [virtual doctype, /products endpoint]
```

### 2.2 ERPNextItemToSync Dataclass

**File**: `tasks/sync_items.py`, lines 126–135

```python
@dataclass
class ERPNextItemToSync:
    item: Item
    item_woocommerce_server_idx: int  # 1-based index into item.woocommerce_servers

    @property
    def item_woocommerce_server(self):
        return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]
```

This dataclass binds an ERPNext `Item` document to one specific entry in its `woocommerce_servers` child table, effectively creating a "Item + Server" pair as the unit of sync work.

### 2.3 WooCommerce Product Virtual DocType

`WooCommerceProduct` is a **Frappe virtual doctype** that does not have a database table. All CRUD operations are forwarded to the WooCommerce REST API:

| Frappe Method | WC API Call |
|---------------|-------------|
| `db_insert()` | `POST /wp-json/wc/v3/products` |
| `db_update()` | `PUT /wp-json/wc/v3/products/{id}` |
| `load_from_db()` | `GET /wp-json/wc/v3/products/{id}` |
| `get_list()` | `GET /wp-json/wc/v3/products` (paginated) |

Variations use a sub-resource: `products/{parent_id}/variations/{id}`.

### 2.4 Record Naming Convention

WooCommerce records inside Frappe use the format:

```
{domain}~{woocommerce_resource_id}
```

Examples:
- `mystore.woocommerce.com~42`
- `shop.example.com~1001`

The delimiter `~` is defined as `WC_RESOURCE_DELIMITER` in `woocommerce_api.py`. Helper functions:

- `generate_woocommerce_record_name_from_domain_and_id()` (line 493)
- `get_domain_and_id_from_woocommerce_record_name()` (line 611)
- `parse_domain_from_url()` (line 604)

---

## 3. Item Sync — Trigger Points

### 3.1 Document Hooks (Real-Time)

Defined in `hooks.py` lines 127–135:

```python
doc_events = {
    "Item": {
        "on_update": "woocommerce_fusion.tasks.sync_items.run_item_sync_from_hook",
        "after_insert": "woocommerce_fusion.tasks.sync_items.run_item_sync_from_hook",
    }
}
```

**`run_item_sync_from_hook()`** (lines 25–39):
1. Checks `item.created_by_sync != True` — this flag prevents recursive syncs when items are created/updated by the sync itself.
2. Shows a blue info alert to the user: "Syncing with WooCommerce..."
3. Enqueues `clear_sync_hash_and_run_item_sync()` with `enqueue_after_commit=True` so the background job only starts after the current database transaction commits.

**`clear_sync_hash_and_run_item_sync()`**:
- Clears `woocommerce_last_sync_hash` on the `Item WooCommerce Server` child row.
- Then calls `run_item_sync()`.
- Clearing the hash forces the sync to re-evaluate, because the hash comparison (see Section 5) would otherwise consider the item "already synced."

### 3.2 Scheduled Tasks

Defined in `hooks.py` lines 147–154:

```python
scheduler_events = {
    "hourly_long": [
        "woocommerce_fusion.tasks.sync_items.sync_woocommerce_products_modified_since",
    ],
    "daily_long": [
        "woocommerce_fusion.tasks.sync_items.run_item_price_sync_in_background",
    ],
}
```

- **Hourly Long**: Pulls all WooCommerce products modified since the last recorded sync date and enqueues per-product syncs.
- **Daily Long**: Full price list sync for all items linked to WooCommerce servers.

### 3.3 Manual / Programmatic Triggers

Via ERPNext Item form (UI buttons, see Section 18):
- "Sync this Item with WooCommerce" → calls `run_item_sync(item_code)`
- "Sync this Item's Price to WooCommerce" → calls `run_item_price_sync(item_code)`
- "Sync this Item's Stock Levels to WooCommerce" → calls `update_stock_levels_on_woocommerce_site(item_code)`

---

## 4. Item Sync — Core Logic: SynchroniseItem

**File**: `tasks/sync_items.py`, lines 138–587

### 4.1 Entry Point: `run_item_sync()`

Lines 42–92. Accepts either:
- `item_code` (str) + optional `item` doc — resolves to an ERPNext Item
- `woocommerce_product_name` (str) + optional `woocommerce_product` doc — resolves to a WC product

For each WooCommerce Server found on the item (iterating `item.woocommerce_servers`), it:
1. Constructs an `ERPNextItemToSync` with the item and the server index.
2. Creates a `SynchroniseItem` instance.
3. Either calls `.run()` synchronously or enqueues it as a background job depending on the `enqueue` parameter.
4. Returns `(Item, WooCommerceProduct)`.

### 4.2 SynchroniseItem.run() — Main Workflow

Lines 154–172:

```python
def run(self):
    try:
        self.get_corresponding_item_or_product()
        self.sync_wc_product_with_erpnext_item()
    except Exception:
        error_message = frappe.get_traceback()
        # Append item and product data to error message
        frappe.log_error("WooCommerce Error", error_message)
```

### 4.3 `get_corresponding_item_or_product()`

Lines 174–230. Fills in the missing side of the (item, product) pair:

**Case A — Have item, need product:**
- Reads `item_woocommerce_server.woocommerce_id`.
- If not set, attempts to find a WC product by SKU (product's `sku` field matches `item.item_code`).
- If found by SKU, immediately writes the `woocommerce_id` back to the child table.
- If not found at all, `self.woocommerce_product` stays `None` → triggers product creation.

**Case B — Have product, need item:**
- Queries `Item WooCommerce Server` child table for a row where `woocommerce_id` matches the product's ID.
- If not found, tries matching by item_code against the product's SKU.
- If still not found, `self.item` stays `None` → triggers item creation.

---

## 5. Sync Direction & Conflict Resolution

### 5.1 Decision Logic

`sync_wc_product_with_erpnext_item()` — lines 232–255:

```
IF self.item is None:
    → create_item()          (WooCommerce → ERPNext)

ELIF self.woocommerce_product is None:
    → create_woocommerce_product()  (ERPNext → WooCommerce)

ELSE:  # Both exist
    woocommerce_date_modified = woocommerce_product.woocommerce_date_modified
    last_sync_hash = item.item_woocommerce_server.woocommerce_last_sync_hash

    IF woocommerce_date_modified == last_sync_hash:
        # Nothing changed on WooCommerce side since last sync
        # But ERPNext side might have changed → handled by hook trigger

    ELIF woocommerce_date_modified > item.modified:
        → update_item()      (WooCommerce → ERPNext: WC is newer)

    ELIF woocommerce_date_modified < item.modified:
        → update_woocommerce_product()  (ERPNext → WooCommerce: ERPNext is newer)
```

### 5.2 The Sync Hash Mechanism

The `woocommerce_last_sync_hash` field on the `Item WooCommerce Server` child table stores the **WooCommerce product's `date_modified` timestamp** at the moment of last successful sync.

Its purpose is two-fold:
1. **Loop prevention**: After syncing ERPNext → WooCommerce, WooCommerce returns a new `date_modified`. That value is stored as the hash. On the next hourly scan, this product would match the `date_time_from` filter — but the hash comparison immediately identifies that this is the same state that was just synced, skipping redundant work.
2. **Change detection**: If `date_modified != last_sync_hash`, something changed in WooCommerce since last sync. Combined with timestamp comparison, the system decides which system to trust.

**How it's updated** — `set_sync_hash()` (lines 571–577):
```python
frappe.db.set_value(
    "Item WooCommerce Server",
    child_row_name,
    "woocommerce_last_sync_hash",
    woocommerce_date_modified,
    update_modified=False  # Doesn't bump Item.modified, avoids re-triggering hook
)
```

**When it's cleared**: Before a hook-triggered sync (`clear_sync_hash_and_run_item_sync()`). This forces the comparison to detect a difference, ensuring the hook-triggered change propagates.

### 5.3 Conflict Resolution Summary

| Scenario | Winner | Action |
|----------|--------|--------|
| Only product exists | WooCommerce | Create ERPNext Item |
| Only item exists (with WC server entry) | ERPNext | Create WooCommerce Product |
| Both exist, WC newer | WooCommerce | Update ERPNext Item |
| Both exist, ERPNext newer | ERPNext | Update WooCommerce Product |
| Both exist, hash matches WC date | No change needed | Skip |

There is **no manual priority override** — it is strictly last-write-wins based on timestamps.

---

## 6. ERPNext → WooCommerce: Create & Update

### 6.1 Create WooCommerce Product

`create_woocommerce_product()` — lines 303–374

**When triggered**: Item has a `woocommerce_servers` entry with a configured server but no `woocommerce_id`.

**Steps**:
1. Create a new `WooCommerceProduct` document in-memory.
2. Set `woocommerce_server` to the server name.
3. Set `woocommerce_name` = `item.item_name`.
4. Determine product type:
   - `"variable"` if `item.has_variants == 1`
   - `"variation"` if `item.variant_of` is set
   - `"simple"` otherwise
5. If `type == "variable"`: Build `attributes` JSON from `Item Attribute` docs and set it on the product.
6. If `type == "variation"`: Find parent's `woocommerce_id` from the parent item's `woocommerce_servers` table and set `parent_id`.
7. If `WooCommerce Server.enable_price_list_sync`: Set `regular_price` via `get_item_price_rate()`.
8. Apply custom field mappings via `set_product_fields()`.
9. Call `woocommerce_product.insert()` → `db_insert()` → `POST /products`.
10. WooCommerce returns the new product ID and `date_modified`; these are written back to `Item WooCommerce Server.woocommerce_id`.
11. Call `set_sync_hash()` with the returned `date_modified`.

### 6.2 Update WooCommerce Product

`update_woocommerce_product()` — lines 282–301

**When triggered**: Both item and product exist, and ERPNext item is newer.

**Steps**:
1. Compare `item.item_name` with `woocommerce_product.woocommerce_name`; update if different.
2. If `WooCommerce Server.enable_image_sync` and item has an image URL: update `woocommerce_product.images`.
3. Apply custom field mappings via `set_product_fields()`.
4. If any field changed (`product_dirty == True`): call `woocommerce_product.save()` → `db_update()` → `PUT /products/{id}`.
5. Call `set_sync_hash()`.

### 6.3 `set_product_fields()` — Field Mapping ERPNext → WooCommerce

Lines 510–564.

For each entry in `WooCommerce Server.item_field_map`:
1. Read `erpnext_field_name` value from `self.item.item`.
2. Deserialize WooCommerce product fields that are stored as JSON strings (e.g., `meta_data`, `attributes`).
3. Use `jsonpath_ng.ext.parse(woocommerce_field_name).update(wc_product_dict, value)` to write the value into the correct nested location.
4. Re-serialize after updates.
5. Track if anything actually changed (`product_dirty` flag).

**Strict validation for updates**: If the JSONPath expression targets a path that doesn't exist in the product dict, an error is raised. For new product creation, missing paths are allowed (the field just won't be set).

---

## 7. WooCommerce → ERPNext: Create & Update

### 7.1 Create ERPNext Item

`create_item()` — lines 376–440

**When triggered**: A WooCommerce product has no matching ERPNext Item.

**Item Code Determination** based on `WooCommerce Server.name_by`:
- `"Product SKU"`: Uses `woocommerce_product.sku` as `item_code`.
- `"WooCommerce ID"` (default): Uses `woocommerce_product.woocommerce_id` (the numeric WC product ID).

**Key fields set**:

| Item Field | Source |
|------------|--------|
| `item_code` | SKU or WC ID (per `name_by` setting) |
| `item_name` | `woocommerce_product.woocommerce_name` |
| `item_group` | `WooCommerce Server.item_group` |
| `stock_uom` | `WooCommerce Server.uom` (fallback: `"Nos"`) |
| `image` | First image URL from WC product (if `enable_image_sync`) |
| `variant_of` | Parent item's `item_code` (for variations) |
| Custom fields | Via `set_item_fields()` field mappings |

**Variant handling**:
- For **variable** products (`type == "variable"`): Creates `Item Attribute` docs for each product attribute.
- For **variation** products (`type == "variation"`): Sets `variant_of` to the parent item's code; also adds attribute values from the variation's attributes.

**Insert flags**:
```python
item.flags.ignore_mandatory = True   # Skip mandatory field validation
item.flags.created_by_sync = True    # Prevent on_update hook from re-triggering sync
item.insert()
```

After insert, adds a row to `item.woocommerce_servers` with `woocommerce_id` and server info, then calls `set_sync_hash()`.

### 7.2 Update ERPNext Item

`update_item()` — lines 257–280

**When triggered**: Both exist, WooCommerce product has a more recent `date_modified` than `item.modified`.

**Steps**:
1. Update `item.item_name` from `woocommerce_product.woocommerce_name` if different.
2. Update `item.image` from first WC product image if `enable_image_sync`.
3. Apply custom field mappings via `set_item_fields()`.
4. If any change detected (`item_dirty == True`):
   - Set `item.flags.created_by_sync = True` (prevents recursive hook).
   - Call `item.save()`.
5. Call `set_sync_hash()`.

### 7.3 `set_item_fields()` — Field Mapping WooCommerce → ERPNext

Lines 483–508.

For each entry in `WooCommerce Server.item_field_map`:
1. Deserialize WooCommerce product JSON fields.
2. Use `jsonpath_ng.ext.parse(woocommerce_field_name).find(wc_product_dict)` to extract the value.
3. Assign the extracted value to `item.{erpnext_field_name}`.
4. Set `item_dirty = True` if value changed.

---

## 8. Scheduled (Hourly) Item Sync

### 8.1 `sync_woocommerce_products_modified_since()`

Lines 95–123.

**Flow**:
1. Read `WooCommerce Integration Settings.wc_last_sync_date_items` (the last time this ran).
2. Call `get_list_of_wc_products(date_time_from=last_sync_date)`.
3. For each product returned:
   - Enqueue `run_item_sync(woocommerce_product=product, enqueue=True)`.
4. Update `wc_last_sync_date_items = frappe.utils.now()`.

On the **first run** (no `wc_last_sync_date_items`), fetches all products. Subsequent runs fetch only those modified since the last sync, making it efficient.

### 8.2 `get_list_of_wc_products()`

Lines 590–633.

- Paginates: fetches 100 products per page, continues until no more pages.
- Handles **multiple servers**: iterates all enabled `WooCommerce Server` docs.
- Supports an optional `item` parameter — if provided, fetches only the specific product linked to that item (for hook-triggered syncs).
- Builds product names as `{domain}~{id}` for each returned product.
- Includes product **variations** (fetched separately per variable product).

---

## 9. Item Price Sync — Architecture

### 9.1 Design Philosophy

Item price sync is **one-directional**: ERPNext → WooCommerce only.

- ERPNext is the **source of truth** for prices.
- The system reads from a configured `Price List` and pushes `regular_price` to WooCommerce.
- There is no mechanism to pull WooCommerce prices back to ERPNext.

### 9.2 Trigger Points

**Hook-based** (hooks.py line 127):
```python
"Item Price": {
    "on_update": "woocommerce_fusion.tasks.sync_item_prices.update_item_price_for_woocommerce_item_from_hook"
}
```

`update_item_price_for_woocommerce_item_from_hook()`:
- Checks if the changed Item Price belongs to a WooCommerce-linked item on the relevant price list.
- Enqueues `run_item_price_sync(item_code, item_price_doc)` with `enqueue_after_commit=True`.

**Scheduled** (hooks.py line 153):
```python
"daily_long": ["woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync_in_background"]
```

`run_item_price_sync_in_background()`:
- Enqueues `run_item_price_sync()` with `queue="long"` and `timeout=3600`.
- No item filter — syncs all items across all servers.

---

## 10. Item Price Sync — Core Logic: SynchroniseItemPrice

**File**: `tasks/sync_item_prices.py`, lines 40–134

### 10.1 `run_item_price_sync()` — Entry Point

Lines 34–37:
```python
def run_item_price_sync(item_code=None, item_price_doc=None):
    SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc).run()
```

### 10.2 `SynchroniseItemPrice.run()`

Lines 60–67:

```python
def run(self):
    for server in self.servers:  # All enabled WooCommerce Servers
        self.wc_server = server
        self.get_erpnext_item_prices()
        self.sync_items_with_woocommerce_products()
```

### 10.3 `get_erpnext_item_prices()`

Lines 69–96. Runs the following SQL join:

```sql
SELECT
    Item Price.name,
    Item Price.item_code,
    Item Price.price_list_rate,
    Item WooCommerce Server.woocommerce_server,
    Item WooCommerce Server.woocommerce_id
FROM `tabItem Price`
JOIN `tabItem WooCommerce Server`
    ON Item Price.item_code = Item WooCommerce Server.parent
    AND Item WooCommerce Server.parenttype = 'Item'
JOIN `tabItem`
    ON Item.name = Item WooCommerce Server.parent
WHERE
    Item Price.price_list = {server.price_list}
    AND Item WooCommerce Server.woocommerce_server = {server.name}
    AND Item.disabled = 0
    AND Item WooCommerce Server.woocommerce_id IS NOT NULL
    AND Item WooCommerce Server.enabled = 1
    [AND Item Price.item_code = {item_code}]  -- if item_code provided
```

Results are stored in `self.item_price_list`.

### 10.4 `sync_items_with_woocommerce_products()`

Lines 98–133.

For each item price record:
1. Construct WooCommerce product name: `{domain}~{woocommerce_id}`.
2. Load WooCommerce Product via `frappe.get_doc("WooCommerce Product", name)`.
3. Compare prices:
   ```python
   wc_price = float(wc_product.regular_price or 0)
   erp_price = float(item_price.price_list_rate or 0)
   if wc_price != erp_price:
       wc_product.regular_price = erp_price
       wc_product.save()  # PUT /products/{id}
   ```
4. Sleep `WooCommerce Server.price_list_delay_per_item` seconds (default: 2) between requests.
5. Exceptions per product are caught, logged, and the loop continues.

### 10.5 Price on Product Creation

`get_item_price_rate()` — lines 636–655:

When **creating** a new WooCommerce product from an ERPNext item, if `enable_price_list_sync` is enabled:
- Queries Item Price for:
  - `item_code = item.name`
  - `price_list = server.price_list`
  - Valid: no `valid_upto` OR `valid_upto > now()`
- Returns `price_list_rate` of the first valid price.
- Sets `woocommerce_product.regular_price = rate` before the initial `POST`.

---

## 11. Field Mapping & JSONPath Integration

### 11.1 Configuration

Field mappings are stored in a child table `WooCommerce Server Item Field` on each `WooCommerce Server` document (field name: `item_field_map`).

Each row has:
| Field | Purpose |
|-------|---------|
| `erpnext_field_name` | Name of the ERPNext Item field |
| `woocommerce_field_name` | JSONPath expression pointing to a location in the WooCommerce product JSON |

### 11.2 JSONPath Expression Examples

| JSONPath | Targets |
|----------|---------|
| `$.short_description` | Top-level WC field |
| `$.meta_data[?(@.key=='_custom_field')].value` | Value inside meta_data array filtered by key |
| `$.attributes[0].options[0]` | First option of first attribute |

The library used is `jsonpath-ng.ext` which supports extended JSONPath syntax including filter expressions (`?(@.key==...)`).

### 11.3 Validation

`WooCommerceServer.validate_item_map()` (server doctype's `validate()` hook):
- Parses each JSONPath expression using `jsonpath_ng.ext.parse()`.
- Raises a `frappe.ValidationError` if any expression is syntactically invalid.
- Explicitly **disallows** the expressions `"attributes"` and `"images"` — these are handled by built-in sync logic, not field mapping.

### 11.4 Serialization / Deserialization

WooCommerce product fields like `attributes`, `images`, `categories`, and `meta_data` are arrays/objects in the WC API but stored as JSON strings in Frappe's virtual doctype layer.

Before JSONPath operations, `deserialize_attributes_of_type_dict_or_list()` converts JSON strings back to Python dicts/lists. After updates, `serialize_attributes_of_type_dict_or_list()` converts them back to strings for storage and API transmission.

This means JSONPath queries always operate on **native Python data structures**, not raw strings.

---

## 12. WooCommerce API Layer

### 12.1 WooCommerceResource Base Class

**File**: `woocommerce/woocommerce_api.py`

All WooCommerce virtual doctypes inherit from this. Key methods:

**`load_from_db()`**: Parses the document name (`{domain}~{id}`), extracts the domain to find the correct `WooCommerce Server` config, calls `GET /products/{id}`, and populates `self` fields from the response via `pre_init_document()`.

**`db_insert()`**: Serializes the document, calls `POST /products`, captures the returned `id` and `date_modified`, and stores them back on the document.

**`db_update()`**: Serializes only the **changed fields** (tracked via Frappe's dirty field mechanism), calls `PUT /products/{id}`.

**`get_list_of_records()`**: Handles pagination — loops through pages of 100 records, collecting all results. Supports date filters via `modified_after` parameter.

**`pre_init_document()`**: Transforms the WooCommerce API response format into Frappe's expected format:
- Maps `id` → `woocommerce_id`
- Maps `name` (WC title) → `woocommerce_name`
- Serializes nested objects to JSON strings for storage

### 12.2 Request Logging via APIWithRequestLogging

**File**: `tasks/utils.py`

When `WooCommerce Server.enable_woocommerce_request_logs` is enabled:

1. `APIWithRequestLogging` wraps the `woocommerce` Python library's internal `_API__request()` method.
2. Every API call is intercepted: method, URL, params, request body, response body, status code, and elapsed time are captured.
3. An async background job logs this to a `WooCommerce Request Log` document.
4. Logs are automatically purged after 7 days (`hooks.py`, line 260–262).

### 12.3 Authentication

Uses OAuth 1.0a via WooCommerce's `api_consumer_key` and `api_consumer_secret`. These are stored on the `WooCommerce Server` document and passed to the `woocommerce` Python library on each instantiation.

---

## 13. Variant / Attribute Handling

### 13.1 ERPNext Item Types

| ERPNext Field | Meaning | WC Product Type |
|---------------|---------|-----------------|
| `has_variants = 0`, `variant_of = None` | Simple item | `"simple"` |
| `has_variants = 1` | Item template with variants | `"variable"` |
| `variant_of = {parent_code}` | Specific variant | `"variation"` |

### 13.2 Creating Variable Products from ERPNext Templates

When `item.has_variants == 1`:
1. Reads `Item Attribute` records linked to the item.
2. Builds an `attributes` JSON array: each attribute has a name and its possible values.
3. Sets `woocommerce_product.attributes = json.dumps(attributes_list)`.
4. WooCommerce receives this as `type: "variable"` with `attributes` array — WooCommerce then allows creating variations.

### 13.3 Creating Variations from ERPNext Variants

When `item.variant_of` is set:
1. Fetches the parent item's `woocommerce_id` from its `woocommerce_servers` child table.
2. Sets `woocommerce_product.parent_id = parent_woocommerce_id`.
3. Reads the variant's specific attribute values (e.g., `Color: Red`, `Size: Large`).
4. Sets `woocommerce_product.attributes` with the specific selected values.
5. Product is created via the variations sub-endpoint: `POST /products/{parent_id}/variations`.

### 13.4 Creating ERPNext Attributes from WooCommerce Variable Products

When `woocommerce_product.type == "variable"`:
- Iterates `woocommerce_product.attributes` (deserialized from JSON).
- For each attribute, ensures an `Item Attribute` doc exists in ERPNext (creates if missing).
- Adds attribute to the item template's `attributes` child table.

For `type == "variation"`:
- Sets `item.variant_of` to parent item code.
- Adds the specific attribute values to the variant item.

---

## 14. Stock Level Sync (Related Context)

While not the primary focus of this research, stock sync is tightly integrated with item sync architecture.

### 14.1 Triggers

Document hooks on submit/cancel:
- `Stock Entry`, `Stock Reconciliation`, `Sales Invoice` (if `update_stock == 1`), `Delivery Note`

All call `update_stock_levels_for_woocommerce_item()` → enqueues `update_stock_levels_on_woocommerce_site(item_code)`.

Scheduled: **Daily Long** runs `update_stock_levels_for_all_enabled_items_in_background()`.

### 14.2 Quantity Calculation

For each WooCommerce Server linked to the item:
1. Query all `Bin` records for the item.
2. Filter bins to those in `WooCommerce Server.warehouses` (Table MultiSelect).
3. Sum `actual_qty` across matching bins.
4. If `WooCommerce Server.subtract_reserved_stock == 1`: subtract `reserved_qty`.
5. Floor to integer (WooCommerce requires whole number stock counts).
6. PUT `stock_quantity` to the product.

---

## 15. Configuration Reference

### 15.1 WooCommerce Server Settings

**DocType**: `WooCommerce Server` (multi-document, one per WooCommerce site)

#### Connection
| Field | Type | Description |
|-------|------|-------------|
| `enable_sync` | Check | Master on/off switch — required for any sync |
| `woocommerce_server_url` | Data | Full URL of the WooCommerce site |
| `api_consumer_key` | Data | OAuth consumer key |
| `api_consumer_secret` | Password | OAuth consumer secret |
| `secret` | Data | Webhook verification hash |

#### Item Sync
| Field | Type | Description |
|-------|------|-------------|
| `name_by` | Select | How to set `item_code`: "WooCommerce ID" or "Product SKU" |
| `item_group` | Link | Default Item Group for items created from WooCommerce |
| `uom` | Link | Default Unit of Measure for new items |
| `enable_image_sync` | Check | Sync WC product's first image to `Item.image` |
| `item_field_map` | Table | Custom field mappings (JSONPath based) |

#### Price List
| Field | Type | Description |
|-------|------|-------------|
| `enable_price_list_sync` | Check | Enable price syncing |
| `price_list` | Link | ERPNext Price List to sync from |
| `price_list_delay_per_item` | Int | Seconds to sleep between price updates (default: 2) |

#### Stock
| Field | Type | Description |
|-------|------|-------------|
| `enable_stock_level_synchronisation` | Check | Enable stock sync |
| `warehouses` | Table | Warehouses included in stock calculations |
| `subtract_reserved_stock` | Check | Subtract `reserved_qty` from stock counts |

#### Orders
| Field | Type | Description |
|-------|------|-------------|
| `company` | Link | ERPNext Company for auto-created sales orders |
| `warehouse` | Link | Default warehouse for sales orders |
| `creation_user` | Link | User assigned as creator of auto-created docs |

#### Logging
| Field | Type | Description |
|-------|------|-------------|
| `enable_woocommerce_request_logs` | Check | Log all WC API calls to `WooCommerce Request Log` |

### 15.2 WooCommerce Integration Settings

**DocType**: `WooCommerce Integration Settings` (Single)

| Field | Description |
|-------|-------------|
| `wc_last_sync_date_items` | Timestamp of last hourly item sync run |
| `wc_last_sync_date` | Timestamp of last order sync run |
| `minimum_creation_date` | Orders created before this date are ignored |

### 15.3 Item WooCommerce Server (Child Table on Item)

**DocType**: `Item WooCommerce Server`

| Field | Description |
|-------|-------------|
| `enabled` | Whether sync is active for this server |
| `woocommerce_server` | Link to `WooCommerce Server` doc |
| `woocommerce_id` | Numeric product ID on WooCommerce |
| `woocommerce_last_sync_hash` | Stores WC `date_modified` at last sync (read-only) |
| `view_product` | Button: navigates to WooCommerce Product form |

---

## 16. Custom Fields Added to Standard DocTypes

Defined in `fixtures/custom_field.json`:

**On Item**:
- `woocommerce_servers` — Table field (links to `Item WooCommerce Server` child table)
- `custom_woocommerce_tab` — Tab Break creating a "WooCommerce" tab in the Item form

**On Sales Order** (for order sync, not item sync):
- `woocommerce_id`, `woocommerce_server`, `woocommerce_status`
- `woocommerce_payment_method`, `woocommerce_payment_entry`

---

## 17. Error Handling & Logging

### 17.1 Per-Sync Error Handling

In `SynchroniseItem.run()` (lines 161–172):
```python
except Exception:
    error_message = (
        frappe.get_traceback()
        + "\nItem data: " + str(self.item)
        + "\nProduct data: " + str(self.woocommerce_product)
    )
    frappe.log_error("WooCommerce Error", error_message)
```

The full traceback plus both the item and product data objects are logged. This helps diagnose partial state issues.

### 17.2 Per-Item Price Error Handling

In `sync_items_with_woocommerce_products()` (lines 130–132):
```python
except Exception:
    frappe.log_error("WooCommerce Price Sync Error", frappe.get_traceback())
    # Loop continues to next product
```

Failures on individual products do **not** halt the batch — the sync continues with the next product.

### 17.3 SyncDisabledError

**File**: `exceptions.py`

A custom exception `SyncDisabledError` is raised when:
- A `WooCommerce Server` has `enable_sync == 0`.
- The item's specific server entry has `enabled == 0`.

This is caught in the sync flow and treated as a non-error early exit (the item is silently skipped).

### 17.4 WooCommerce Request Logs

**DocType**: `WooCommerce Request Log`

Stores per-API-call records when logging is enabled:
- Request: method, endpoint, params, body
- Response: status code, body
- Metadata: server name, timestamp, elapsed time, calling code traceback
- Auto-deleted after 7 days (`hooks.py` line 260–262).

---

## 18. UI Integration

### 18.1 Custom Buttons on Item Form

**File**: `public/js/stock/item.js`

Three custom buttons are added to the Item form in the "WooCommerce" section:

1. **"Sync this Item with WooCommerce"**
   - Freezes the UI, calls `run_item_sync(item_code)` via `frappe.call`.
   - Shows success alert and reloads doc; shows error alert on failure.

2. **"Sync this Item's Price to WooCommerce"**
   - Calls `run_item_price_sync(item_code)` via `frappe.call`.

3. **"Sync this Item's Stock Levels to WooCommerce"**
   - Calls `update_stock_levels_on_woocommerce_site(item_code)` via `frappe.call`.

### 18.2 View Product Button in Child Table

In the `Item WooCommerce Server` child table on the Item form, a "View Product" button navigates to:
```
/app/woocommerce-product/{server}~{woocommerce_id}
```

This opens the WooCommerce Product virtual doctype form where the current WC state can be viewed.

---

## 19. End-to-End Sync Flow Diagrams

### 19.1 New WooCommerce Product → Create ERPNext Item

```
WC Product created on WooCommerce site
        ↓
[Hourly scheduler fires]
sync_woocommerce_products_modified_since()
        ↓
get_list_of_wc_products(date_time_from=last_sync_date)
  [paginates /products?after={date}, 100/page]
        ↓
For each WC product returned:
  enqueue run_item_sync(woocommerce_product=wc_product)
        ↓
SynchroniseItem.run()
  get_corresponding_item_or_product()
    → No matching Item found → self.item = None
        ↓
  sync_wc_product_with_erpnext_item()
    → self.item is None → create_item()
        ↓
  create_item()
    - item_code = wc_product.sku  OR  wc_product.id  (per name_by)
    - item_group = server.item_group
    - uom = server.uom
    - image = wc_product.images[0] (if enable_image_sync)
    - Apply set_item_fields() custom mappings
    - item.insert(ignore_mandatory=True, created_by_sync=True)
        ↓
  set_sync_hash(woocommerce_date_modified)
        ↓
Update wc_last_sync_date_items = now()
```

### 19.2 ERPNext Item Modified → Update WooCommerce Product

```
User edits ERPNext Item (e.g., changes item_name)
        ↓
Item.on_update hook fires
  run_item_sync_from_hook()
    - Check: created_by_sync != True ✓
    - Show alert: "Syncing with WooCommerce..."
    - enqueue_after_commit: clear_sync_hash_and_run_item_sync()
        ↓
[Transaction commits]
        ↓
clear_sync_hash_and_run_item_sync()
  - Clear woocommerce_last_sync_hash on Item WC Server row
        ↓
run_item_sync(item_code)
  SynchroniseItem.run()
  get_corresponding_item_or_product()
    → Load WC product via woocommerce_id
        ↓
  sync_wc_product_with_erpnext_item()
    - woocommerce_date_modified ≠ last_sync_hash (hash was cleared) ✓
    - item.modified > woocommerce_date_modified
    → update_woocommerce_product()
        ↓
  update_woocommerce_product()
    - Compare woocommerce_name vs item_name
    - Apply set_product_fields() custom mappings
    - If changed: woocommerce_product.save()
        → PUT /products/{id} with changed fields only
        ↓
  set_sync_hash(new woocommerce_date_modified from response)
```

### 19.3 WooCommerce Product Modified → Update ERPNext Item

```
Product updated on WooCommerce
        ↓
[Hourly scheduler fires]
sync_woocommerce_products_modified_since()
  → Fetches products modified since last_sync_date
  → Finds the modified product
        ↓
enqueue run_item_sync(woocommerce_product=modified_product)
        ↓
SynchroniseItem.run()
  get_corresponding_item_or_product()
    → Finds matching ERPNext Item via woocommerce_id
        ↓
  sync_wc_product_with_erpnext_item()
    - woocommerce_date_modified ≠ last_sync_hash ✓
    - woocommerce_date_modified > item.modified
    → update_item()
        ↓
  update_item()
    - item_name ← woocommerce_product.woocommerce_name
    - image ← wc_product.images[0] (if enable_image_sync)
    - Apply set_item_fields() custom mappings
    - item.flags.created_by_sync = True  [prevents hook re-trigger]
    - item.save()
        ↓
  set_sync_hash(woocommerce_date_modified)
```

### 19.4 Item Price Update → Sync to WooCommerce

```
User updates Item Price in ERPNext
        ↓
Item Price.on_update hook fires
  update_item_price_for_woocommerce_item_from_hook()
    - Check: item linked to WC server ✓
    - Check: price_list matches server.price_list ✓
    - enqueue_after_commit: run_item_price_sync(item_code)
        ↓
[Transaction commits]
        ↓
SynchroniseItemPrice(item_code=item_code).run()
  For each WooCommerce Server:
    get_erpnext_item_prices()
      [JOIN query: Item Price + Item WC Server + Item]
        ↓
    sync_items_with_woocommerce_products()
      For each item_price:
        - Load WC product
        - float(wc_product.regular_price) vs float(price_list_rate)
        - If different:
            wc_product.regular_price = price_list_rate
            wc_product.save()  → PUT /products/{id}
        - Sleep price_list_delay_per_item seconds
```

---

## 20. Key Gotchas & Implementation Nuances

### 20.1 The `created_by_sync` Flag

This flag on `item.flags` (transient, not persisted) is **critical** for preventing infinite sync loops. When the sync system modifies an ERPNext item, it sets this flag before `item.save()`. The `on_update` hook checks for it and bails out early, preventing the save from triggering another round of sync.

**Risk**: If the flag is not set correctly (e.g., due to a bug or direct `frappe.db` manipulation), infinite loops can occur.

### 20.2 Sync Hash Clearing

The sync hash is cleared by `clear_sync_hash_and_run_item_sync()` before each hook-triggered sync. This is necessary because:
- Without clearing, the comparison `woocommerce_date_modified == last_sync_hash` might short-circuit the sync.
- The ERPNext item was just modified, so we **want** to push changes to WooCommerce.
- Clearing the hash forces the `≠` check to pass and proceeds to the timestamp comparison.

### 20.3 Timestamp Comparison Precision

The sync direction decision (`WC newer` vs `ERPNext newer`) is based on comparing:
- `woocommerce_product.woocommerce_date_modified` (string from WC API, e.g., `"2024-01-15T10:30:00"`)
- `item.modified` (Frappe datetime)

This string comparison works because ISO 8601 timestamps sort lexicographically. However, **timezone differences** could potentially cause incorrect comparisons if the ERPNext server and WooCommerce server are in different timezones.

### 20.4 Price List Delay

The `price_list_delay_per_item` setting (default: 2 seconds) exists to avoid hitting WooCommerce API rate limits during bulk price syncs. For stores with thousands of products, the daily price sync can take a long time. A 2-second delay × 1000 products = ~33 minutes, which fits within the 3600-second (1 hour) timeout.

### 20.5 `db_update()` Sends Only Changed Fields

`WooCommerceResource.db_update()` leverages Frappe's built-in dirty field tracking to only send fields that actually changed in the PUT request. This is important because sending unchanged fields (especially complex ones like `attributes`) to WooCommerce can cause unintended side effects.

### 20.6 Multi-Server Independence

Each row in `item.woocommerce_servers` is processed independently. An item synced to Server A and Server B will have separate `woocommerce_id`, `woocommerce_last_sync_hash`, and `enabled` flags for each. A sync failure on one server does not affect the other.

### 20.7 Product Variations and `parent_id`

For variations, the WooCommerce API uses a different endpoint structure:
```
/products/{parent_id}/variations/{variation_id}
```

The `parent_id` must be determined by looking up the parent template item's `woocommerce_id`. If the parent has not yet been synced to WooCommerce (no `woocommerce_id`), variation creation will fail.

### 20.8 Images: First Image Only

Only the **first** image in the WooCommerce product's `images` array is synced to `Item.image`. Multiple images on a WooCommerce product do not map to ERPNext's single `image` field. When pushing from ERPNext to WooCommerce, only `Item.image` is synced (as a single image).

### 20.9 JSONPath Filter Expressions Require Deserialization

Because WooCommerce product fields like `meta_data` are stored as JSON strings in the virtual doctype, JSONPath filter expressions (e.g., `$.meta_data[?(@.key=='my_key')].value`) will only work correctly if the deserialization step runs first. The sync code always deserializes before JSONPath operations, but custom code that bypasses the sync layer would need to handle this manually.

### 20.10 Price Sync Does Not Read from WooCommerce

The `SynchroniseItemPrice` class never reads WooCommerce product prices to compare them before pushing. It relies entirely on the ERPNext Item Price data and pushes regardless of what's currently in WooCommerce. The only comparison is done at push time (loaded product vs. ERPNext rate) to avoid unnecessary API calls.

### 20.11 Hourly Sync Updates `wc_last_sync_date_items` Even on Partial Failure

`sync_woocommerce_products_modified_since()` updates `wc_last_sync_date_items` after enqueuing all jobs, not after they all complete. Individual job failures (logged via Frappe Error Log) do not roll back this timestamp. Products from a failed sync run won't be re-fetched unless they are modified again in WooCommerce.

---

## 21. File Location Reference

| Component | File Path |
|-----------|-----------|
| **Item Sync — Main Logic** | `woocommerce_fusion/tasks/sync_items.py` |
| **Item Price Sync** | `woocommerce_fusion/tasks/sync_item_prices.py` |
| **Stock Update** | `woocommerce_fusion/tasks/stock_update.py` |
| **WooCommerce API Base Class** | `woocommerce_fusion/woocommerce/woocommerce_api.py` |
| **WooCommerce Product Virtual DocType** | `woocommerce_fusion/woocommerce/doctype/woocommerce_product/` |
| **Item WooCommerce Server DocType** | `woocommerce_fusion/woocommerce/doctype/item_woocommerce_server/` |
| **WooCommerce Server DocType** | `woocommerce_fusion/woocommerce/doctype/woocommerce_server/` |
| **WooCommerce Integration Settings** | `woocommerce_fusion/woocommerce/doctype/woocommerce_integration_settings/` |
| **Hooks & Scheduling** | `woocommerce_fusion/hooks.py` |
| **Custom Fields Fixtures** | `woocommerce_fusion/fixtures/custom_field.json` |
| **Item Form UI Buttons** | `woocommerce_fusion/public/js/stock/item.js` |
| **API Request Logging Wrapper** | `woocommerce_fusion/tasks/utils.py` |
| **Custom Exceptions** | `woocommerce_fusion/exceptions.py` |

---

*Research compiled from source code analysis of woocommerce_fusion codebase. All line numbers reference the state of the code at the time of analysis.*
