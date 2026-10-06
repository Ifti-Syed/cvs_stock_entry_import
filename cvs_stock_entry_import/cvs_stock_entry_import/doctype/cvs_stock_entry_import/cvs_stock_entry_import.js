frappe.ui.form.on("CVS Stock Entry Import", {
	setup(frm) {
		frm.set_query("cvs_setting", () => ({
			filters: frm.doc.company ? { company: frm.doc.company } : {},
		}));
		frm.set_query("item_code", "items", () => ({
			filters: { disabled: 0, is_stock_item: 1 },
		}));
		frm.set_query("batch_no", "items", (doc, cdt, cdn) => ({
			filters: { item: locals[cdt][cdn].item_code, disabled: 0 },
		}));
	},

	cvs_setting(frm) {
		if (!frm.doc.cvs_setting) return;
		frappe.db
			.get_value("CVS Stock Entry Import Setting", frm.doc.cvs_setting, ["company", "default_source_warehouse"])
			.then(({ message }) => {
				if (message.company && !frm.doc.company) frm.set_value("company", message.company);
				if (message.default_source_warehouse && !frm.doc.source_warehouse) {
					frm.set_value("source_warehouse", message.default_source_warehouse);
				}
			});
	},

	extract_data(frm) {
		const run = () =>
			save_if_needed(frm)
				.then(() =>
					frm.call({
						method: "extract_data",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Extracting data with AI..."),
					})
				)
				.then(({ message }) => {
					if (!message) return;
					if (message.date_unreadable) {
						frappe.msgprint(__("The date on the document could not be read. Please enter the Posting Date."));
					}
					frappe.show_alert({
						message: message.needs_review
							? __("{0} item(s) extracted, {1} need review.", [message.rows, message.needs_review])
							: __("{0} item(s) extracted.", [message.rows]),
						indicator: message.needs_review ? "orange" : "green",
					});
				});

		if ((frm.doc.items || []).length) {
			frappe.confirm(__("Re-extracting will replace the current items. Continue?"), run);
		} else {
			run();
		}
	},

	generate_stock_entry(frm) {
		frappe.confirm(__("Create a Draft Stock Entry (Material Issue) from this document?"), () =>
			save_if_needed(frm)
				.then(() =>
					frm.call({
						method: "generate_stock_entry",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Creating Stock Entry..."),
					})
				)
				.then(({ message }) => {
					if (!message) return;
					if (message.warnings.length) {
						frappe.msgprint({ title: __("Please Review"), message: message.warnings.join("<br>"), indicator: "orange" });
					}
					frappe.show_alert({
						message: __("Stock Entry {0} created.", [frappe.utils.get_form_link("Stock Entry", message.stock_entry, true)]),
						indicator: "green",
					});
				})
		);
	},
});

frappe.ui.form.on("CVS Stock Entry Import Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) {
			frappe.model.set_value(cdt, cdn, { item_name: "", stock_uom: "" });
			return;
		}
		frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom"]).then(({ message }) => {
			frappe.model.set_value(cdt, cdn, {
				item_name: message.item_name,
				stock_uom: message.stock_uom,
				match_status: "Manual Selection",
				match_confidence: 100,
				match_notes: __("Item Code selected manually."),
			});
		});
		if (row.original_description) {
			frappe.call({
				method: "cvs_stock_entry_import.cvs_stock_entry_import.doctype.cvs_stock_entry_import.cvs_stock_entry_import.save_manual_match_alias",
				args: { original_description: row.original_description, item_code: row.item_code, source_name: frm.is_new() ? null : frm.doc.name },
			});
		}
	},
});

function save_if_needed(frm) {
	return frm.is_new() || frm.is_dirty() ? frm.save() : Promise.resolve();
}
