import json
from datetime import datetime
from typing import Dict, Optional, Tuple, Union

import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.selling.doctype.sales_order_item.sales_order_item import SalesOrderItem
from frappe import _
from frappe.utils import get_datetime
from frappe.utils.data import cstr, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError, WooCommerceOrderNotFoundError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.sync_items import run_item_sync
from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_STATUS_MAPPING,
	WC_ORDER_STATUS_MAPPING_REVERSE,
	WooCommerceOrder,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def run_sales_order_sync_from_hook(doc, method):
	if (
		doc.doctype == "Sales Order"
		and not doc.flags.get("created_by_sync", None)
		and doc.woocommerce_server
	):
		frappe.enqueue(run_sales_order_sync, queue="long", sales_order_name=doc.name)


@frappe.whitelist()
def run_sales_order_sync(
	sales_order_name: Optional[str] = None,
	sales_order: Optional[SalesOrder] = None,
	woocommerce_order_name: Optional[str] = None,
	woocommerce_order: Optional[WooCommerceOrder] = None,
	enqueue=False,
):
	"""
	Helper funtion that prepares arguments for order sync
	"""
	# Validate inputs, at least one of the parameters should be provided
	if not any([sales_order_name, sales_order, woocommerce_order_name, woocommerce_order]):
		raise ValueError(
			"At least one of sales_order_name, sales_order, woocommerce_order_name, woocommerce_order is required"
		)

	# Get ERPNext Sales Order and WooCommerce Order if they exist
	if woocommerce_order or woocommerce_order_name:
		if not woocommerce_order:
			woocommerce_order = frappe.get_doc(
				{"doctype": "WooCommerce Order", "name": woocommerce_order_name}
			)
			woocommerce_order.load_from_db()

		# Trigger sync
		sync = SynchroniseSalesOrder(woocommerce_order=woocommerce_order)
		if enqueue:
			frappe.enqueue(sync.run)
		else:
			sync.run()

	elif sales_order_name or sales_order:
		if not sales_order:
			sales_order = frappe.get_doc("Sales Order", sales_order_name)
		if not sales_order.woocommerce_server:
			frappe.throw(_("No WooCommerce Server defined for Sales Order {0}").format(sales_order_name))
		# Trigger sync for every linked server
		sync = SynchroniseSalesOrder(sales_order=sales_order)
		if enqueue:
			frappe.enqueue(sync.run)
		else:
			sync.run()

	return (
		sync.sales_order if sync else None,
		sync.woocommerce_order if sync else None,
	)


def sync_woocommerce_orders_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce orders modified since date_time_from
	"""
	wc_settings = frappe.get_doc("WooCommerce Integration Settings")

	if not date_time_from:
		date_time_from = wc_settings.wc_last_sync_date

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

	wc_orders = get_list_of_wc_orders(date_time_from=date_time_from)
	wc_orders += get_list_of_wc_orders(date_time_from=date_time_from, status="trash")
	for wc_order in wc_orders:
		try:
			run_sales_order_sync(woocommerce_order=wc_order, enqueue=True)
		# Skip orders with errors, as these exceptions will be logged
		except Exception:
			pass

	frappe.db.set_single_value("WooCommerce Settings", "wc_last_sync_date_items", now())


class SynchroniseSalesOrder(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of a WooCommerce Order with an ERPNext Sales Order
	"""

	def __init__(
		self,
		sales_order: Optional[SalesOrder] = None,
		woocommerce_order: Optional[WooCommerceOrder] = None,
	) -> None:
		super().__init__()
		self.sales_order = sales_order
		self.woocommerce_order = woocommerce_order
		self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")

	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_sales_order_or_woocommerce_order()
			self.sync_wc_order_with_erpnext_order()
		except Exception as err:
			error_message = f"{frappe.get_traceback()}\n\nSales Order Data: \n{str(self.sales_order.as_dict()) if self.sales_order else ''}\n\nWC Product Data \n{str(self.woocommerce_order.as_dict()) if self.woocommerce_order else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_sales_order_or_woocommerce_order(self):
		"""
		If we have an ERPNext Sales Order, get the corresponding WooCommerce Order
		If we have a WooCommerce Order, get the corresponding ERPNext Sales Order

		Assumes that both exist, and that the Sales Order is linked to the WooCommerce Order
		"""
		if self.sales_order and not self.woocommerce_order and self.sales_order.woocommerce_id:
			# Validate that this Sales Order's WooCommerce Server has sync enabled
			wc_server = frappe.get_cached_doc("WooCommerce Server", self.sales_order.woocommerce_server)
			if not wc_server.enable_sync:
				raise SyncDisabledError(wc_server)

			wc_orders = get_list_of_wc_orders(sales_order=self.sales_order)

			# If we can't find a linked WooCommerce Order (it may have been deleted), we can't proceed
			if len(wc_orders) == 0:
				raise WooCommerceOrderNotFoundError(self.sales_order)

			self.woocommerce_order = wc_orders[0]

		if self.woocommerce_order and not self.sales_order:
			self.get_erpnext_sales_order()

	def get_erpnext_sales_order(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		filters = [
			["Sales Order", "woocommerce_id", "is", "set"],
			["Sales Order", "woocommerce_server", "is", "set"],
		]
		filters.append(["Sales Order", "woocommerce_id", "=", self.woocommerce_order.id])
		filters.append(
			[
				"Sales Order",
				"woocommerce_server",
				"=",
				self.woocommerce_order.woocommerce_server,
			]
		)

		sales_orders = frappe.get_all(
			"Sales Order",
			filters=filters,
			fields=["name"],
		)
		if len(sales_orders) > 0:
			self.sales_order = frappe.get_doc("Sales Order", sales_orders[0].name)

	def sync_wc_order_with_erpnext_order(self):
		"""
		Syncronise Sales Order between ERPNext and WooCommerce
		"""
		if self.sales_order and not self.woocommerce_order:
			# create missing order in WooCommerce
			pass
		elif self.woocommerce_order and not self.sales_order:
			# create missing order in ERPNext
			self.create_sales_order(self.woocommerce_order)
		elif self.sales_order and self.woocommerce_order:
			# both exist, check sync hash
			if (
				self.woocommerce_order.woocommerce_date_modified
				!= self.sales_order.custom_woocommerce_last_sync_hash
			):
				if get_datetime(self.woocommerce_order.woocommerce_date_modified) > get_datetime(
					self.sales_order.modified
				):
					self.update_sales_order(self.woocommerce_order, self.sales_order)
				if get_datetime(self.woocommerce_order.woocommerce_date_modified) < get_datetime(
					self.sales_order.modified
				):
					self.update_woocommerce_order(self.woocommerce_order, self.sales_order)

			# If the Sales Order exists and has been submitted in the mean time, sync Payment Entries
			if (
				self.sales_order.docstatus == 1
				and not self.sales_order.woocommerce_payment_entry
				and not self.sales_order.custom_attempted_woocommerce_auto_payment_entry
			):
				self.sales_order.reload()
				if self.create_and_link_payment_entry(self.woocommerce_order, self.sales_order):
					self.sales_order.save()

	def update_sales_order(self, woocommerce_order: WooCommerceOrder, sales_order: SalesOrder):
		"""
		Update the ERPNext Sales Order with fields from it's corresponding WooCommerce Order
		"""
		# Ignore cancelled Sales Orders
		if sales_order.docstatus != 2:
			so_dirty = False

			# Update the woocommerce_status field if necessary
			wc_order_status = WC_ORDER_STATUS_MAPPING_REVERSE[woocommerce_order.status]
			if sales_order.woocommerce_status != wc_order_status:
				sales_order.woocommerce_status = wc_order_status
				so_dirty = True

			if sales_order.custom_woocommerce_customer_note != woocommerce_order.customer_note:
				sales_order.custom_woocommerce_customer_note = woocommerce_order.customer_note

			# Update the payment_method_title field if necessary, use the payment method ID
			# if the title field is too long
			payment_method = (
				woocommerce_order.payment_method_title
				if len(woocommerce_order.payment_method_title) < 140
				else woocommerce_order.payment_method
			)
			if sales_order.woocommerce_payment_method != payment_method:
				sales_order.woocommerce_payment_method = payment_method
				so_dirty = True

			if not sales_order.woocommerce_payment_entry:
				if self.create_and_link_payment_entry(woocommerce_order, sales_order):
					so_dirty = True

			if so_dirty:
				sales_order.flags.created_by_sync = True
				sales_order.save()

	def create_and_link_payment_entry(
		self, wc_order: WooCommerceOrder, sales_order: SalesOrder
	) -> bool:
		"""
		Create a Payment Entry for a WooCommerce Order that has been marked as Paid
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", sales_order.woocommerce_server)
		if not wc_server:
			raise ValueError("Could not find woocommerce_server in list of servers")

		# Validate that WooCommerce order has been paid, and that sales order doesn't have a linked Payment Entry yet
		if (
			wc_server.enable_payments_sync
			and wc_order.payment_method
			and ((wc_server.ignore_date_paid) or (not wc_server.ignore_date_paid and wc_order.date_paid))
			and not sales_order.woocommerce_payment_entry
			and sales_order.docstatus == 1
		):
			# If the grand total is 0, skip payment entry creation
			if sales_order.grand_total == None or float(sales_order.grand_total) == 0:
				return True

			# Get Company Bank Account for this Payment Method
			payment_method_bank_account_mapping = json.loads(wc_server.payment_method_bank_account_mapping)

			if wc_order.payment_method not in payment_method_bank_account_mapping:
				raise KeyError(
					f"WooCommerce payment method {wc_order.payment_method} not found in WooCommerce Server"
				)

			company_bank_account = payment_method_bank_account_mapping[wc_order.payment_method]

			if company_bank_account:
				# Get G/L Account for this Payment Method
				payment_method_gl_account_mapping = json.loads(wc_server.payment_method_gl_account_mapping)
				company_gl_account = payment_method_gl_account_mapping[wc_order.payment_method]

				# Create a new Payment Entry
				company = frappe.get_value("Account", company_gl_account, "company")
				meta_data = wc_order.get("meta_data", None)

				# Attempt to get Payfast Transaction ID
				payment_reference_no = wc_order.get("transaction_id", None)

				# Attempt to get Yoco Transaction ID
				if not payment_reference_no:
					payment_reference_no = (
						next(
							(data["value"] for data in meta_data if data["key"] == "yoco_order_payment_id"),
							None,
						)
						if meta_data and type(meta_data) is list
						else None
					)

				# Determine if the reference should be Sales Order or Sales Invoice
				reference_doctype = "Sales Order"
				reference_name = sales_order.name
				total_amount = sales_order.grand_total
				if sales_order.per_billed > 0:
					si_item_details = frappe.get_all(
						"Sales Invoice Item",
						fields=["name", "parent"],
						filters={"sales_order": sales_order.name},
					)
					if len(si_item_details) > 0:
						reference_doctype = "Sales Invoice"
						reference_name = si_item_details[0].parent
						total_amount = sales_order.grand_total

				# Create Payment Entry
				payment_entry_dict = {
					"company": company,
					"payment_type": "Receive",
					"reference_no": payment_reference_no or wc_order.payment_method_title,
					"reference_date": wc_order.date_paid or sales_order.transaction_date,
					"party_type": "Customer",
					"party": sales_order.customer,
					"posting_date": wc_order.date_paid or sales_order.transaction_date,
					"paid_amount": float(wc_order.total),
					"received_amount": float(wc_order.total),
					"bank_account": company_bank_account,
					"paid_to": company_gl_account,
				}
				payment_entry = frappe.new_doc("Payment Entry")
				payment_entry.update(payment_entry_dict)
				row = payment_entry.append("references")
				row.reference_doctype = reference_doctype
				row.reference_name = reference_name
				row.total_amount = total_amount
				row.allocated_amount = total_amount
				payment_entry.save()

				# Link created Payment Entry to Sales Order
				sales_order.woocommerce_payment_entry = payment_entry.name

			sales_order.custom_attempted_woocommerce_auto_payment_entry = 1
			return True

	def update_woocommerce_order(self, wc_order: WooCommerceOrder, sales_order: SalesOrder) -> None:
		"""
		Update the WooCommerce Order with fields from it's corresponding ERPNext Sales Order
		"""
		wc_order_dirty = False

		# Update the woocommerce_status field if necessary
		sales_order_wc_status = (
			WC_ORDER_STATUS_MAPPING[sales_order.woocommerce_status]
			if sales_order.woocommerce_status
			else None
		)
		if sales_order_wc_status != wc_order.status:
			wc_order.status = sales_order_wc_status
			wc_order_dirty = True

		# Get the Item WooCommerce ID's
		for so_item in sales_order.items:
			so_item.woocommerce_id = frappe.get_value(
				"Item WooCommerce Server",
				filters={"parent": so_item.item_code, "woocommerce_server": wc_order.woocommerce_server},
				fieldname="woocommerce_id",
			)

		# Update the line_items field if necessary
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_order.woocommerce_server)
		if wc_server.sync_so_items_to_wc:
			sales_order_items_changed = False
			line_items = json.loads(wc_order.line_items)
			# Check if count of line items are different
			if len(line_items) != len(sales_order.items):
				sales_order_items_changed = True
			# Check if any line item properties changed
			else:
				for i, so_item in enumerate(sales_order.items):
					if not so_item.woocommerce_id:
						break
					elif (
						int(so_item.woocommerce_id) != line_items[i]["product_id"]
						or so_item.qty != line_items[i]["quantity"]
						or so_item.rate != get_tax_inc_price_for_woocommerce_line_item(line_items[i])
					):
						sales_order_items_changed = True
						break

			if sales_order_items_changed:
				# Build the correct lines
				new_line_items = [
					{
						"product_id": so_item.woocommerce_id,
						"quantity": so_item.qty,
						"price": so_item.rate,
						"meta_data": line_items[i].get("meta_data", []) if i < len(line_items) else [],
					}
					for i, so_item in enumerate(sales_order.items)
				]
				# Process Order Item Line Field Mappings
				for i, line_item in enumerate(new_line_items):
					self.set_wc_order_line_items_mapped_fields(line_item, sales_order.items[i])

				# Set the product_id for existing lines to null, to clear the line items for the WooCommerce order
				replacement_line_items = [
					{"id": line_item["id"], "product_id": None} for line_item in json.loads(wc_order.line_items)
				]
				replacement_line_items.extend(new_line_items)

				wc_order.line_items = json.dumps(replacement_line_items)
				wc_order_dirty = True

		if wc_order_dirty:
			wc_order.save()

	def set_wc_order_line_items_mapped_fields(
		self, woocommerce_order_line_item: Dict, so_item: SalesOrderItem
	) -> Tuple[bool, WooCommerceOrder]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to set their values from
		ERPNext to WooCommerce
		"""
		wc_line_item_dirty = False
		if woocommerce_order_line_item and self.sales_order and self.woocommerce_order:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_order.woocommerce_server
			)
			if wc_server.order_line_item_field_map:
				for map in wc_server.order_line_item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")
					erpnext_item_field_value = getattr(so_item, erpnext_item_field_name[0])

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_order_line_field_matches = jsonpath_expr.find(woocommerce_order_line_item)

					if len(woocommerce_order_line_field_matches) == 0:
						if self.woocommerce_order.name:
							# The field should exist, else raise an error
							raise ValueError(
								_("Field <code>{0}</code> not found in Item Line of WooCommerce Order {1}").format(
									map.woocommerce_field_name, self.woocommerce_order.name
								)
							)

					# JSONPath parsing typically returns a list, we'll only take the first value
					woocommerce_order_line_field_value = woocommerce_order_line_field_matches[0].value

					if erpnext_item_field_value != woocommerce_order_line_field_value:
						jsonpath_expr.update(woocommerce_order_line_item, erpnext_item_field_value)
						wc_line_item_dirty = True

		return wc_line_item_dirty, woocommerce_order_line_item

	def create_sales_order(self, wc_order: WooCommerceOrder) -> None:
		"""
		Create an ERPNext Sales Order from the given WooCommerce Order
		"""
		customer_docname = self.create_or_link_customer_and_address(wc_order)
		self.create_missing_items(wc_order, json.loads(wc_order.line_items), wc_order.woocommerce_server)

		new_sales_order = frappe.new_doc("Sales Order")
		self.sales_order = new_sales_order
		new_sales_order.customer = customer_docname
		new_sales_order.po_no = new_sales_order.woocommerce_id = wc_order.id
		new_sales_order.custom_woocommerce_customer_note = wc_order.customer_note

		new_sales_order.woocommerce_status = WC_ORDER_STATUS_MAPPING_REVERSE[wc_order.status]
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_order.woocommerce_server)

		new_sales_order.woocommerce_server = wc_order.woocommerce_server
		# Set the payment_method_title field if necessary, use the payment method ID if the title field is too long
		payment_method = (
			wc_order.payment_method_title
			if len(wc_order.payment_method_title) < 140
			else wc_order.payment_method
		)
		new_sales_order.woocommerce_payment_method = payment_method
		created_date = wc_order.date_created.split("T")
		new_sales_order.transaction_date = created_date[0]
		delivery_after = wc_server.delivery_after_days or 7
		new_sales_order.delivery_date = frappe.utils.add_days(created_date[0], delivery_after)
		new_sales_order.company = wc_server.company
		new_sales_order.currency = wc_order.currency

		if (
			(wc_server.enable_shipping_methods_sync)
			and (shipping_lines := json.loads(wc_order.shipping_lines))
			and len(wc_server.shipping_rule_map) > 0
		):
			if len(wc_order.shipping_lines) > 0:
				shipping_rule_mapping = next(
					(
						rule
						for rule in wc_server.shipping_rule_map
						if rule.wc_shipping_method_id == shipping_lines[0]["method_title"]
					),
					None,
				)
				new_sales_order.shipping_rule = shipping_rule_mapping.shipping_rule

		self.set_items_in_sales_order(new_sales_order, wc_order)
		self.set_fee_lines_in_sales_order(new_sales_order, wc_order)
		new_sales_order.flags.ignore_mandatory = True
		new_sales_order.flags.created_by_sync = True
		new_sales_order.insert()
		if wc_server.submit_sales_orders:
			new_sales_order.submit()

		new_sales_order.reload()
		self.create_and_link_payment_entry(wc_order, new_sales_order)
		new_sales_order.save()

	def create_or_link_customer_and_address(self, wc_order: WooCommerceOrder) -> str:
		"""
		Create or update Customer and Address records, with special handling for guest orders using order ID.
		"""
		raw_billing_data = json.loads(wc_order.billing)
		raw_shipping_data = json.loads(wc_order.shipping)
		first_name = raw_billing_data.get("first_name", "").strip()
		last_name = raw_billing_data.get("last_name", "").strip()
		email = raw_billing_data.get("email", "").strip()
		company_name = raw_billing_data.get("company", "").strip()
		individual_name = f"{first_name} {last_name}".strip() or email

		# Determine if the order is from a guest user
		is_guest = wc_order.customer_id is None or wc_order.customer_id == 0

		# Use the WooCommerce order ID as the identifier for guest orders
		order_id = wc_order.id

		customer_woo_com_email = raw_billing_data.get("email")
		if not customer_woo_com_email and not is_guest:
			# Log raw_billing_data
			frappe.log_error(
				"WooCommerce Error",
				f"Email is required to create or link a customer. \n\nCustomer Data: {raw_billing_data}",
			)
			return None

		# Use order ID for guest users, otherwise use email
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_order.woocommerce_server)
		if is_guest:
			customer_identifier = f"Guest-{order_id}"
		elif company_name and wc_server.enable_dual_accounts:
			customer_identifier = f"{customer_woo_com_email}-{company_name}"
		else:
			customer_identifier = customer_woo_com_email

		# Check if customer exists using the identifier

		existing_customer = frappe.get_value(
			"Customer", {"woocommerce_identifier": customer_identifier}, "name"
		)

		if not existing_customer:
			# Create Customer
			customer = frappe.new_doc("Customer")
			customer.woocommerce_identifier = customer_identifier
			customer.customer_type = "Company" if company_name else "Individual"
			customer.woocommerce_is_guest = is_guest
		else:
			# Edit Customer
			customer = frappe.get_doc("Customer", existing_customer)

		customer.customer_name = company_name if company_name else individual_name
		customer.woocommerce_identifier = customer_identifier

		# Check if vat_id exists in raw_billing_data and is a valid string
		vat_id = raw_billing_data.get("vat_id")

		if isinstance(vat_id, str) and vat_id.strip():
			customer.tax_id = vat_id

		customer.flags.ignore_mandatory = True

		try:
			customer.save()
		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nCustomer Data{str(customer.as_dict())}"
			frappe.log_error("WooCommerce Error", error_message)
		finally:
			self.customer = customer

		self.create_or_update_address(wc_order)
		contact = create_contact(raw_billing_data, self.customer)
		self.customer.reload()
		self.customer.customer_primary_contact = contact.name
		try:
			self.customer.save()
		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nCustomer Data{str(customer.as_dict())}"
			frappe.log_error("WooCommerce Error", error_message)

		return customer.name

	def create_missing_items(self, wc_order, items_list, woocommerce_site):
		"""
		Searching for items linked to multiple WooCommerce sites
		"""
		for item_data in items_list:
			item_woo_com_id = cstr(item_data.get("variation_id") or item_data.get("product_id"))

			# Deleted items will have a "0" for variation_id/product_id
			if item_woo_com_id != "0":
				woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
					woocommerce_site, item_woo_com_id
				)
				run_item_sync(woocommerce_product_name=woocommerce_product_name)

	def set_items_in_sales_order(self, new_sales_order, wc_order):
		"""
		Customised version of set_items_in_sales_order to allow searching for items linked to
		multiple WooCommerce sites
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", new_sales_order.woocommerce_server)
		if not wc_server.warehouse:
			frappe.throw(_("Please set Warehouse in WooCommerce Server"))

		for item in json.loads(wc_order.line_items):
			woocomm_item_id = item.get("variation_id") or item.get("product_id")

			# Deleted items will have a "0" for variation_id/product_id
			if woocomm_item_id == 0:
				found_item = create_placeholder_item(new_sales_order)
			else:
				iws = frappe.qb.DocType("Item WooCommerce Server")
				itm = frappe.qb.DocType("Item")
				item_codes = (
					frappe.qb.from_(iws)
					.join(itm)
					.on(iws.parent == itm.name)
					.where(
						(iws.woocommerce_id == cstr(woocomm_item_id))
						& (iws.woocommerce_server == new_sales_order.woocommerce_server)
						& (itm.disabled == 0)
					)
					.select(iws.parent)
					.limit(1)
				).run(as_dict=True)

				found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None

			rate = item.get("price")
			# If we are applying a Sales Taxes and Charges Template (as opposed to Actual Tax), then we need to
			# determine if the item price should include tax or not
			if wc_server.enable_tax_lines_sync and not wc_server.use_actual_tax_type:
				tax_template = frappe.get_cached_doc(
					"Sales Taxes and Charges Template", wc_server.sales_taxes_and_charges_template
				)
				if tax_template.taxes[0].included_in_print_rate:
					rate = get_tax_inc_price_for_woocommerce_line_item(item)

			new_sales_order_line = {
				"item_code": found_item.name,
				"item_name": found_item.item_name,
				"description": found_item.item_name,
				"delivery_date": new_sales_order.delivery_date,
				"qty": item.get("quantity"),
				"rate": rate,
				"warehouse": wc_server.warehouse,
				"discount_percentage": 100 if item.get("price") == 0 else 0,
			}

			# Process Order Item Line Field Mappings
			self.set_sales_order_item_fields(woocommerce_order_line_item=item, so_item=new_sales_order_line)

			new_sales_order.append(
				"items",
				new_sales_order_line,
			)

			if wc_server.enable_tax_lines_sync:
				if not wc_server.use_actual_tax_type:
					new_sales_order.taxes_and_charges = wc_server.sales_taxes_and_charges_template

					# Trigger taxes calculation
					new_sales_order.set_missing_lead_customer_details()
				else:
					ordered_items_tax = item.get("total_tax")
					add_tax_details(new_sales_order, ordered_items_tax, "Ordered Item tax", wc_server.tax_account)

		# If a Shipping Rule is added, shipping charges will be determined by the Shipping Rule. If not, then
		# get it from the WooCommerce Order
		if not new_sales_order.shipping_rule:
			add_tax_details(
				new_sales_order, wc_order.shipping_tax, "Shipping Tax", wc_server.f_n_f_tax_account
			)
			add_tax_details(
				new_sales_order,
				wc_order.shipping_total,
				"Shipping Total",
				wc_server.f_n_f_account,
			)

		# Handle scenario where Woo Order has no items, then manually set the total
		if len(new_sales_order.items) == 0:
			new_sales_order.base_grand_total = float(wc_order.total)
			new_sales_order.grand_total = float(wc_order.total)
			new_sales_order.base_rounded_total = float(wc_order.total)
			new_sales_order.rounded_total = float(wc_order.total)

	def set_fee_lines_in_sales_order(self, new_sales_order, wc_order):
		"""
		If enabled, Synchronise Fee Lines from Woo Order to ERPNext Sales Order
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", new_sales_order.woocommerce_server)
		if wc_server.enable_order_fees_sync:
			if not wc_server.account_for_order_fee_lines:
				frappe.throw(_("Please set 'Account for Order Fee Lines' in WooCommerce Server"))
			if not wc_server.account_for_negative_order_fee_lines:
				frappe.throw(_("Please set 'Account for Negative Order Fee Lines' in WooCommerce Server"))
			if not wc_order.fee_lines:
				return
			for fee_line in json.loads(wc_order.fee_lines):

				# Add line for fee in Taxes and Charges table
				new_sales_order.append(
					"taxes",
					{
						"charge_type": "Actual",
						"account_head": wc_server.account_for_order_fee_lines
						if float(fee_line["total"]) > 0
						else wc_server.account_for_negative_order_fee_lines,
						"tax_amount": fee_line["total"],
						"description": fee_line["name"],
					},
				)

				# Add line for fee's taxes in Taxes and Charges table
				if fee_line["tax_status"] == "taxable" or len(fee_line["taxes"]) > 0:
					if not wc_server.tax_account_for_order_fee_lines:
						frappe.throw(_("Please set 'Tax Account for Order Fee Lines' in WooCommerce Server"))

					for fee_line_tax in fee_line["taxes"]:
						new_sales_order.append(
							"taxes",
							{
								"charge_type": "Actual",
								"account_head": wc_server.tax_account_for_order_fee_lines,
								"tax_amount": fee_line_tax["total"],
								"description": fee_line["name"] + " " + _("Tax"),
							},
						)

	def set_sales_order_item_fields(
		self, woocommerce_order_line_item: Dict, so_item: Union[SalesOrderItem, Dict]
	) -> Tuple[bool, WooCommerceOrder]:
		"""
		If there exist any Order Item Line Field Mappings on `WooCommerce Server`, attempt to set their values from
		the WooCommerce Order Line Item to the ERPNext Sales Order Item

		Returns true if woocommerce_order_line_item was changed
		"""
		so_item_dirty = False
		if so_item and self.woocommerce_order:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_order.woocommerce_server
			)
			if wc_server.order_line_item_field_map:
				for map in wc_server.order_line_item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")

					# We expect woocommerce_field_name to be valid JSONPath
					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_order_line_item_field_matches = jsonpath_expr.find(woocommerce_order_line_item)

					if len(woocommerce_order_line_item_field_matches) > 0:
						if type(so_item) is dict:
							so_item[erpnext_item_field_name[0]] = woocommerce_order_line_item_field_matches[0].value
						else:
							setattr(
								so_item, erpnext_item_field_name[0], woocommerce_order_line_item_field_matches[0].value
							)
							so_item_dirty = True
			return so_item_dirty, so_item

	def create_or_update_address(self, wc_order: WooCommerceOrder):
		"""
		If the address(es) exist, update it, else create it
		"""
		addresses = get_addresses_linking_to(
			"Customer", self.customer.name, fields=["name", "is_primary_address", "is_shipping_address"]
		)

		existing_billing_address = next(
			(addr for addr in addresses if addr.is_primary_address == 1), None
		)
		existing_shipping_address = next(
			(addr for addr in addresses if addr.is_shipping_address == 1), None
		)

		raw_billing_data = json.loads(wc_order.billing)
		raw_shipping_data = json.loads(wc_order.shipping)

		address_keys_to_compare = [
			"first_name",
			"last_name",
			"company",
			"address_1",
			"address_2",
			"city",
			"state",
			"postcode",
			"country",
		]
		address_keys_same = [
			True if raw_billing_data[key] == raw_shipping_data[key] else False
			for key in address_keys_to_compare
		]

		if all(address_keys_same):
			# Use one address for both billing and shipping
			address = existing_billing_address or existing_shipping_address
			if address:
				self.update_address(
					address.name, raw_billing_data, self.customer, is_primary_address=1, is_shipping_address=1
				)
			else:
				self.create_address(
					raw_billing_data, self.customer, "Billing", is_primary_address=1, is_shipping_address=1
				)
		else:
			# Handle billing address
			if existing_billing_address:
				self.update_address(
					existing_billing_address.name,
					raw_billing_data,
					self.customer,
					is_primary_address=1,
					is_shipping_address=0,
				)
			else:
				self.create_address(
					raw_billing_data, self.customer, "Billing", is_primary_address=1, is_shipping_address=0
				)

			# Handle shipping address
			if existing_shipping_address:
				self.update_address(
					existing_shipping_address.name,
					raw_shipping_data,
					self.customer,
					is_primary_address=0,
					is_shipping_address=1,
				)
			else:
				self.create_address(
					raw_shipping_data, self.customer, "Shipping", is_primary_address=0, is_shipping_address=1
				)

	def create_address(
		self, raw_data: Dict, customer, address_type, is_primary_address=0, is_shipping_address=0
	):
		title_convention = frappe.db.get_value(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server, "address_title_convention"
		)
		address = frappe.new_doc("Address")

		address.address_type = address_type
		address.address_line1 = raw_data.get("address_1", "Not Provided")
		address.address_line2 = raw_data.get("address_2", "Not Provided")
		address.city = raw_data.get("city", "Not Provided")
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "IN").lower()})
		address.state = raw_data.get("state")
		address.pincode = raw_data.get("postcode")
		address.phone = raw_data.get("phone")
		address.address_title = (
			customer.customer_name
			if title_convention == "Customer Name only"
			else f"{customer.name}-{address.address_type}"
		)
		address.is_primary_address = is_primary_address
		address.is_shipping_address = is_shipping_address
		address.append("links", {"link_doctype": "Customer", "link_name": customer.name})

		address.flags.ignore_mandatory = True
		address.save()

	def update_address(
		self, address_name, raw_data: Dict, customer, is_primary_address=0, is_shipping_address=0
	):
		title_convention = frappe.db.get_value(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server, "address_title_convention"
		)
		address = frappe.get_doc("Address", address_name)

		address.address_line1 = raw_data.get("address_1", "Not Provided")
		address.address_line2 = raw_data.get("address_2", "Not Provided")
		address.city = raw_data.get("city", "Not Provided")
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "IN").lower()})
		address.state = raw_data.get("state")
		address.pincode = raw_data.get("postcode")
		address.phone = raw_data.get("phone")
		address.address_title = (
			{customer.customer_name}
			if title_convention == "Customer Name only"
			else f"{customer.name}-{address.address_type}"
		)
		address.is_primary_address = is_primary_address
		address.is_shipping_address = is_shipping_address

		address.flags.ignore_mandatory = True
		address.save()


def get_list_of_wc_orders(
	date_time_from: Optional[datetime] = None,
	sales_order: Optional[SalesOrder] = None,
	status: Optional[str] = None,
):
	"""
	Fetches a list of WooCommerce Orders within a specified date range or linked with a Sales Order, using pagination.

	At least one of date_time_from, or sales_order parameters are required
	"""
	if not any([date_time_from, sales_order]):
		raise ValueError("At least one of date_time_from or sales_order parameters are required")

	wc_records_per_page_limit = 100
	page_length = wc_records_per_page_limit
	new_results = True
	start = 0
	filters = []
	wc_orders = []

	wc_settings = frappe.get_cached_doc("WooCommerce Integration Settings")
	minimum_creation_date = wc_settings.minimum_creation_date

	# Build filters
	if date_time_from:
		filters.append(["WooCommerce Order", "date_modified", ">", date_time_from])
	if minimum_creation_date:
		filters.append(["WooCommerce Order", "date_created", ">", minimum_creation_date])
	if sales_order:
		filters.append(["WooCommerce Order", "id", "=", sales_order.woocommerce_id])
	if status:
		filters.append(["WooCommerce Order", "status", "=", status])

	while new_results:
		woocommerce_order = frappe.get_doc({"doctype": "WooCommerce Order"})
		new_results = woocommerce_order.get_list(
			args={"filters": filters, "page_lenth": page_length, "start": start, "as_doc": True}
		)
		for wc_order in new_results:
			wc_orders.append(wc_order)
		start += page_length
		if len(new_results) < page_length:
			new_results = []

	return wc_orders


def rename_address(address, customer):
	old_address_title = address.name
	new_address_title = customer.name + "-" + address.address_type
	address.address_title = customer.customer_name
	address.save()

	frappe.rename_doc("Address", old_address_title, new_address_title)


def create_contact(data, customer):
	email = data.get("email", None)
	phone = data.get("phone", None)

	if not email and not phone:
		return

	contact = frappe.new_doc("Contact")
	contact.first_name = data.get("first_name")
	contact.last_name = data.get("last_name")
	contact.is_primary_contact = 1
	contact.is_billing_contact = 1

	if phone:
		contact.add_phone(phone, is_primary_mobile_no=1, is_primary_phone=1)

	if email:
		contact.add_email(email, is_primary=1)

	contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})

	contact.flags.ignore_mandatory = True
	contact.save()

	return contact


def add_tax_details(sales_order, price, desc, tax_account_head):
	sales_order.append(
		"taxes",
		{
			"charge_type": "Actual",
			"account_head": tax_account_head,
			"tax_amount": price,
			"description": desc,
		},
	)


def get_tax_inc_price_for_woocommerce_line_item(line_item: Dict):
	"""
	WooCommerce's Line Item "price" field will always show the tax excluding amount.
	This function calculates the tax inclusive rate for an item
	"""
	return (float(line_item.get("subtotal")) + float(line_item.get("subtotal_tax"))) / float(
		line_item.get("quantity")
	)


def create_placeholder_item(sales_order: SalesOrder):
	"""
	Create a placeholder Item for deleted WooCommerce Products
	"""
	wc_server = frappe.get_cached_doc("WooCommerce Server", sales_order.woocommerce_server)
	if not frappe.db.exists("Item", "DELETED_WOOCOMMERCE_PRODUCT"):
		item = frappe.new_doc("Item")
		item.item_code = "DELETED_WOOCOMMERCE_PRODUCT"
		item.item_name = "Deletet WooCommerce Product"
		item.description = "Deletet WooCommerce Product"
		item.item_group = "All Item Groups"
		item.stock_uom = wc_server.uom
		item.is_stock_item = 0
		item.is_fixed_asset = 0
		item.opening_stock = 0
		item.flags.created_by_sync = True
		item.save()
	else:
		item = frappe.get_doc("Item", "DELETED_WOOCOMMERCE_PRODUCT")
	return item


def get_addresses_linking_to(doctype, docname, fields=None):
	"""Return a list of Addresses containing a link to the given document."""
	return frappe.get_all(
		"Address",
		fields=fields,
		filters=[
			["Dynamic Link", "link_doctype", "=", doctype],
			["Dynamic Link", "link_name", "=", docname],
		],
	)
