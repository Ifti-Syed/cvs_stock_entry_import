from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext.stock.get_item_details import get_conversion_factor

from cvs_stock_entry_import.cvs_stock_entry_import import item_matcher


class CVSStockEntryImport(Document):
	pass


@frappe.whitelist()
def generate_stock_entry(name):
	"""
	Create a standard draft ERPNext Stock Entry (Material Issue) from a
	reviewed CVS Stock Entry Import. Always goes through frappe.get_doc/
	standard document methods so ERPNext's own validation, valuation, UOM
	conversion, warehouse handling and (for batch items) Serial and Batch
	Bundle creation all run exactly as they would from the Desk UI.

	Duplicate-prevention is enforced here (server-side, row-locked) — not
	only by the client-side button visibility.
	"""
	doc = frappe.get_doc("CVS Stock Entry Import", name)
	doc.check_permission("write")

	# Row-lock this record for the duration of the request so two rapid
	# clicks (or two browser tabs) can't both pass the "not yet generated"
	# check before either has committed its Stock Entry link back.
	locked = frappe.db.sql(
		"select generated_stock_entry from `tabCVS Stock Entry Import` where name=%s for update",
		(name,),
		as_dict=True,
	)
	existing_name = locked[0].generated_stock_entry if locked else None

	if existing_name:
		existing = frappe.db.get_value("Stock Entry", existing_name, ["name", "docstatus"], as_dict=True)
		if existing and existing.docstatus != 2:
			frappe.throw(
				_("A Stock Entry has already been generated for this document: {0}").format(
					frappe.get_desk_link("Stock Entry", existing.name)
				)
			)
		# The previously generated Stock Entry was cancelled/deleted — allow
		# a fresh one to be generated below instead of staying stuck.

	if not doc.company:
		frappe.throw(_("Please select a Company."))
	if not doc.source_warehouse:
		frappe.throw(_("Please select a Source Warehouse."))
	if not doc.items:
		frappe.throw(_("Please extract or add at least one item before generating the Stock Entry."))

	se_meta = frappe.get_meta("Stock Entry")
	sed_meta = frappe.get_meta("Stock Entry Detail")

	warnings = []
	se_items = []

	for row in doc.items:
		if not row.item_code:
			frappe.throw(_("Row #{0}: Please select an Item Code before generating the Stock Entry.").format(row.idx))

		item = frappe.db.get_value("Item", row.item_code, ["disabled", "is_stock_item"], as_dict=True)
		if not item:
			frappe.throw(_("Row #{0}: Item {1} does not exist.").format(row.idx, row.item_code))
		if item.disabled:
			frappe.throw(_("Row #{0}: Item {1} is disabled.").format(row.idx, row.item_code))
		if not item.is_stock_item:
			frappe.throw(_("Row #{0}: Item {1} is not a stock item.").format(row.idx, row.item_code))

		if not row.issued_qty or row.issued_qty <= 0:
			frappe.throw(_("Row #{0}: Issued Qty must be greater than zero for Item {1}.").format(row.idx, row.item_code))

		if row.batch_no:
			batch_item = frappe.db.get_value("Batch", row.batch_no, "item")
			if not batch_item:
				frappe.throw(_("Row #{0}: Batch {1} does not exist.").format(row.idx, row.batch_no))
			if batch_item != row.item_code:
				frappe.throw(
					_("Row #{0}: Batch {1} does not belong to Item {2}.").format(row.idx, row.batch_no, row.item_code)
				)

		# UOM is extracted/user-chosen and is never silently replaced with Stock
		# UOM here — only checked, with a review warning if it doesn't look
		# configured for this Item. Stock UOM is always ERP-controlled.
		stock_uom = item_matcher.get_stock_uom(row.item_code)
		row_uom = row.uom or stock_uom
		is_valid, uom_warning = item_matcher.check_uom(row.item_code, row_uom)
		if not is_valid:
			warnings.append(_("Row {0}: {1}").format(row.idx, uom_warning))

		conversion_factor = get_conversion_factor(row.item_code, row_uom).get("conversion_factor") or 1.0

		se_row = {
			"item_code": row.item_code,
			"qty": row.issued_qty,
			"uom": row_uom,
			"stock_uom": stock_uom,
			"conversion_factor": conversion_factor,
			"s_warehouse": doc.source_warehouse,
		}
		if sed_meta.has_field("requested_qty"):
			se_row["requested_qty"] = row.requested_qty
		if row.batch_no:
			se_row["batch_no"] = row.batch_no
			se_row["use_serial_batch_fields"] = 1

		se_items.append(se_row)

	opr_value = None
	if doc.opr_number:
		if frappe.db.exists("Order Processing Request", doc.opr_number):
			opr_value = doc.opr_number
		else:
			warnings.append(
				_("OPR {0} was not found in Order Processing Request — left blank on the Stock Entry.").format(
					doc.opr_number
				)
			)

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Issue"
	se.purpose = "Material Issue"
	se.company = doc.company
	if doc.posting_date:
		se.posting_date = doc.posting_date
	se.from_warehouse = doc.source_warehouse
	if se_meta.has_field("job_number"):
		se.job_number = doc.job_number
	if opr_value and se_meta.has_field("custom_opr"):
		se.custom_opr = opr_value

	for row in se_items:
		se.append("items", row)

	# Standard document insert — ERPNext's own controller handles valuation,
	# UOM conversion, warehouse validation, and (for batch items) Serial and
	# Batch Bundle creation. No ignore_permissions: the user's own Stock
	# Entry "create" permission is enforced exactly as it would be from the
	# Desk UI.
	se.insert()

	doc.generated_stock_entry = se.name
	doc.save()

	return {"status": 1, "stock_entry": se.name, "warnings": warnings}


@frappe.whitelist()
def save_manual_match_alias(original_description, item_code, source_name=None):
	"""
	Called when a user manually corrects an Item Code on the Items grid, so
	the same printed description resolves automatically next time.
	"""
	item_matcher.upsert_alias(
		original_description,
		item_code,
		source="CVS Stock Entry Import",
		created_from=source_name,
	)
