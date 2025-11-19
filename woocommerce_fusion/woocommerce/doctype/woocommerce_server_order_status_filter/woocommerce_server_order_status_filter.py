# Copyright (c) 2025, Dirk van der Laarse and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class WooCommerceServerOrderStatusFilter(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		woocommerce_order_status: DF.Literal[
			"pending",
			"processing",
			"on-hold",
			"completed",
			"cancelled",
			"refunded",
			"failed",
			"trash",
		]
	# end: auto-generated types

	pass
