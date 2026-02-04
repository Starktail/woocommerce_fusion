// Copyright (c) 2023, Dirk van der Laarse and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Server", {
  refresh: function (frm) {
    // Only list enabled warehouses
    frm.fields_dict.warehouses.get_query = function (doc) {
      return {
        filters: {
          disabled: 0,
          is_group: 0,
        },
      };
    };

    // Set the Options for erpnext_field_name field on 'Items' > 'Fields Mapping' child table
    frappe.call({
      method: "get_item_docfields",
      doc: frm.doc,
      args: {
        doctype: "Item",
      },
      callback: function (r) {
        // Sort the array of objects alphabetically by the label property
        r.message.sort((a, b) => {
          const labelA = a.label || "";
          const labelB = b.label || "";
          return labelA.localeCompare(labelB);
        });

        // Use map to create an array of strings in the desired format
        const formattedStrings = r.message.map(
          (fields) => `${fields.fieldname} | ${fields.label}`,
        );

        // Join the strings with newline characters to create the final string
        const options = formattedStrings.join("\n");

        // Set the Options property
        frm.fields_dict.item_field_map.grid.update_docfield_property(
          "erpnext_field_name",
          "options",
          options,
        );
      },
    });

    // Set the Options for order_line_item_field_map field on 'Sales Orders' > 'Fields Mapping' child table
    frappe.call({
      method: "get_item_docfields",
      doc: frm.doc,
      args: {
        doctype: "Sales Order Item",
      },
      callback: function (r) {
        // Sort the array of objects alphabetically by the label property
        r.message.sort((a, b) => {
          const labelA = a.label || "";
          const labelB = b.label || "";
          return labelA.localeCompare(labelB);
        });

        // Use map to create an array of strings in the desired format
        const formattedStrings = r.message.map(
          (fields) => `${fields.fieldname} | ${fields.label}`,
        );

        // Join the strings with newline characters to create the final string
        const options = formattedStrings.join("\n");

        // Set the Options property
        frm.fields_dict.order_line_item_field_map.grid.update_docfield_property(
          "erpnext_field_name",
          "options",
          options,
        );
      },
    });

    if (
      frm.doc.enable_so_status_sync &&
      !frm.fields_dict.sales_order_status_map.grid.get_docfield(
        "woocommerce_sales_order_status",
      ).options
    ) {
      frm.trigger("get_woocommerce_order_status_list");
    }

    // Set Options field for 'Sales Order Status Sync' section
    let warningHTML = `
			<div class="form-message red">
				<div>
					${__(
            "This setting is Experimental. Monitor your Error Log after enabling this setting",
          )}
				</div>
			</div>
			`;
    frm.set_df_property(
      "enable_so_status_sync_warning_html",
      "options",
      warningHTML,
    );
    frm.refresh_field("enable_so_status_sync_warning_html");
  },
  // Handle click of 'Keep the Status of ERPNext Sales Orders and WooCommerce Orders in sync'
  enable_so_status_sync: function (frm) {
    if (
      frm.doc.enable_so_status_sync &&
      !frm.fields_dict.sales_order_status_map.grid.get_docfield(
        "woocommerce_sales_order_status",
      ).options
    ) {
      frm.trigger("get_woocommerce_order_status_list");
    }
  },
  // Retrieve WooCommerce order statuses
  get_woocommerce_order_status_list: function (frm) {
    frappe.call({
      method: "get_woocommerce_order_status_list",
      doc: frm.doc,
      callback: function (r) {
        // Join the strings with newline characters to create the final string
        const options = r.message.join("\n");

        // Set the Options property
        frm.fields_dict.sales_order_status_map.grid.update_docfield_property(
          "woocommerce_sales_order_status",
          "options",
          options,
        );
      },
    });
  },
  // View WooCommerce Webhook Configuration
  view_webhook_config: function (frm) {
    let d = new frappe.ui.Dialog({
      title: __("WooCommerce Webhook Settings"),
      fields: [
        {
          label: __("Status"),
          fieldname: "status",
          fieldtype: "Data",
          default: "Active",
          read_only: 1,
        },
        {
          label: __("Topic"),
          fieldname: "topic",
          fieldtype: "Data",
          default: "Order created",
          read_only: 1,
        },
        {
          label: __("Delivery URL"),
          fieldname: "url",
          fieldtype: "Data",
          default:
            "<site url here>/api/method/woocommerce_fusion.woocommerce_endpoint.order_created",
          read_only: 1,
        },
        {
          label: __("Secret"),
          fieldname: "secret",
          fieldtype: "Code",
          default: frm.doc.secret,
          read_only: 1,
        },
        {
          label: __("API Version"),
          fieldname: "api_version",
          fieldtype: "Data",
          default: "WP REST API Integration v3",
          read_only: 1,
        },
      ],
      size: "large", // small, large, extra-large
      primary_action_label: __("OK"),
      primary_action(values) {
        d.hide();
      },
    });

    d.show();
  },
});
