frappe.ui.form.on("Sales Order", {
  refresh: function (frm) {
    // Add a custom button to navigate to WooCommerce and open this order
    if (frm.doc.woocommerce_id) {
      frm.add_custom_button(
        __("Open in WooCommerce"),
        function () {
          frappe.db.get_value(
            "WooCommerce Server",
            frm.doc.woocommerce_server,
            "woocommerce_server_url",
            (values) => {
              window.open(
                values.woocommerce_server_url +
                  `/wp-admin/post.php?post=${frm.doc.woocommerce_id}&action=edit`,
                "_blank",
              );
            },
          );
        },
        __("Actions"),
      );
    }

    // Add a custom button to sync Sales Orders with WooCommerce
    if (frm.doc.woocommerce_id) {
      frm.add_custom_button(
        __("Sync this Order with WooCommerce"),
        function () {
          frm.trigger("sync_sales_order");
        },
        __("Actions"),
      );
    }

    // Add a custom button to allow adding or editing Shipment Trackings
    if (frm.doc.woocommerce_id) {
      frm.add_custom_button(
        __("Edit WooCommerce Shipment Trackings"),
        function () {
          frm.trigger("prompt_user_for_shipment_trackings");
        },
        __("Actions"),
      );
    }

    if (
      frm.doc.woocommerce_id &&
      frm.doc.woocommerce_server &&
      ["Shipped", "Delivered"].includes(frm.doc.woocommerce_status)
    ) {
      frm.trigger("load_shipment_trackings_table");
    } else {
      // Clean up Shipment Tracking HTML
      frm.doc.woocommerce_shipment_trackings = [];
      frm.set_df_property("woocommerce_shipment_tracking_html", "options", " ");
    }
  },

  sync_sales_order: function (frm) {
    // Sync this Sales Order
    frappe.dom.freeze(__("Sync Order with WooCommerce..."));
    frappe.call({
      method: "woocommerce_fusion.tasks.sync_sales_orders.run_sales_order_sync",
      args: {
        sales_order_name: frm.doc.name,
      },
      callback: function (r) {
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

  woocommerce_status: function (frm) {
    // Triggered when woocommerce_status is changed
    frappe.confirm(
      "Changing the status will update the order status on WooCommerce. Do you want to continue?",
      // If Yes is clicked
      function () {
        frm.save(
          "Update",
          function () {
            console.log("frm.doc", frm.doc);
            // The callback on frm.save() is always called, even if there are errors in saving
            // so first check if the form is unsaved
            if (!frm.doc.__unsaved) {
              frappe.dom.freeze(__("Updating Order status on WooCommerce..."));
              frappe.call({
                method:
                  "woocommerce_fusion.tasks.sync_sales_orders.run_sales_order_sync",
                args: {
                  sales_order_name: frm.doc.name,
                },
                // disable the button until the request is completed
                btn: $(".primary-action"),
                callback: (r) => {
                  frappe.dom.unfreeze();
                  frappe.show_alert(
                    {
                      message: __("Updated WooCommerce Order successfully"),
                      indicator: "green",
                    },
                    5,
                  );
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
                  console.error(r); // Log the error for debugging
                },
              });
            }
          },
          // eslint-disable-next-line no-undef
          (on_error = function (error) {
            // If the .save() fails
            console.error(error.exception); // Log the error for debugging
            frm.reload_doc();
          }),
        );
      },
      // If No is clicked
      function () {
        frm.reload_doc();
      },
    );
  },

  load_shipment_trackings_table: function (frm) {
    // Add a table with Shipment Trackings
    frm.set_df_property(
      "woocommerce_shipment_tracking_html",
      "options",
      "🚚 <i>Loading Shipments...</i><br><br><br><br>",
    );
    frm.refresh_field("woocommerce_shipment_tracking_html");
    frappe.call({
      method:
        "woocommerce_fusion.overrides.selling.sales_order.get_woocommerce_order_shipment_trackings",
      args: {
        doc: frm.doc,
      },
      callback: function (r) {
        if (r.message) {
          frappe.show_alert({
            indicator: "green",
            message: __("Retrieved WooCommerce Shipment Trackings"),
          });
          frm.doc.woocommerce_shipment_trackings = r.message;

          let trackingsHTML =
            `<b>WooCommerce Shipments:</b><br><table class="table table-striped">` +
            `<tr><th>Date Shipped</th><th>Provider</th><th>Tracking Number</th>`;
          frm.doc.woocommerce_shipment_trackings.forEach((tracking) => {
            trackingsHTML +=
              `<tr><td>${tracking.date_shipped}</td>` +
              `<td>${tracking.tracking_provider}</td>` +
              `<td><a href="${tracking.tracking_link}">${tracking.tracking_number}</a></td></tr>`;
          });
          trackingsHTML += `</table>`;
          frm.set_df_property(
            "woocommerce_shipment_tracking_html",
            "options",
            trackingsHTML,
          );
          frm.refresh_field("woocommerce_shipment_tracking_html");
        } else {
          frm.set_df_property(
            "woocommerce_shipment_tracking_html",
            "options",
            "",
          );
          frm.refresh_field("woocommerce_shipment_tracking_html");
        }
      },
    });
  },

  prompt_user_for_shipment_trackings: function (frm) {
    //Get the shipment providers from 'WooCommerce Server'
    frappe.call({
      method:
        "woocommerce_fusion.woocommerce.doctype.woocommerce_server" +
        ".woocommerce_server.get_woocommerce_shipment_providers",
      args: {
        woocommerce_server: frm.doc.woocommerce_server,
      },
      callback: function (r) {
        const trackingProviders = r.message;
        let shipment_trackings = frm.doc.woocommerce_shipment_trackings;

        //Prompt the use to update the Tracking Details
        let d = new frappe.ui.Dialog({
          title: __("Enter Shipment Tracking details"),
          fields: [
            {
              fieldname: "tracking_id",
              fieldtype: "Data",
              label: "Tracking ID",
              read_only: 1,
              default:
                shipment_trackings.length > 0
                  ? shipment_trackings[0].tracking_id
                  : null,
            },
            {
              fieldname: "tracking_provider",
              fieldtype: "Select",
              label: "Tracking Provider",
              reqd: 1,
              options: trackingProviders,
              default:
                shipment_trackings.length > 0
                  ? shipment_trackings[0].tracking_provider
                  : null,
            },
            {
              fieldname: "tracking_number",
              fieldtype: "Data",
              label: "Tracking Number",
              reqd: 1,
              default:
                shipment_trackings.length > 0
                  ? shipment_trackings[0].tracking_number
                  : null,
            },
            {
              fieldname: "tracking_link",
              fieldtype: "Data",
              label: "Tracking Link",
              read_only: 1,
              default:
                shipment_trackings.length > 0
                  ? shipment_trackings[0].tracking_link
                  : null,
            },
            {
              fieldname: "date_shipped",
              fieldtype: "Date",
              label: "Date Shipped",
              reqd: 1,
              default:
                shipment_trackings.length > 0
                  ? shipment_trackings[0].date_shipped
                  : null,
            },
          ],
          primary_action: function () {
            let values = d.get_values();
            let shipment_tracking = {
              tracking_id: null,
              tracking_provider: values.tracking_provider,
              tracking_link: null,
              tracking_number: values.tracking_number,
              date_shipped: values.date_shipped,
            };
            d.hide();

            // Call a method to update the shipment tracking
            frm.doc.woocommerce_shipment_trackings = [shipment_tracking];
            frm.trigger("update_shipment_trackings");
          },
          primary_action_label: __("Submit and Sync to WooCommerce"),
        });
        d.show();
      },
    });
  },

  update_shipment_trackings: function (frm) {
    //Call method to update the Shipment Trackings
    frappe.call({
      method:
        "woocommerce_fusion.overrides.selling.sales_order.update_woocommerce_order_shipment_trackings",
      args: {
        doc: frm.doc,
        shipment_trackings: frm.doc.woocommerce_shipment_trackings,
      },
      callback: function (r) {
        frm.reload_doc();
      },
    });
  },
});
