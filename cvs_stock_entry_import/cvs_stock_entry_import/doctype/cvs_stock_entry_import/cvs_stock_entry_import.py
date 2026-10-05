import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from cvs_stock_entry_import.cvs_stock_entry_import import item_matcher
from cvs_stock_entry_import.cvs_stock_entry_import.extract import extract_document

REVIEW_STATUSES = ("Possible Match", "Multiple Matches", "No Match")


class CVSStockEntryImport(Document):
	@frappe.whitelist()
	def extract_data(self):
		self.check_permission("write")
		if self.generated_stock_entry and frappe.db.get_value("Stock Entry", self.generated_stock_entry, "docstatus") != 2:
			frappe.throw(_("A Stock Entry has already been generated from this document."))
		if not self.material_issuance_summary or not self.cvs_setting:
			frappe.throw(_("Please attach the Material Issuance Summary and select an AI Setting."))

		data = extract_document(self.material_issuance_summary, self.cvs_setting)

		self.posting_date = _valid_date(data["posting_date"])
		self.job_number = data["job_number"]
		self.opr_number = data["opr_number"]
		self.set("items", [])
		for row in data["items"]:
			match = item_matcher.match_row(row["item_code_on_document"], row["description"])
			batch_no, batch_note = _check_batch(row["batch_id"], match["item_code"])
			if batch_note:
				match["match_notes"] = f"{match['match_notes']} {batch_note}"
			self.append("items", {
				**match,
				"original_description": row["description"],
				"extracted_item_code": row["item_code_on_document"],
				"uom": row["uom"],
				"requested_qty": row["requested_qty"],
				"issued_qty": row["issued_qty"],
				"batch_no": batch_no,
			})
		self.save()

		return {
			"rows": len(self.items),
			"needs_review": sum(1 for r in self.items if r.match_status in REVIEW_STATUSES),
			"date_unreadable": bool(data["posting_date"] and not self.posting_date),
		}

	@frappe.whitelist()
	def generate_stock_entry(self):
		self.check_permission("write")

		# Row lock: two simultaneous requests can't both pass the duplicate check.
		existing = frappe.db.sql(
			"select generated_stock_entry from `tabCVS Stock Entry Import` where name=%s for update",
			(self.name,),
		)[0][0]
		if existing and frappe.db.get_value("Stock Entry", existing, "docstatus") not in (None, 2):
			frappe.throw(
				_("Stock Entry {0} has already been generated from this document.").format(
					frappe.get_desk_link("Stock Entry", existing)
				)
			)

		if not self.source_warehouse:
			frappe.throw(_("Please select a Source Warehouse."))
		if not self.items:
			frappe.throw(_("There are no items to issue."))

		has_requested_qty = frappe.get_meta("Stock Entry Detail").has_field("requested_qty")
		se_meta = frappe.get_meta("Stock Entry")
		warnings = []

		se = frappe.new_doc("Stock Entry")
		se.update({
			"stock_entry_type": "Material Issue",
			"purpose": "Material Issue",
			"company": self.company,
			"from_warehouse": self.source_warehouse,
		})
		if self.posting_date:
			se.update({"posting_date": self.posting_date, "set_posting_time": 1})
		if se_meta.has_field("job_number"):
			se.job_number = self.job_number
		if self.opr_number:
			if se_meta.has_field("custom_opr") and frappe.db.exists("Order Processing Request", self.opr_number):
				se.custom_opr = self.opr_number
			else:
				warnings.append(_("OPR {0} was not found, so it was left blank on the Stock Entry.").format(self.opr_number))

		for row in self.items:
			stock_uom = self._validate_row(row)
			uom = row.uom or stock_uom
			if warning := item_matcher.uom_warning(row.item_code, row.uom, stock_uom):
				warnings.append(_("Row {0}: {1}").format(row.idx, warning))

			se_row = {
				"item_code": row.item_code,
				"qty": row.issued_qty,
				"uom": uom,
				"stock_uom": stock_uom,
				"conversion_factor": get_conversion_factor(row.item_code, uom)["conversion_factor"],
				"s_warehouse": self.source_warehouse,
			}
			if has_requested_qty:
				se_row["requested_qty"] = row.requested_qty
			if row.batch_no:
				se_row.update({"batch_no": row.batch_no, "use_serial_batch_fields": 1})
			se.append("items", se_row)

		se.insert()  # draft; ERPNext computes rates, valuation and batch bundles

		self.generated_stock_entry = se.name
		self.save()
		return {"stock_entry": se.name, "warnings": warnings}

	def _validate_row(self, row):
		if not row.item_code:
			frappe.throw(_("Row {0}: Please select an Item Code.").format(row.idx))
		item = frappe.db.get_value("Item", row.item_code, ["disabled", "is_stock_item", "stock_uom"], as_dict=True)
		if not item or item.disabled or not item.is_stock_item:
			frappe.throw(_("Row {0}: Item {1} does not exist, is disabled, or is not a stock item.").format(row.idx, row.item_code))
		if row.issued_qty <= 0:
			frappe.throw(_("Row {0}: Issued Qty must be greater than zero.").format(row.idx))
		if row.batch_no and frappe.db.get_value("Batch", row.batch_no, "item") != row.item_code:
			frappe.throw(_("Row {0}: Batch {1} does not belong to Item {2}.").format(row.idx, row.batch_no, row.item_code))
		return item.stock_uom


def _valid_date(value):
	try:
		return getdate(value) if value else None
	except Exception:
		return None


def _check_batch(batch_id, item_code):
	"""Only keep a printed Batch if it exists and belongs to the matched Item."""
	if not batch_id:
		return "", None
	batch_item = frappe.db.get_value("Batch", batch_id, "item")
	if not batch_item:
		return "", _('Batch "{0}" was not found. Please verify.').format(batch_id)
	if batch_item != item_code:
		return "", _('Batch "{0}" belongs to Item {1}. Please verify.').format(batch_id, batch_item)
	return batch_id, None


@frappe.whitelist()
def save_manual_match_alias(original_description, item_code, source_name=None):
	frappe.has_permission("CVS Item Matching Alias", "create", throw=True)
	item_matcher.save_alias(original_description, item_code, source_name)
