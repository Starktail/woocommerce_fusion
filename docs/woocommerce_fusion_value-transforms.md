# Value Transforms

A *Value Transform* on a **WooCommerce Server** > *Items* > *Fields Mapping* row converts a value between its ERPNext and its WooCommerce representation.

**This page is for advanced users.** A transform is Python code that you deploy in your own app - see [Custom Fields Mapping](/woocommerce_fusion_items#custom-fields-mapping) for mapping fields that need no code.

## When you need one

On its own, a mapping row copies a value straight across - it does no reshaping. Add a transform when:

- the ERPNext field is a **child table**, which has to become a list of objects on WooCommerce. A transform is mandatory in this case, and the mapping row is rejected without one
- a relative file path has to become an absolute URL
- the two sides use different keys, units, or terms

## Registering a transform

A transform is a Python function deployed by an app and registered in that app's `hooks.py`:

```python
# my_app/hooks.py
woocommerce_item_field_transforms = {
    "my_product_documents": "my_app.woocommerce_transforms.product_documents",
}
```

The key is what appears in the *Value Transform* dropdown; the value is the dotted path to the function.

Only registered names can be selected. This is deliberate: a free-text dotted path would let anyone who can edit a **WooCommerce Server** call any function in any installed app, with arguments the sync controls.

After adding the hook, run `bench --site your-site clear-cache` and reload the **WooCommerce Server** form for the new name to appear in the dropdown.

## Writing a transform

The same function is called on both legs of the sync:

```python
from woocommerce_fusion.tasks.field_transforms import SKIP, TO_WOOCOMMERCE


def product_documents(value, *, direction, item, woocommerce_product, row):
    if direction != TO_WOOCOMMERCE:
        # ERPNext owns this field, so do not write anything back to the Item
        return SKIP

    # `value` is the ERPNext field value - a list of child rows for a Table field
    return [
        {"type": document.document_type, "url": frappe.utils.get_url(document.pdf_file)}
        for document in value or []
    ]
```

Both directions live in one function on purpose: the two legs have to agree on shape, and splitting them makes it easy to change one and forget the other.

### Arguments

All arguments after `value` are keyword-only.

| Argument              | Description                                                                                                                                          |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `value`               | The source value: the ERPNext field value outbound, the JSONPath match on the product inbound                                                          |
| `direction`           | `"to_woocommerce"` or `"to_erpnext"`                                                                                                                  |
| `item`                | The ERPNext **Item** document                                                                                                                          |
| `woocommerce_product` | The **WooCommerce Product**, with its JSON fields already deserialised. Read only. Its `name` is empty for a product that does not exist in WooCommerce yet |
| `row`                 | The Fields Mapping row, so the transform can read its own JSONPath instead of hardcoding it                                                            |

### Two rules

1. **The outbound value must match what WooCommerce echoes back, key for key.** It is compared against the product's current value to decide whether the product needs updating. If the shapes differ, that comparison never settles and every sync run PATCHes the product. Watch out for numbers WordPress returns as strings - carrying the existing value over is usually safer than hardcoding one.
2. **Return `SKIP` for a direction you do not want to handle.** Do not return `None` - that is a legitimate value and will be written to the target.

## Child tables

A child table field is only selectable as a mapping target once a transform is available, because ERPNext holds a list of child rows where WooCommerce holds a list of objects, and only your transform knows how the two line up.

Outbound, `value` is the list of child row documents, so read their fields as attributes (`document.document_type`). Return plain dicts.

Inbound, `value` is whatever the JSONPath selected on the product - typically a list of dicts. Return a list of dicts keyed by the child DocType's fieldnames, and the rows are replaced wholesale. Rows are compared on their value-bearing fields only, so returning the same content twice does not mark the **Item** as changed.

## Targets that do not exist yet

A filtered JSONPath such as `$.meta_data[?key='_my_key'].value` matches nothing at all until WordPress has written that meta row, because a filter can only select from what is already in the list.

Where the target is a filter on an equality test, the entry is created on the product and the value written to it - so a product that has never held the meta key still syncs. `[?key='_my_key']` creates `{"key": "_my_key"}`, and WooCommerce creates the meta row from that key and its value.

Anything else that matches nothing is still reported as an error, which is what keeps a mistyped JSONPath from silently syncing nothing.

## Troubleshooting

- An unregistered transform name is rejected when the **WooCommerce Server** is saved, so a hook that was removed or renamed surfaces at configuration time
- Errors raised inside a transform stop that Item's sync and are recorded under **Error Log**
- If a product is PATCHed on every sync run, the outbound value does not match what WooCommerce echoes back. Compare the payload in **WooCommerce Request Log** against the response from the run before it (*Enable WooCommerce Request Logs* needs to be turned on on **WooCommerce Server** > *Logs*)
