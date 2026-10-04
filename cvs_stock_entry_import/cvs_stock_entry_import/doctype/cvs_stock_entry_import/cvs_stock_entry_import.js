// -----------------------------------------------
// Error Handler (same shape as CVS Invoice Tool's handler)
// -----------------------------------------------
function handle_erp_error(err, fallback_title) {
  if (err && err._server_messages) {
    try {
      JSON.parse(err._server_messages).forEach((raw) => {
        try {
          frappe.msgprint(JSON.parse(raw));
        } catch (e) {
          frappe.msgprint({ title: fallback_title, message: raw, indicator: "red" });
        }
      });
    } catch (e) {
      frappe.msgprint({ title: fallback_title, message: String(err._server_messages), indicator: "red" });
    }
    return;
  }

  if (err && err.exc) {
    const lines = String(err.exc).split("\n").map((l) => l.trim()).filter(Boolean);
    frappe.msgprint({
      title: fallback_title || __("Server Error"),
      message: lines[lines.length - 1] || __("An unexpected server error occurred."),
      indicator: "red",
    });
    return;
  }

  frappe.msgprint({
    title: fallback_title || __("Error"),
    message: err?.message || err?.statusText || __("An unexpected error occurred. Please check the Error Log for details."),
    indicator: "red",
  });
}

frappe.ui.form.on("CVS Stock Entry Import", {
  refresh(frm) {
    frm.set_df_property("generated_stock_entry", "read_only", 1);

    if (!frm.doc.company) {
      let default_company = frappe.defaults.get_user_default("Company");
      if (default_company) frm.set_value("company", default_company);
    }

    frm.set_query("cvs_setting", () => ({
      filters: frm.doc.company ? { company: frm.doc.company } : {},
    }));

    frm.set_query("item_code", "items", () => ({
      filters: { disabled: 0, is_stock_item: 1 },
    }));

    frm.set_query("batch_no", "items", (doc, cdt, cdn) => {
      let row = frappe.get_doc(cdt, cdn);
      return { filters: { item: row.item_code, disabled: 0 } };
    });

    // -----------------------------------------------
    // EXTRACT DATA
    // -----------------------------------------------
    $("button[data-fieldname='extract_data']")
      .off("click")
      .on("click", () => {
        if ($("button[data-fieldname='extract_data']").prop("disabled")) return;

        let missing = [];
        if (!frm.doc.material_issuance_summary) missing.push(__("Material Issuance Summary"));
        if (!frm.doc.cvs_setting) missing.push(__("AI Setting"));

        if (missing.length) {
          frappe.msgprint({
            title: __("Required Fields Missing"),
            message: __("Please fill in the following fields before extracting: {0}", [
              "<br><ul><li>" + missing.join("</li><li>") + "</li></ul>",
            ]),
            indicator: "orange",
          });
          return;
        }

        $("button[data-fieldname='extract_data']").prop("disabled", true);
        frappe.dom.freeze(__("Extracting data with AI — this may take a moment..."));

        frappe.call({
          method: "cvs_stock_entry_import.cvs_stock_entry_import.extract.extract_material_issuance",
          args: {
            file_path: frm.doc.material_issuance_summary,
            setting_name: frm.doc.cvs_setting,
          },
          callback(r) {
            frappe.dom.unfreeze();
            $("button[data-fieldname='extract_data']").prop("disabled", false);

            if (!r.message || r.message.status !== 1) {
              frappe.msgprint({
                title: __("Extraction Failed"),
                message: r.message?.error || __("The AI could not extract data from this file. Please check the file and try again."),
                indicator: "red",
              });
              return;
            }

            const data = r.message.data;

            frm.set_value("posting_date", data.posting_date || "");
            frm.set_value("job_number", data.job_number || "");
            frm.set_value("opr_number", data.opr_number || "");

            let rows = (data.items || []).map((row) => ({
              original_description: row.description || "",
              extracted_item_code: row.item_code_on_document || "",
              item_code: row.item_code || "",
              item_name: row.item_name || "",
              uom: row.uom || "",
              requested_qty: row.requested_qty || 0,
              issued_qty: row.issued_qty || 0,
              batch_no: row.batch_no || "",
              match_status: row.match_status || "",
              match_confidence: row.match_confidence || 0,
              match_notes: row.match_notes || "",
              candidates_json: row.candidates_json || "",
            }));

            frm.set_value("items", rows);
            frm.refresh_field("items");

            let needs_review = rows.filter((r) =>
              ["Multiple Matches", "No Match", "Possible Match"].includes(r.match_status)
            ).length;

            frappe.show_alert(
              {
                message: needs_review
                  ? __("Extraction complete — {0} item(s) found, {1} need review.", [rows.length, needs_review])
                  : __("Extraction complete — {0} item(s) found.", [rows.length]),
                indicator: needs_review ? "orange" : "green",
              },
              8
            );
          },
          error(r) {
            frappe.dom.unfreeze();
            $("button[data-fieldname='extract_data']").prop("disabled", false);
            handle_erp_error(r, __("Extraction Error"));
          },
        });
      });

    // -----------------------------------------------
    // GENERATE STOCK ENTRY
    // -----------------------------------------------
    $("button[data-fieldname='generate_stock_entry']")
      .off("click")
      .on("click", () => {
        if ($("button[data-fieldname='generate_stock_entry']").prop("disabled")) return;

        if (!frm.doc.company) {
          frappe.msgprint({ title: __("Company Required"), message: __("Please select a Company."), indicator: "orange" });
          return;
        }
        if (!frm.doc.source_warehouse) {
          frappe.msgprint({ title: __("Source Warehouse Required"), message: __("Please select a Source Warehouse."), indicator: "orange" });
          return;
        }
        if (!(frm.doc.items || []).length) {
          frappe.msgprint({ title: __("No Items"), message: __("Please extract or add at least one item first."), indicator: "orange" });
          return;
        }

        let unmatched = (frm.doc.items || []).filter((r) => !r.item_code);
        if (unmatched.length) {
          frappe.msgprint({
            title: __("Unmatched Items"),
            message: __(
              "{0} row(s) still have no Item Code selected. Please review the Match Status column and select an Item Code for every row before generating the Stock Entry.",
              [unmatched.length]
            ),
            indicator: "orange",
          });
          return;
        }

        frappe.confirm(
          __("Generate a standard ERPNext Stock Entry (Material Issue) from this document? It will be created as a Draft."),
          () => {
            $("button[data-fieldname='generate_stock_entry']").prop("disabled", true);
            frappe.dom.freeze(__("Generating Stock Entry — please wait..."));

            frappe.call({
              method: "cvs_stock_entry_import.cvs_stock_entry_import.doctype.cvs_stock_entry_import.cvs_stock_entry_import.generate_stock_entry",
              args: { name: frm.doc.name },
              callback(r) {
                frappe.dom.unfreeze();
                $("button[data-fieldname='generate_stock_entry']").prop("disabled", false);

                if (!r.message || r.message.status !== 1) return;

                frm.reload_doc();

                (r.message.warnings || []).forEach((w) => {
                  frappe.msgprint({ title: __("Review"), message: w, indicator: "orange" });
                });

                frappe.show_alert(
                  {
                    message: __("Stock Entry {0} created successfully.", [
                      `<a href="/app/stock-entry/${r.message.stock_entry}" target="_blank">${r.message.stock_entry}</a>`,
                    ]),
                    indicator: "green",
                  },
                  8
                );
              },
              error(r) {
                frappe.dom.unfreeze();
                $("button[data-fieldname='generate_stock_entry']").prop("disabled", false);
                handle_erp_error(r, __("Stock Entry Generation Failed"));
              },
            });
          }
        );
      });
  },

  company(frm) {
    // Clear the AI Setting only if it actually belongs to a different
    // Company — not when this change was triggered by selecting the AI
    // Setting itself (see cvs_setting handler below).
    if (!frm.doc.cvs_setting) return;
    frappe.db.get_value("CVS Stock Entry Import Setting", frm.doc.cvs_setting, "company").then((r) => {
      if (r.message?.company && r.message.company !== frm.doc.company) {
        frm.set_value("cvs_setting", "");
      }
    });
  },

  cvs_setting(frm) {
    if (!frm.doc.cvs_setting) return;
    frappe.db.get_value("CVS Stock Entry Import Setting", frm.doc.cvs_setting, ["company", "default_source_warehouse"]).then((r) => {
      if (r.message?.company && !frm.doc.company) frm.set_value("company", r.message.company);
      if (r.message?.default_source_warehouse && !frm.doc.source_warehouse) {
        frm.set_value("source_warehouse", r.message.default_source_warehouse);
      }
    });
  },
});

// -----------------------------------------------
// Items grid: manual Item Code correction
// -----------------------------------------------
frappe.ui.form.on("CVS Stock Entry Import Item", {
  item_code(frm, cdt, cdn) {
    let row = frappe.get_doc(cdt, cdn);
    if (!row.item_code) return;

    frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom"]).then((r) => {
      if (!r.message) return;
      frappe.model.set_value(cdt, cdn, "item_name", r.message.item_name || "");
      if (!row.uom) frappe.model.set_value(cdt, cdn, "uom", r.message.stock_uom || "");
      frappe.model.set_value(cdt, cdn, "match_status", "Manual Selection");
      frappe.model.set_value(cdt, cdn, "match_confidence", 100);
      frappe.model.set_value(cdt, cdn, "match_notes", __("Item Code selected manually."));
    });

    if (row.original_description) {
      frappe.call({
        method:
          "cvs_stock_entry_import.cvs_stock_entry_import.doctype.cvs_stock_entry_import.cvs_stock_entry_import.save_manual_match_alias",
        args: {
          original_description: row.original_description,
          item_code: row.item_code,
          source_name: frm.doc.name,
        },
      });
    }
  },
});
