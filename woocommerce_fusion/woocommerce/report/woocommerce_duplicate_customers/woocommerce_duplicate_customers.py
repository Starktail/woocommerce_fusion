"""
Groups Customers that look like the same buyer, so that duplicates created by earlier order syncs
can be found and merged by hand.

Reports only - nothing is changed. Use *Merge with*, on the **Customer** form, to act on a group.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum

# Providers whose address says nothing about who the customer is, so a shared domain there is not a
# sign of two records being the same buyer. The default of the report's *Ignore Email Domains* filter -
# global providers only, since which regional ISPs matter depends on where the shop sells.
DEFAULT_IGNORED_EMAIL_DOMAINS = (
	"gmail.com, googlemail.com, yahoo.com, ymail.com, hotmail.com, outlook.com, live.com, msn.com, "
	"icloud.com, me.com, mac.com, aol.com, protonmail.com, proton.me, pm.me, gmx.com, gmx.net, "
	"zoho.com, mail.com, yandex.com, qq.com, 163.com"
)


def get_ignored_email_domains(filters) -> set[str]:
	"""
	The domains that do not identify a customer. Comma or newline separated, `@` optional.
	"""
	listed = filters.get("ignore_email_domains")
	if listed is None:
		listed = DEFAULT_IGNORED_EMAIL_DOMAINS

	return {
		domain.strip().lower().lstrip("@")
		for domain in listed.replace("\n", ",").split(",")
		if domain.strip()
	}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	customers = get_customers(filters)
	groups = build_groups(customers, filters)

	return get_columns(), get_rows(groups, customers)


def get_columns() -> list[dict]:
	return [
		{"label": _("Grouped By"), "fieldname": "reason", "fieldtype": "Data", "width": 110},
		{"label": _("Group"), "fieldname": "group", "fieldtype": "Data", "width": 200},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 200,
		},
		{"label": _("Customer Name"), "fieldname": "customer_name", "fieldtype": "Data", "width": 180},
		{"label": _("Type"), "fieldname": "customer_type", "fieldtype": "Data", "width": 90},
		{"label": _("WooCommerce Identifier"), "fieldname": "identifier", "fieldtype": "Data", "width": 220},
		{"label": _("Guest"), "fieldname": "is_guest", "fieldtype": "Check", "width": 60},
		{"label": _("Sales Orders"), "fieldname": "orders", "fieldtype": "Int", "width": 110},
		{"label": _("Ordered"), "fieldname": "ordered", "fieldtype": "Currency", "width": 120},
		{"label": _("Created"), "fieldname": "creation", "fieldtype": "Datetime", "width": 150},
	]


def get_customers(filters) -> list[frappe._dict]:
	conditions = {}
	if filters.get("woocommerce_only"):
		conditions["woocommerce_identifier"] = ("is", "set")

	customers = frappe.get_all(
		"Customer",
		filters=conditions,
		fields=[
			"name",
			"customer_name",
			"customer_type",
			"woocommerce_identifier",
			"woocommerce_is_guest",
			"customer_primary_contact",
			"creation",
		],
	)

	# Sales Order counts and values, so that the record worth keeping is obvious
	so = frappe.qb.DocType("Sales Order")
	totals = (
		frappe.qb.from_(so)
		.select(so.customer, Count(so.name).as_("orders"), Sum(so.grand_total).as_("ordered"))
		.where(so.docstatus < 2)
		.groupby(so.customer)
		.run(as_dict=True)
	)
	by_customer = {row.customer: row for row in totals}

	for customer in customers:
		row = by_customer.get(customer.name)
		customer.orders = row.orders if row else 0
		customer.ordered = row.ordered if row else 0
		customer.email = get_customer_email(customer)

	return customers


def get_customer_email(customer) -> str:
	"""
	The Customer's email: its WooCommerce identifier where that is an address, else its primary
	Contact's. The identifier carries a `-Company` suffix when Dual Accounts is enabled.
	"""
	identifier = (customer.woocommerce_identifier or "").strip().lower()
	if "@" in identifier:
		return identifier.split("-")[0] if identifier.count("@") == 1 else identifier

	if customer.customer_primary_contact:
		return (
			(frappe.db.get_value("Contact", customer.customer_primary_contact, "email_id") or "")
			.strip()
			.lower()
		)

	return ""


def build_groups(customers: list, filters) -> dict[tuple[str, str], list[str]]:
	"""
	Bucket the Customers by each signal in turn. Only buckets holding more than one Customer are
	reported - a bucket of one is just a customer.

	Email is the signal to trust; Name is the widest net and needs a judgement call on every group.
	"""
	buckets: dict[tuple[str, str], list[str]] = {}
	wanted = filters.get("grouped_by")
	ignored_domains = get_ignored_email_domains(filters)

	def add(reason: str, key: str, customer: str):
		if key and (not wanted or reason == wanted):
			buckets.setdefault((reason, key), []).append(customer)

	for customer in customers:
		add(_("Email"), customer.email, customer.name)
		add(_("Name"), (customer.customer_name or "").strip().lower(), customer.name)

		domain = customer.email.rsplit("@", 1)[-1] if "@" in customer.email else ""
		if domain and domain not in ignored_domains:
			add(_("Email Domain"), domain, customer.name)

	return {key: names for key, names in buckets.items() if len(names) > 1}


def get_rows(groups: dict, customers: list) -> list[dict]:
	by_name = {customer.name: customer for customer in customers}
	rows = []

	# Largest groups first, and within a group the oldest Customer first - that is usually the one to
	# keep and merge the rest into
	for (reason, key), names in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
		for name in sorted(names, key=lambda name: by_name[name].creation):
			customer = by_name[name]
			rows.append(
				{
					"reason": reason,
					"group": key,
					"customer": customer.name,
					"customer_name": customer.customer_name,
					"customer_type": customer.customer_type,
					"identifier": customer.woocommerce_identifier,
					"is_guest": customer.woocommerce_is_guest,
					"orders": customer.orders,
					"ordered": customer.ordered,
					"creation": customer.creation,
				}
			)

	return rows
