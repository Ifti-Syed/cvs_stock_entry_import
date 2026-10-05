"""AI extraction of a Material Issuance Summary (PDF/image) into structured JSON."""

import base64
import json
import mimetypes
import os
import re
import time

import frappe
from frappe import _
from frappe.utils.file_manager import get_file_path
from openai import OpenAI

MAX_ATTEMPTS = 3
MAX_PDF_PAGES = 10
PDF_DPI = 200

PROMPT = """
Extract data from this "Material Issuance Summary" (a stores document recording materials issued
against a Job and an OPR). It may be printed, handwritten, or both, and may be a photo of paper.

Return ONLY one valid JSON object (no markdown, no comments). Extract ONLY what is printed or written.
Never invent, guess, or infer. If a value is missing or illegible, use "" for text and 0 for numbers.

HEADER:
- posting_date: document date as "YYYY-MM-DD". Two-digit years are 20YY. Numeric dates are
  day-first (e.g. "14-9-26" = 2026-09-14). "" if absent.
- job_number: Job No., copied exactly.
- opr_number: OPR No. (e.g. "OPR-26-02144"), copied exactly. Do not fill in illegible characters.

ITEMS (one object per table row):
- item_code_on_document: Item Code only if printed (e.g. "2020111"), else "". Never copy the Job No.,
  OPR No., or Batch ID here.
- description: exactly as written. Preserve dimensions, grade (G90), material (GI/SS/MS),
  thickness and brand exactly; do not reorder or clean up.
- uom: unit as written (e.g. "m", "Sqm", "Pkt", "Nos"), else "".
- requested_qty: Requested Qty column. If the document has only one quantity column, put it in
  issued_qty and use 0 here.
- issued_qty: Issued Qty column. A dash "-" or blank cell is 0.
- batch_id: Batch ID exactly as written, else "".

Skip header rows, totals, signature/footer rows (Requested by, Issued by, Entered by, Date Entered),
and empty rows.

{
  "posting_date": "",
  "job_number": "",
  "opr_number": "",
  "items": [
    {"item_code_on_document": "", "description": "", "uom": "", "requested_qty": 0, "issued_qty": 0, "batch_id": ""}
  ]
}
""".strip()


def _supports_temperature(model):
	# GPT-5 and later are reasoning models that reject `temperature`.
	return model.lower().startswith(("gpt-4o", "gpt-4.1", "gpt-4-"))


def _str(v):
	return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def _float(v):
	try:
		return float(str(v).replace(",", "").strip()) if v not in (None, "") else 0.0
	except ValueError:
		return 0.0


def _parse_json(text):
	text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (text or "").strip(), flags=re.IGNORECASE).strip()
	for candidate in (text, (re.search(r"\{.*\}", text, re.DOTALL) or [None])[0]):
		if candidate:
			try:
				data = json.loads(candidate)
				if isinstance(data, dict):
					return data
			except ValueError:
				pass
	return None


def _normalize(payload):
	items = payload.get("items") if isinstance(payload.get("items"), list) else []
	return {
		"posting_date": _str(payload.get("posting_date")),
		"job_number": _str(payload.get("job_number")),
		"opr_number": _str(payload.get("opr_number")),
		"items": [
			{
				"item_code_on_document": _str(it.get("item_code_on_document")),
				"description": _str(it.get("description")),
				"uom": _str(it.get("uom")),
				"requested_qty": _float(it.get("requested_qty")),
				"issued_qty": _float(it.get("issued_qty")),
				"batch_id": _str(it.get("batch_id")),
			}
			for it in items
			if isinstance(it, dict) and (_str(it.get("description")) or _str(it.get("item_code_on_document")))
		],
	}


def _image_blocks(file_url):
	path = get_file_path(file_url)
	if not path or not os.path.exists(path):
		frappe.throw(_("Material Issuance Summary file not found."))

	ext = os.path.splitext(path)[1].lower()
	if ext in (".jpg", ".jpeg", ".png"):
		mime = mimetypes.guess_type(path)[0] or "image/jpeg"
		with open(path, "rb") as f:
			urls = [f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"]
	elif ext == ".pdf":
		import fitz  # PyMuPDF

		zoom = fitz.Matrix(PDF_DPI / 72, PDF_DPI / 72)
		with fitz.open(path) as pdf:
			urls = [
				"data:image/png;base64," + base64.b64encode(pdf[i].get_pixmap(matrix=zoom).tobytes("png")).decode()
				for i in range(min(len(pdf), MAX_PDF_PAGES))
			]
	else:
		frappe.throw(_("Unsupported file type {0}. Please upload a PDF, JPG or PNG.").format(ext))

	return [{"type": "input_image", "image_url": url} for url in urls]


def extract_document(file_url, setting_name):
	setting = frappe.get_doc("CVS Stock Entry Import Setting", setting_name)
	api_key = setting.get_password("gpt_key", raise_exception=False)
	if not api_key or not setting.gpt_model:
		frappe.throw(_("Please set the GPT API Key and Model on AI Setting {0}.").format(setting_name))

	client = OpenAI(api_key=api_key)
	images = _image_blocks(file_url)
	request = {"model": setting.gpt_model}
	if _supports_temperature(setting.gpt_model):
		request["temperature"] = 0

	prompt, last_error, output = PROMPT, "", ""
	for attempt in range(1, MAX_ATTEMPTS + 1):
		try:
			resp = client.responses.create(
				input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}, *images]}], **request
			)
			output = resp.output_text or ""
			data = _parse_json(output)
			if data:
				return _normalize(data)
			last_error = "The AI response was not valid JSON."
			prompt = "Your previous reply was not valid JSON. Reply with ONLY the JSON object.\n\n" + PROMPT
		except Exception as e:
			last_error = str(e)
		if attempt < MAX_ATTEMPTS:
			time.sleep(0.5)

	frappe.log_error(
		title="CVS Stock Entry Import: extraction failed",
		message=f"Model: {setting.gpt_model}\nError: {last_error}\nLast output:\n{output[:4000]}",
	)
	frappe.throw(_("AI extraction failed after {0} attempts: {1}").format(MAX_ATTEMPTS, last_error))
