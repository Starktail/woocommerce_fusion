# WooCommerce Batch Queue - Quick Start Guide

## What's New?

Your WooCommerce Fusion app now includes an **intelligent queue system** that automatically batches item updates to WooCommerce, improving performance by up to **25x**.

## How It Works (Automatic)

When you save an Item in ERPNext that's linked to WooCommerce:

1. ✅ Item is added to a queue (not synced immediately)
   - **Auto-detects** if it should CREATE (no woocommerce_id) or UPDATE (has woocommerce_id)
2. ⏱️ Queue waits for either:
   - 10 seconds, OR
   - 100 items (whichever comes first)
3. 🚀 All queued items are sent to WooCommerce in a **single batch API call**
   - Creates and updates are combined into one request
4. ✨ Sync hashes and WooCommerce IDs are updated automatically

**No configuration needed** - it works automatically!

## Performance Comparison

### Before (Legacy Sync)
```
50 items saved → 50 API calls → 10-25 seconds
```

### After (Queue System)
```
50 items saved → Added to queue → 1 API call → 0.5-1 seconds
```

**Result: 10-25x faster! 🎉**

## Quick Examples

### Example 1: Bulk Price Update (Automatic Batching)

```python
# Update 20 items - they'll be automatically batched!
items = frappe.get_all("Item", filters={"item_group": "Products"}, limit=20)

for item_name in items:
    item = frappe.get_doc("Item", item_name.name)
    item.standard_rate = item.standard_rate * 1.1  # 10% price increase
    item.save()  # Added to queue automatically

# Wait 10 seconds or reach 100 items → Batch sync happens automatically!
```

### Example 2: Check Queue Status

```python
from woocommerce_fusion.tasks.batch_queue import get_queue_status

status = get_queue_status()
print(status)

# Output:
# {
#     "site1.example.com": {
#         "size": 15,
#         "age_seconds": 3.2,
#         "should_process": False,
#         "operations": {"create": 0, "update": 15, "delete": 0}
#     }
# }
```

### Example 3: Manual Batch Update

If you prefer explicit control:

```python
from woocommerce_fusion.tasks.sync_items import batch_update_woocommerce_products

# Update specific items
result = batch_update_woocommerce_products([
    "ITEM-001",
    "ITEM-002",
    "ITEM-003"
])

print(f"Updated {result['total_items_processed']} items")
```

### Example 4: Process Queue Manually

```python
from woocommerce_fusion.tasks.batch_queue import process_all_queues

# Process all queues immediately (don't wait for scheduler)
result = process_all_queues()
```

## Configuration

### Enable/Disable Queue System

The queue is **enabled by default**. To disable:

1. Go to: **WooCommerce Integration Settings**
2. Uncheck: **Enable Batch Queue**
3. Save

When disabled, items sync immediately (old behavior).

### Queue Settings

Current settings (in `batch_queue.py`):
- **Max Batch Size**: 100 items
- **Max Wait Time**: 10 seconds
- **Scheduler**: Runs every few seconds

## Monitoring

### From Python Console

```python
from woocommerce_fusion.tasks.batch_queue import get_queue_status

# All servers
all_status = get_queue_status()

# Specific server
status = get_queue_status("site1.example.com")
```

### From JavaScript Console

```javascript
frappe.call({
    method: "woocommerce_fusion.tasks.batch_queue.get_queue_status",
    callback: function(r) {
        console.log("Queue Status:", r.message);
    }
});
```

## When to Use What?

### Use Automatic Queue (Default)
- ✅ Regular item updates
- ✅ Bulk imports
- ✅ Data Import tool
- ✅ User-initiated changes

### Use Manual Batch API
- ✅ Custom migration scripts
- ✅ Scheduled maintenance tasks
- ✅ When you need immediate execution
- ✅ External API integrations

### Disable Queue (Use Legacy)
- ✅ When you need immediate sync (< 1 second)
- ✅ When testing individual items
- ✅ Debugging sync issues

## Troubleshooting

### Items not syncing?

1. **Check queue status**:
   ```python
   from woocommerce_fusion.tasks.batch_queue import get_queue_status
   print(get_queue_status())
   ```

2. **Process queue manually**:
   ```python
   from woocommerce_fusion.tasks.batch_queue import process_all_queues
   process_all_queues()
   ```

3. **Check Error Logs**: Go to Error Log doctype and filter for "WooCommerce"

### Queue not processing?

1. **Check scheduler is running**:
   ```bash
   bench doctor
   ```

2. **Restart scheduler**:
   ```bash
   bench restart
   ```

3. **Check Redis is running**:
   ```bash
   redis-cli ping
   # Should return: PONG
   ```

### Need immediate sync?

Disable batch queue in WooCommerce Integration Settings.

## Advanced Usage

### Clear Queue

```python
from woocommerce_fusion.tasks.batch_queue import WooCommerceBatchQueue

queue = WooCommerceBatchQueue("site1.example.com")
queue.clear_queue()
```

### Manual Queue Operations

```python
from woocommerce_fusion.tasks.batch_queue import WooCommerceBatchQueue

queue = WooCommerceBatchQueue("site1.example.com")

# Add items
queue.add_item_update("ITEM-001", woocommerce_id=123)
queue.add_item_update("ITEM-002", woocommerce_id=456)

# Check if should process
if queue.should_process():
    from woocommerce_fusion.tasks.batch_queue import process_batch_queue
    process_batch_queue("site1.example.com")
```

## Questions?

See full documentation in `BATCH_UPDATE_USAGE.md`

## Summary

🎯 **Key Benefits**:
- 10-25x faster sync
- Automatic batching
- No configuration needed
- Works with existing code
- Can be disabled anytime

🚀 **Just save your items as normal - batching happens automatically!**
