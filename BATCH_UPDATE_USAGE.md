# Batch Update & Queue System for WooCommerce Fusion

## Overview

This document describes the batch update functionality and intelligent queuing system that have been added to WooCommerce Fusion to dramatically improve performance when syncing items from ERPNext to WooCommerce.

## Problem Statement

Previously, when modifying multiple items in ERPNext that need to be synced to WooCommerce, each item would trigger a separate API call. For example:
- Modifying 50 items → 50 individual PUT requests to WooCommerce API
- This results in slow performance and increased API load
- Creates unnecessary strain on both systems

## Solution

We've implemented a two-tier solution:

1. **Batch API Support**: Core functionality to send multiple updates in a single API call
2. **Intelligent Queue System**: Automatically collects and batches changes for optimal performance

## Implementation Details

### 1. Core Batch Method (`WooCommerceResource.db_batch`)

Location: `woocommerce_fusion/woocommerce/woocommerce_api.py`

This is a class method added to `WooCommerceResource` that enables batch operations (create, update, delete) on WooCommerce resources using the WooCommerce REST API batch endpoint.

**Endpoint Used:** `/wp-json/wc/v3/{resource}/batch`

**Parameters:**
- `woocommerce_server`: Name of the WooCommerce Server
- `create`: List of dictionaries for new resources (optional)
- `update`: List of dictionaries for updating existing resources (optional, must include 'id')
- `delete`: List of resource IDs to delete (optional)

**Example Usage:**
```python
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import WooCommerceProduct

result = WooCommerceProduct.db_batch(
    woocommerce_server="site1.example.com",
    update=[
        {"id": 123, "name": "Updated Product 1", "regular_price": "19.99"},
        {"id": 456, "name": "Updated Product 2", "regular_price": "29.99"}
    ]
)
```

### 2. Batch Update Function for Items (`batch_update_woocommerce_products`)

Location: `woocommerce_fusion/tasks/sync_items.py`

This is a whitelisted function that can be called to batch update multiple WooCommerce products from ERPNext items.

**Parameters:**
- `item_codes`: List of ERPNext Item codes to sync (optional - if None, syncs all modified items)

**Functionality:**
1. Collects all items that need syncing
2. Groups updates by WooCommerce server
3. Prepares update data for each product (name, price, custom fields, etc.)
4. Sends batch requests to each WooCommerce server
5. Updates sync hashes for successfully synced items

**Example Usage:**

From Python/Server Script:
```python
from woocommerce_fusion.tasks.sync_items import batch_update_woocommerce_products

# Update specific items
result = batch_update_woocommerce_products(["ITEM-001", "ITEM-002", "ITEM-003"])

# Update all modified items
result = batch_update_woocommerce_products()
```

From Client/JavaScript:
```javascript
frappe.call({
    method: "woocommerce_fusion.tasks.sync_items.batch_update_woocommerce_products",
    args: {
        item_codes: ["ITEM-001", "ITEM-002", "ITEM-003"]
    },
    callback: function(r) {
        console.log("Batch update result:", r.message);
        console.log("Total items processed:", r.message.total_items_processed);
    }
});
```

**Return Value:**
```json
{
    "total_items_processed": 3,
    "servers": {
        "site1.example.com": {
            "success": true,
            "updated_count": 3,
            "items": [123, 456, 789]
        }
    },
    "items": [
        {"item_code": "ITEM-001", "woocommerce_id": 123, "server": "site1.example.com"},
        {"item_code": "ITEM-002", "woocommerce_id": 456, "server": "site1.example.com"},
        {"item_code": "ITEM-003", "woocommerce_id": 789, "server": "site1.example.com"}
    ]
}
```

## Performance Benefits

### Before (Individual Updates)
- 50 items = 100 API calls (50 fetches + 50 updates)
- Each call takes ~200-500ms
- Total time: 20-50 seconds

### After (Batch Updates with Bulk Fetch)
- 50 items = 2 API calls (1 bulk fetch + 1 batch update)
- Fetch: ~500-800ms, Batch update: ~500-800ms
- Total time: ~1-1.5 seconds

**Performance Improvement: ~20-50x faster**

### API Call Reduction
- **Before**: 2N calls (N fetches + N updates)
- **After**: 2 calls (1 fetch + 1 update) per server
- **Savings**: Up to 99% fewer API calls for large batches

## WooCommerce API Batch Endpoint Format

The batch endpoint accepts requests in the following format:

```json
{
    "create": [
        {
            "name": "New Product 1",
            "regular_price": "19.99",
            "description": "Product description"
        }
    ],
    "update": [
        {
            "id": 123,
            "name": "Updated Product Name",
            "regular_price": "29.99"
        },
        {
            "id": 456,
            "description": "Updated description"
        }
    ],
    "delete": [789, 790]
}
```

Response format:
```json
{
    "create": [
        {
            "id": 800,
            "name": "New Product 1",
            "date_modified": "2025-11-10T12:00:00"
        }
    ],
    "update": [
        {
            "id": 123,
            "name": "Updated Product Name",
            "date_modified": "2025-11-10T12:00:00"
        }
    ],
    "delete": [
        {
            "id": 789
        }
    ]
}
```

## Intelligent Queue System

### Overview

The queue system automatically collects item changes and processes them in optimized batches, eliminating the need for manual batch operations in most cases.

**Location:** `woocommerce_fusion/tasks/batch_queue.py`

### How It Works

1. **Item Hook Integration**: When an Item is saved, instead of immediate sync, it's added to a Redis-backed queue
   - **Auto-determines operation**: CREATE (no woocommerce_id) or UPDATE (has woocommerce_id)
2. **Automatic Batching**: The queue collects changes until either:
   - **100 items** are queued (WooCommerce API limit), OR
   - **10 seconds** have passed since the first item was queued
3. **Scheduled Processing**: A background job runs frequently (via "all" scheduler event) to check and process ready queues
4. **Immediate Processing**: When queue reaches 100 items, processing is triggered immediately
5. **Batch Execution**: Both creates and updates are sent in a single batch API call per server

### Queue Configuration

```python
# Default configuration in batch_queue.py
MAX_BATCH_SIZE = 100        # Maximum items per batch (WooCommerce limit)
MAX_WAIT_TIME_SECONDS = 10  # Maximum time before processing
```

### Queue Manager Class

```python
from woocommerce_fusion.tasks.batch_queue import WooCommerceBatchQueue

# Create queue for a specific server
queue = WooCommerceBatchQueue("site1.example.com")

# Add operations
queue.add_item_update("ITEM-001", woocommerce_id=123)
queue.add_item_create("ITEM-002", create_data={...})
queue.add_item_delete(woocommerce_id=456)

# Check queue status
size = queue._get_queue_size()  # Uses hash length
age = queue.get_queue_age_seconds()
should_process = queue.should_process()

# Process queue manually
from woocommerce_fusion.tasks.batch_queue import process_batch_queue
process_batch_queue("site1.example.com")
```

**Multi-Tenancy Support**: The queue system properly supports multi-tenant setups. All cache keys are automatically site-scoped by Frappe's cache wrapper, and queue size is calculated using hash length rather than a separate counter to ensure proper isolation.

### Monitoring Queue Status

**From Python/Server Script:**
```python
from woocommerce_fusion.tasks.batch_queue import get_queue_status

# Get status for all servers
all_status = get_queue_status()

# Get status for specific server
server_status = get_queue_status("site1.example.com")
```

**From JavaScript/Client:**
```javascript
frappe.call({
    method: "woocommerce_fusion.tasks.batch_queue.get_queue_status",
    args: { server: "site1.example.com" },
    callback: function(r) {
        console.log("Queue status:", r.message);
        // Shows: {size, age_seconds, should_process, operations}
    }
});
```

### Enabling/Disabling Queue System

The queue system is **enabled by default**. To use legacy immediate sync instead:

1. Open **WooCommerce Integration Settings**
2. Uncheck **Enable Batch Queue** (field: `enable_batch_queue`)
3. Save

When disabled, items will sync immediately as before (one API call per item).

### Scheduled Job

The queue processor is configured in `hooks.py`:

```python
scheduler_events = {
    "all": [
        "woocommerce_fusion.tasks.batch_queue.process_all_queues"
    ],
}
```

The "all" event runs frequently (typically every few seconds), ensuring:
- Queues are processed within 10 seconds maximum
- No changes are lost
- Optimal batching is maintained

## Use Cases

### Automatic (Queue-Based)
1. **Normal Item Updates**: User saves items in ERPNext → automatically queued and batched
2. **Import Operations**: Bulk imports automatically benefit from batching
3. **Field Updates**: Mass updates via Data Import or scripts get batched automatically

### Manual (Direct Batch API)
1. **Custom Scripts**: When you need explicit control over batching
2. **Migration Tasks**: One-time bulk updates during data migration
3. **Scheduled Syncs**: Periodic full catalog syncs
4. **API Integrations**: External systems triggering bulk updates

## Integration with Existing Code

The batch functionality is **complementary** to the existing one-by-one sync:

- **Queue System (Default)**: Automatically batches changes from item hooks
- **Manual Batch** (`batch_update_woocommerce_products`): Use for bulk operations when you have multiple items to sync
- **Legacy Sync** (when queue disabled): Original one-by-one sync for immediate updates

## Future Enhancements

Potential improvements for the future:
1. ✅ ~~Automatic batching in hooks when multiple items are saved in quick succession~~ (IMPLEMENTED)
2. ✅ ~~Queue-based batching system that collects updates over a time window~~ (IMPLEMENTED)
3. Batch support for other resources (orders, customers, categories, etc.)
4. Configurable batch size and wait time limits via Settings
5. Progress tracking UI for large batch operations
6. Queue monitoring dashboard
7. Failed operation retry mechanism
8. Queue persistence across system restarts
9. Multi-level priority queues (urgent vs. normal)

## Testing

### Testing the Queue System (Automatic)

1. **Enable the queue** (should be enabled by default):
   - Go to WooCommerce Integration Settings
   - Ensure "Enable Batch Queue" is checked

2. **Modify multiple items quickly**:
   ```python
   # In a server script or console
   for i in range(5):
       item = frappe.get_doc("Item", f"ITEM-00{i}")
       item.item_name = f"Updated Name {i}"
       item.save()
   ```

3. **Check queue status**:
   ```python
   from woocommerce_fusion.tasks.batch_queue import get_queue_status
   status = get_queue_status()
   print(status)
   # Should show 5 items queued
   ```

4. **Wait 10 seconds or trigger manually**:
   ```python
   from woocommerce_fusion.tasks.batch_queue import process_all_queues
   process_all_queues()
   ```

5. **Verify in WooCommerce**: Check that all 5 products are updated
6. **Check logs**: Look for batch processing logs in Error Log

### Testing Manual Batch API

1. Modify multiple items in ERPNext
2. Call `batch_update_woocommerce_products()` with the item codes:
   ```python
   from woocommerce_fusion.tasks.sync_items import batch_update_woocommerce_products
   result = batch_update_woocommerce_products(["ITEM-001", "ITEM-002", "ITEM-003"])
   print(result)
   ```
3. Verify that products are updated in WooCommerce
4. Check that sync hashes are updated correctly

### Testing Queue Monitoring

**Python:**
```python
from woocommerce_fusion.tasks.batch_queue import get_queue_status
status = get_queue_status("site1.example.com")
print(f"Queue size: {status['site1.example.com']['size']}")
print(f"Queue age: {status['site1.example.com']['age_seconds']} seconds")
```

**JavaScript Console:**
```javascript
frappe.call({
    method: "woocommerce_fusion.tasks.batch_queue.get_queue_status",
    callback: (r) => console.log(r.message)
});
```

## References

- [WooCommerce REST API Batch Documentation](https://woocommerce.github.io/woocommerce-rest-api-docs/#batch-update-products)
- [WooCommerce REST API Products](https://woocommerce.github.io/woocommerce-rest-api-docs/#products)
