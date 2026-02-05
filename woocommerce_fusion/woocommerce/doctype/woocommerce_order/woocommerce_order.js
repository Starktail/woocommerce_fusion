// Copyright (c) 2023, Dirk van der Laarse and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Order", {
  refresh: function (frm) {
    // Add a custom button to sync this WooCommerce order to a Sales Order
    frm.add_custom_button(
      __("Sync this Order to ERPNext"),
      function () {
        frm.trigger("sync_sales_order");
      },
      __("Actions"),
    );

    // Set intro text
    const intro_txt = __(
      "Note: This is a Virtual Document. Saving changes on this document will update this resource on WooCommerce.",
    );
    frm.set_intro(intro_txt, "orange");
  },
  sync_sales_order: function (frm) {
    // Sync this WooCommerce Order
    frappe.dom.freeze(__("Sync Order with ERPNext..."));
    frappe.call({
      method: "woocommerce_fusion.tasks.sync_sales_orders.run_sales_order_sync",
      args: {
        woocommerce_order_name: frm.doc.name,
      },
      callback: function (r) {
        console.log(r);
        frappe.dom.unfreeze();
        frappe.show_alert(
          {
            message: __("Sync completed successfully"),
            indicator: "green",
          },
          5,
        );
        frm.reload_doc();
      },
      error: (r) => {
        frappe.dom.unfreeze();
        frappe.show_alert(
          {
            message: __(
              "There was an error processing the request. See Error Log.",
            ),
            indicator: "red",
          },
          5,
        );
      },
    });
  },
});
