# Copyright (c) 2025, Dirk van der Laarse and contributors
# For license information, please see license.txt
"""
Optional value transforms for `WooCommerce Server` > Items > Fields Mapping.

The built-in field mapping can only move scalars: it copies one ERPNext Item field value
straight into one JSONPath location on the WooCommerce Product, and back. Anything that needs
restructuring - a child table becoming a list of dicts, a relative `/files/x.pdf` path becoming
an absolute URL, a Select value becoming a WooCommerce term - needs a transform.

A transform is a plain Python callable, deployed by an app and registered in that app's
`hooks.py`::

    # my_app/hooks.py
    woocommerce_item_field_transforms = {
        "my_product_documents": "my_app.woocommerce_transforms.product_documents",
    }

Only registered names may be selected in the row's "Value Transform" field.
The callable is invoked on both legs of the sync.

Arguments, in both directions:

``item``
    The ERPNext `Item` document. On the inbound leg it is the document about to be written to, so
    its current values are the pre-sync ones.
``woocommerce_product``
    The WooCommerce Product with its JSON fields (``meta_data``, ``images``, ...) already
    deserialised into Python objects. Read it, do not mutate it - the sync writes the returned value
    into the mapped JSONPath location itself. Its ``name`` is empty for a product that has not been
    created in WooCommerce yet.
``row``
    The `WooCommerce Server Item Field` row being processed, so a transform can read its own
    ``woocommerce_field_name`` JSONPath rather than hardcoding it.

Both directions live in one callable on purpose. The outbound value is compared against what
WooCommerce currently holds to decide whether the product is dirty, so the two legs have to agree
on shape.

Two rules for transform authors:

1. The outbound value must match the structure WooCommerce echoes back, key for key. If it does
   not, the comparison in `set_product_fields` never settles and every sync run PATCHes the
   product.
2. Return `SKIP` for a direction you do not want to handle. Do not return `None` - that is a
   legitimate value and will be written to the target.
"""

from collections.abc import Callable

import frappe
from frappe import _

HOOK = "woocommerce_item_field_transforms"

TO_ERPNEXT = "to_erpnext"
TO_WOOCOMMERCE = "to_woocommerce"


class SkipTransform:
	"""Sentinel telling the sync to leave the target field untouched for this direction."""

	def __repr__(self):
		return "SKIP"


SKIP = SkipTransform()


def get_registered_transforms() -> dict[str, str]:
	"""
	Return {transform name: dotted path} for every transform registered by an installed app.
	"""
	hooks = frappe.get_hooks(HOOK) or {}

	# Frappe listifies the values of dict-shaped hooks across apps. Where two apps register the
	# same name, the last app to be loaded wins, matching Frappe's convention for hook overrides.
	return {
		name: (paths[-1] if isinstance(paths, list | tuple) else paths)
		for name, paths in hooks.items()
		if paths
	}


def resolve_transform(name: str) -> Callable:
	"""
	Resolve a registered transform name to its callable, or throw if it is not registered.
	"""
	registered = get_registered_transforms()
	if name not in registered:
		frappe.throw(
			_(
				"Value Transform <code>{0}</code> is not registered by any installed app. "
				"Register it in an app's <code>hooks.py</code> under "
				"<code>woocommerce_item_field_transforms</code>."
			).format(name),
			title=_("Unknown Value Transform"),
		)

	return frappe.get_attr(registered[name])


def apply_transform(row, value, *, direction: str, item, woocommerce_product):
	"""
	Run the row's transform over `value`, if one is configured.

	Returns the transformed value, `SKIP` if the transform declines this direction, or `value`
	unchanged when the row has no transform.
	"""
	transform_name = row.get("value_transform_method")
	if not transform_name:
		return value

	transform = resolve_transform(transform_name)

	return transform(
		value,
		direction=direction,
		item=item,
		woocommerce_product=woocommerce_product,
		row=row,
	)
