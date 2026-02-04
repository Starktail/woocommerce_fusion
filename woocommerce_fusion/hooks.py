app_name = "woocommerce_fusion"
app_title = "WooCommerce Fusion"
app_publisher = "Starktail (Pty) Ltd"
app_description = "WooCommerce connector for ERPNext v15+"
app_email = "support@starktail.com"
app_license = "GNU GPLv3"

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/woocommerce_fusion/css/woocommerce_fusion.css"
# app_include_js = "/assets/woocommerce_fusion/js/woocommerce_fusion.js"

# include js, css files in header of web template
# web_include_css = "/assets/woocommerce_fusion/css/woocommerce_fusion.css"
# web_include_js = "/assets/woocommerce_fusion/js/woocommerce_fusion.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "woocommerce_fusion/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {"Sales Order": "public/js/selling/sales_order.js", "Item": "public/js/stock/item.js"}
doctype_list_js = {"Sales Order": "public/js/selling/sales_order_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "woocommerce_fusion.utils.jinja_methods",
# 	"filters": "woocommerce_fusion.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "woocommerce_fusion.install.before_install"
# after_install = "woocommerce_fusion.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "woocommerce_fusion.uninstall.before_uninstall"
# after_uninstall = "woocommerce_fusion.uninstall.after_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "woocommerce_fusion.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {
	"Sales Order": "woocommerce_fusion.overrides.selling.sales_order.CustomSalesOrder",
}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }
doc_events = {
	"Stock Entry": {
		"on_submit": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		"on_cancel": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
	},
	"Stock Reconciliation": {
		"on_submit": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		"on_cancel": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
	},
	"Sales Invoice": {
		"on_submit": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		"on_cancel": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
	},
	"Delivery Note": {
		"on_submit": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		"on_cancel": "woocommerce_fusion.tasks.stock_update.update_stock_levels_for_woocommerce_item",
	},
	"Item Price": {
		"on_update": "woocommerce_fusion.tasks.sync_item_prices.update_item_price_for_woocommerce_item_from_hook"
	},
	"Sales Order": {"on_submit": "woocommerce_fusion.tasks.sync_sales_orders.run_sales_order_sync_from_hook"},
	"Item": {
		"on_update": "woocommerce_fusion.tasks.sync_items.run_item_sync_from_hook",
		"after_insert": "woocommerce_fusion.tasks.sync_items.run_item_sync_from_hook",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	# 	"all": [
	# 		"woocommerce_fusion.tasks.all"
	# 	],
	# 	"weekly": [
	# 		"woocommerce_fusion.tasks.daily"
	# 	],
	"hourly_long": [
		"woocommerce_fusion.tasks.sync_sales_orders.sync_woocommerce_orders_modified_since",
		"woocommerce_fusion.tasks.sync_items.sync_woocommerce_products_modified_since",
	],
	"daily_long": [
		"woocommerce_fusion.tasks.stock_update.update_stock_levels_for_all_enabled_items_in_background",
		"woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync_in_background",
	],
	# 	"monthly": [
	# 		"woocommerce_fusion.tasks.monthly"
	# 	],
}

# Testing
# -------

before_tests = "woocommerce_fusion.utils.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "woocommerce_fusion.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

ignore_links_on_delete = [
	"WooCommerce Request Log",
]

# Request Events
# ----------------
# before_request = ["woocommerce_fusion.utils.before_request"]
# after_request = ["woocommerce_fusion.utils.after_request"]

# Job Events
# ----------
# before_job = ["woocommerce_fusion.utils.before_job"]
# after_job = ["woocommerce_fusion.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"woocommerce_fusion.auth.validate"
# ]


fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				(
					"Customer-woocommerce_server",
					"Customer-woocommerce_identifier",
					"Customer-woocommerce_is_guest",
					"Sales Order-woocommerce_id",
					"Sales Order-woocommerce_server",
					"Sales Order-woocommerce_status",
					"Sales Order-woocommerce_payment_method",
					"Sales Order-woocommerce_shipment_tracking_html",
					"Sales Order-woocommerce_payment_entry",
					"Sales Order-custom_attempted_woocommerce_auto_payment_entry",
					"Sales Order-custom_woocommerce_last_sync_hash",
					"Sales Order-custom_woocommerce_customer_note",
					"Address-woocommerce_identifier",
					"Item-woocommerce_servers",
					"Item-custom_woocommerce_tab",
				),
			]
		],
	}
]

default_log_clearing_doctypes = {
	"WooCommerce Request Log": 7,
}


standard_help_items = [
	{
		"item_label": "WooCommerce Fusion Documentation",
		"item_type": "Route",
		"route": "/woocommerce_fusion_introduction",
		"is_standard": 1,
	},
]
