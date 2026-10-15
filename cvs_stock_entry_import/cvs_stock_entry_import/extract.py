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
Never invent, guess, or infer. If a text value is missing or illegible, use "". If a quantity is
illegible, use null: null means "could not read it" and is NOT the same as 0.

HEADER:
- posting_date: document date as "YYYY-MM-DD". Two-digit years are 20YY. Numeric dates are
  day-first (e.g. "14-9-26" = 2026-09-14). "" if absent.
- job_number: Job No., copied exactly.
- opr_number: OPR No. (e.g. "OPR-26-02144"), copied exactly. Do not fill in illegible characters.

ITEMS (one object per table row):
- item_code_on_document: Item Code only if printed (e.g. "2020111"), else "". Never copy the Job No.,
  OPR No., or Batch ID here.
- description: EVERY word exactly as written, in the written order (e.g. "Coil GI JSW 0.6mm" must not
  become "G.I Coil 0.6mm"). Never drop a word: brand names (e.g. JSW, AGIS), grade (G90), material
  (GI/SS/MS), type and every dimension are what identify the Item. Do not reorder or clean up.
- uom: unit as written (e.g. "m", "Sqm", "Pkt", "Nos"), else "".
- requested_qty: Requested Qty column. If the document has only one quantity column, put it in
  issued_qty and use 0 here.
- issued_qty: Issued Qty column. A dash "-" or an empty cell is 0; an illegible one is null.
- batch_id: Batch ID exactly as written (letters and digits, e.g. "VG1A0A0498"; take care to tell
  0/O and 1/I apart), else "".

{brands}Skip header rows, totals, signature/footer rows (Requested by, Issued by, Entered by, Date Entered),
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


def _quantity(v):
	"""A quantity as a float, or None when it could not be read (null, missing, not a number, negative).
	A dash is a real 0 (nothing issued), which is different from unreadable."""
	if isinstance(v, bool) or v is None:
		return None
	if isinstance(v, (int, float)):
		return float(v) if v >= 0 else None
	text = str(v).replace(",", "").strip()
	if text in ("-", "\u2013", "\u2014"):
		return 0.0
	try:
		value = float(text)
	except ValueError:
		return None
	return value if value >= 0 else None


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
	"""Validate the model's JSON against the expected shape and return clean values.

	Raises ValueError (so the caller retries) if the structure is wrong. Per item, "unreadable" lists the
	quantity fields that were null or not a number, so they are never mistaken for a real 0.
	"""
	if not isinstance(payload.get("items"), list):
		raise ValueError('"items" must be a list of rows.')
	for key in ("posting_date", "job_number", "opr_number"):
		if not isinstance(payload.get(key, ""), (str, int, float, type(None))):
			raise ValueError(f'"{key}" must be text.')

	items = []
	for it in payload["items"]:
		if not isinstance(it, dict):
			raise ValueError("Every entry in \"items\" must be an object.")
		if not (_str(it.get("description")) or _str(it.get("item_code_on_document"))):
			continue
		row = {
			"item_code_on_document": _str(it.get("item_code_on_document")),
			"description": _str(it.get("description")),
			"uom": _str(it.get("uom")),
			"requested_qty": _quantity(it.get("requested_qty")),
			"issued_qty": _quantity(it.get("issued_qty")),
			"batch_id": _str(it.get("batch_id")),
		}
		row["unreadable"] = [k for k in ("requested_qty", "issued_qty") if row[k] is None]
		for k in row["unreadable"]:
			row[k] = 0.0
		items.append(row)

	return {
		"posting_date": _str(payload.get("posting_date")),
		"job_number": _str(payload.get("job_number")),
		"opr_number": _str(payload.get("opr_number")),
		"items": items,
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

	brands = frappe.get_all("Brand", pluck="name", limit_page_length=200)
	hint = (
		"BRAND SPELLINGS (a spelling guide only): "
		+ ", ".join(brands)
		+ ".\nIf a handwritten word clearly looks like one of these, write it with that spelling. "
		"Never add a brand that is not written on the document.\n\n"
	) if brands else ""
	base_prompt = PROMPT.replace("{brands}", hint)
	prompt, last_error, output = base_prompt, "", ""
	for attempt in range(1, MAX_ATTEMPTS + 1):
		try:
			resp = client.responses.create(
				input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}, *images]}], **request
			)
			output = resp.output_text or ""
			data = _parse_json(output)
			if data:
				try:
					return _normalize(data)
				except ValueError as e:
					last_error = f"The AI response did not match the expected format: {e}"
					prompt = f"Your previous reply did not match the required format ({e}). Reply with ONLY the JSON object.\n\n" + base_prompt
			else:
				last_error = "The AI response was not valid JSON."
				prompt = "Your previous reply was not valid JSON. Reply with ONLY the JSON object.\n\n" + base_prompt
		except Exception as e:
			last_error = str(e)
		if attempt < MAX_ATTEMPTS:
			time.sleep(0.5)

	frappe.log_error(
		title="CVS Stock Entry Import: extraction failed",
		message=f"Model: {setting.gpt_model}\nError: {last_error}\nLast output:\n{output[:4000]}",
	)
	frappe.throw(_("AI extraction failed after {0} attempts: {1}").format(MAX_ATTEMPTS, last_error))
