from __future__ import unicode_literals

import frappe
import json
import base64
import os
import re
import mimetypes
import time

from frappe import _
from openai import OpenAI
from frappe.utils.file_manager import get_file_path

from cvs_stock_entry_import.cvs_stock_entry_import import item_matcher


# ===================================================
# Helpers
# ===================================================

def get_openai_client(setting_name):
	api_key = frappe.db.get_value("CVS Stock Entry Import Setting", setting_name, "gpt_key")
	model = frappe.db.get_value("CVS Stock Entry Import Setting", setting_name, "gpt_model")

	if not api_key or not model:
		frappe.throw(_("OpenAI API key or model not configured on the selected AI Setting"))

	return OpenAI(api_key=api_key), model


def parse_and_clean_json(text):
	"""Return dict or None."""
	if not text:
		return None

	cleaned = text.strip()
	cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
	cleaned = re.sub(r"\s*```\s*$", "", cleaned)
	cleaned = cleaned.strip()

	try:
		return json.loads(cleaned)
	except Exception:
		pass

	match = re.search(r"\{.*\}", cleaned, re.DOTALL)
	if match:
		try:
			return json.loads(match.group())
		except Exception:
			return None

	return None


def _safe_float(v, default=0.0):
	try:
		if v is None or v == "":
			return float(default)
		if isinstance(v, str):
			v = v.replace(",", "").strip()
		return float(v)
	except Exception:
		return float(default)


def _safe_str(v, default=""):
	if v is None:
		return default
	if isinstance(v, str):
		return v.strip()
	try:
		return str(v)
	except Exception:
		return default


def normalize_and_validate(payload):
	"""
	Ensure the AI output matches the required schema and types. Never
	invents Item Codes or ERP values here — only coerces types/shapes of
	exactly what the model returned.
	"""
	if not isinstance(payload, dict):
		return None

	result = {
		"posting_date": _safe_str(payload.get("posting_date", "")),
		"job_number": _safe_str(payload.get("job_number", "")),
		"opr_number": _safe_str(payload.get("opr_number", "")),
		"items": payload.get("items", []),
	}

	if not isinstance(result["items"], list):
		result["items"] = []

	norm_items = []
	for it in result["items"]:
		if not isinstance(it, dict):
			continue
		description = _safe_str(it.get("description", ""))
		if not description and not _safe_str(it.get("item_code_on_document", "")):
			continue
		norm_items.append({
			"item_code_on_document": _safe_str(it.get("item_code_on_document", "")),
			"description": description,
			"uom": _safe_str(it.get("uom", "")),
			"requested_qty": _safe_float(it.get("requested_qty", 0.0)),
			"issued_qty": _safe_float(it.get("issued_qty", 0.0)),
			"batch_id": _safe_str(it.get("batch_id", "")),
		})
	result["items"] = norm_items

	required = ["posting_date", "items"]
	for k in required:
		if k not in result:
			return None

	return result


def _extract_output_text_from_response(resp):
	ot = getattr(resp, "output_text", None)
	if isinstance(ot, str) and ot.strip():
		return ot.strip()

	text_parts = []
	output = getattr(resp, "output", None)
	if isinstance(output, list):
		for item in output:
			if isinstance(item, dict) and item.get("type") == "output_text":
				t = item.get("text", "")
				if t:
					text_parts.append(t)
	return ("\n".join(text_parts)).strip()


def _pdf_to_images_base64(full_path, max_pages=10, dpi=200):
	"""Convert PDF pages to PNG data URLs. PyMuPDF first, pdf2image fallback."""
	try:
		import fitz  # PyMuPDF
		doc = fitz.open(full_path)
		page_count = min(len(doc), max_pages)

		data_urls = []
		zoom = dpi / 72.0
		mat = fitz.Matrix(zoom, zoom)

		for i in range(page_count):
			page = doc.load_page(i)
			pix = page.get_pixmap(matrix=mat, alpha=False)
			png_bytes = pix.tobytes("png")
			b64 = base64.b64encode(png_bytes).decode("utf-8")
			data_urls.append(f"data:image/png;base64,{b64}")
		doc.close()
		return data_urls

	except Exception:
		pass

	try:
		from pdf2image import convert_from_path
		images = convert_from_path(full_path, dpi=dpi, first_page=1, last_page=max_pages)
		data_urls = []
		for img in images:
			from io import BytesIO
			buf = BytesIO()
			img.save(buf, format="PNG")
			b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
			data_urls.append(f"data:image/png;base64,{b64}")
		return data_urls
	except Exception as e:
		frappe.throw(_("PDF multi-page support requires PyMuPDF (fitz) or pdf2image. Error: {0}").format(str(e)))


def build_prompt():
	return """
CRITICAL INSTRUCTIONS:
1. YOU MUST OUTPUT A VALID JSON OBJECT AND NOTHING ELSE — NO MARKDOWN, NO CODE BLOCKS
2. DO NOT WRAP THE JSON IN ```json OR ANY OTHER FORMATTING
3. DO NOT INCLUDE ANY EXPLANATIONS, COMMENTS, OR EXTRA TEXT
4. ENSURE THE JSON IS PROPERLY FORMATTED AND CAN BE PARSED BY json.loads()
5. ONLY EXTRACT WHAT IS ACTUALLY PRINTED OR HANDWRITTEN ON THE DOCUMENT. NEVER INVENT, GUESS, OR INFER A VALUE.
6. IF A VALUE IS NOT PRESENT OR NOT LEGIBLE, USE THE DEFAULT (empty string "" or 0.0) — DO NOT MAKE ONE UP.

You are an expert AI data extractor for a "Material Issuance Summary" — a warehouse/stores document used to
record materials issued against a Job and an OPR (Operation/Production Request). It may be typed, handwritten,
or a mix of both, and may be a scanned photo of a paper form.

HEADER FIELDS:
- Date (posting_date): the document date, normally near the top (e.g. "01-Oct-26", "14-9-26"). Convert to
  "YYYY-MM-DD". The year may be printed as 2 digits — assume 20YY. If the month is a 3-letter name (e.g. "Oct")
  there is no ambiguity. If the date is purely numeric (e.g. "14-9-26"), interpret it as DD-MM-YY (day first),
  which is the standard order on this document. If no usable date is present, use "".
- Job No. (job_number): an alphanumeric job/work order number (e.g. "2610001", "18920"). Copy it EXACTLY as
  printed/handwritten, including any digits that look unusual — do not "correct" it.
- OPR No. (opr_number): the Order Processing Request reference (e.g. "OPR-26-02144"). Copy exactly as printed.
  If handwritten and partially illegible, extract what is legible and leave the rest out rather than guessing
  missing characters.

ITEM TABLE — one object per row in the "items" array:
- Item Code (item_code_on_document): the ERP item code if printed on the document (e.g. "2020111", "1010169").
  Many documents do NOT print an Item Code at all — in that case, leave this "". Never invent one, and never
  copy the Job No., OPR No., or Batch ID into this field by mistake.
- Description (description): the material description exactly as printed, in its original wording — e.g.
  "GI Duct Flange 20mm GI-FR20-PYROSAFE", "Coil GI G90 AGIS 0.80X1219mm". Preserve technical details EXACTLY:
  dimensions, grade (e.g. G90, G60), material (GI/SS/MS), gauge/thickness, and any brand name if printed — these
  are critical for matching this description to the correct ERP Item later and must not be paraphrased,
  reordered, or "cleaned up".
- UOM (uom): the unit of measure column (e.g. "m", "Sqm", "Pkt", "Pair", "Nos", "Kg"). Leave "" if not printed.
- Requested Qty (requested_qty): the quantity originally requested. This is DIFFERENT from Issued Qty — do not
  confuse the two columns. If only one quantity column exists on this document, treat it as issued_qty and
  leave requested_qty as 0.0.
- Issued Qty (issued_qty): the quantity actually issued/given out. If the document is handwritten and a cell
  contains only a dash "-" or is blank, treat it as 0.0 (not requested — an actual issued quantity of zero).
- Batch ID (batch_id): the batch/lot identifier if printed (e.g. "D0031858X0", "1007"). Leave "" if not shown.

ROWS TO SKIP (not items):
- Column header rows, section dividers, page totals, "Requested by / Issued by / Entered by / Date Entered"
  signature/footer rows, and completely empty rows with no description and no item code.

HANDWRITING:
- This document is sometimes handwritten over a printed template. Read handwritten entries carefully; if a
  handwritten value is genuinely illegible, leave that specific field "" / 0.0 rather than guessing.

REQUIRED JSON STRUCTURE (MUST MATCH EXACTLY):
{
  "posting_date": "YYYY-MM-DD",
  "job_number": "",
  "opr_number": "",
  "items": [
    {
      "item_code_on_document": "",
      "description": "First material description exactly as printed",
      "uom": "",
      "requested_qty": 0.0,
      "issued_qty": 0.0,
      "batch_id": ""
    }
  ]
}
""".strip()


def _validate_batch(batch_id, item_code):
	"""
	Never sets a Batch that doesn't exist or doesn't belong to the matched
	Item — surfaces a note for manual review instead (section 27).
	"""
	if not batch_id:
		return "", None
	if not item_code:
		return "", f'Batch "{batch_id}" was printed but no Item was matched yet — please verify the batch after selecting an Item.'

	batch_item = frappe.db.get_value("Batch", batch_id, "item")
	if not batch_item:
		return "", f'Batch "{batch_id}" was printed but does not exist in the Batch master — please verify.'
	if batch_item != item_code:
		return "", f'Batch "{batch_id}" exists but belongs to a different Item ({batch_item}) — please verify.'
	return batch_id, None


def _log_debug(title, details):
	try:
		frappe.log_error(details, title)
	except Exception:
		pass


# ===================================================
# MAIN METHOD (Frappe API)
# ===================================================

@frappe.whitelist()
def extract_material_issuance(file_path, setting_name):
	"""
	Extract header + item rows from a Material Issuance Summary (PDF/image)
	using OpenAI vision, then run every row through the deterministic Item
	matcher before returning. The AI never selects or invents an Item Code —
	item_matcher only ever returns Item Codes validated against the live
	Item master.
	"""
	try:
		client, model = get_openai_client(setting_name)

		full_path = get_file_path(file_path)
		if not full_path or not os.path.exists(full_path):
			frappe.throw(_("Material Issuance Summary file not found"))

		prompt = build_prompt()
		ext = os.path.splitext(full_path)[1].lower()

		content_blocks = []
		if ext in [".jpg", ".jpeg", ".png"]:
			mime = mimetypes.guess_type(full_path)[0] or "image/jpeg"
			with open(full_path, "rb") as f:
				b64 = base64.b64encode(f.read()).decode("utf-8")
			content_blocks.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
		elif ext == ".pdf":
			data_urls = _pdf_to_images_base64(full_path, max_pages=10, dpi=200)
			for url in data_urls:
				content_blocks.append({"type": "input_image", "image_url": url})
		else:
			return {"status": 0, "error": f"Unsupported file type: {ext}"}

		max_retries = 3
		last_error = ""
		active_prompt = prompt

		for attempt in range(1, max_retries + 1):
			try:
				messages = [{
					"role": "user",
					"content": [{"type": "input_text", "text": active_prompt}] + content_blocks,
				}]
				resp = client.responses.create(model=model, input=messages, temperature=0)
				output_text = _extract_output_text_from_response(resp)

				_log_debug(
					"Material Issuance Vision OCR Raw Output (truncated)",
					f"Attempt: {attempt}\nModel: {model}\nOutput (first 4000 chars):\n{(output_text or '')[:4000]}"
				)

				parsed = parse_and_clean_json(output_text)
				if not parsed:
					last_error = "Invalid JSON returned"
					if attempt < max_retries:
						active_prompt = (
							"YOUR PREVIOUS OUTPUT WAS NOT VALID JSON. Return ONLY ONE VALID JSON OBJECT "
							"that matches the required schema. No markdown, no comments, no extra text.\n\n" + prompt
						)
						time.sleep(0.3)
						continue
					break

				normalized = normalize_and_validate(parsed)
				if not normalized:
					last_error = "Normalization/validation failed"
					if attempt < max_retries:
						active_prompt = (
							"YOUR PREVIOUS JSON DID NOT MATCH THE REQUIRED SCHEMA/TYPES. Return ONLY ONE VALID "
							"JSON OBJECT EXACTLY matching the schema.\n\n" + prompt
						)
						time.sleep(0.3)
						continue
					break

				# ---- Item matching: deterministic, never invented ----
				matched_items = []
				for row in normalized["items"]:
					match = item_matcher.match_row(
						row["item_code_on_document"], row["description"], row["uom"]
					)
					batch_no, batch_note = _validate_batch(row["batch_id"], match.get("item_code"))
					if batch_note:
						existing_notes = match.get("match_notes") or ""
						match["match_notes"] = (existing_notes + " " + batch_note).strip()
					matched_items.append({**row, **match, "batch_no": batch_no})

				return {"status": 1, "data": {**normalized, "items": matched_items}}

			except Exception as e:
				last_error = str(e)
				_log_debug("Material Issuance OCR Attempt Error", f"Attempt: {attempt}\nError: {last_error}")
				if attempt < max_retries:
					time.sleep(0.3)
					continue

		frappe.throw(_("Extraction failed after retries. Last error: {0}").format(last_error))

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Material Issuance OCR Error")
		return {"status": 0, "error": str(e)}
