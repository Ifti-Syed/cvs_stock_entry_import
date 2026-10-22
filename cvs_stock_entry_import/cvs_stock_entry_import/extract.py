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

HEADER (handwritten, copy exactly as written):
- posting_date: document date as "YYYY-MM-DD". Two-digit years are 20YY. Numeric dates are
  day-first (e.g. "14-9-26" = 2026-09-14, "17/10/2026" = 2026-10-17). "" if absent.
- job_number: Job No., copied digit by digit (e.g. "2610065").
- opr_number: OPR No., copied exactly as written, including leading zeros. OPR numbers look like
  "OPR-26-02901" (OPR-YY-NNNNN), but only the digits may be written after the printed "OPR No :"
  (e.g. "02901" or "26-02901"). Do not add or fill in characters that are not written.

ITEMS:
The table is often PRE-PRINTED with dozens of Items (Item Code, Description and UOM already printed in
every row). The store person then WRITES in the Requested Qty, Issued Qty and Batch ID columns only for
the rows that were actually issued.
- Return ONLY the rows where at least one of Requested Qty, Issued Qty or Batch ID has something written
  in it. A row that only has an Item Code / Description / UOM, with those three columns empty, was NOT
  issued: leave it out completely, however many such rows there are. Do not list the whole table.
- The sheet may be photographed at an angle, so handwriting can sit slightly above or below the printed
  text of its row. Follow the table lines to decide which row a handwritten value belongs to; never shift
  a value to a neighbouring row.
Each returned row:
- item_code_on_document: the number in the Item Code column of that row, copied digit by digit
  (e.g. "1010177"). Always fill it when the column has a code; "" only when that cell is empty. Never
  copy the Job No., OPR No. or Batch ID here.
- description: EVERY word of that row's Description exactly as written or printed, in the same order
  (e.g. "Coil GI JSW 0.6mm" must not become "G.I Coil 0.6mm"). Never drop a word: brand names (e.g. JSW,
  AGIS, Hadeed), grade (G90), material (GI/SS/MS), type and every dimension identify the Item.
- uom: the UOM of that row as printed or written (e.g. "m", "Sqm", "Pkt", "Nos"), else "".
- requested_qty: the number in the Requested Qty column. If words are written instead of a number
  (e.g. "OFF cut"), use null. Empty = 0.
- issued_qty: the number in the Issued Qty column, as a number only even if a unit is written with it
  ("200M" -> 200, "35 m" -> 35). A dash "-" or an empty cell is 0; an illegible one is null.
- batch_id: the Batch ID written in that row, copied character by character exactly as written (typically
  a long number such as "1626290063", or letters and digits). Never shorten, "fix" or reformat it; tell
  0/O and 1/I apart carefully. "" if that cell is empty.

{brands}Skip header rows, totals and signature/footer rows (Requested by, Issued by, Entered by, Date Entered, STE Ref).

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
	m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)\s*[a-z]{0,4}\.?", text, re.IGNORECASE)  # "35", "200M", "35 m"
	return float(m.group(1)) if m else None


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
		# A printed Item Code / Description with nothing written in Requested, Issued or Batch ID was not issued.
		# (An unreadable cell counts as written: something is there, it just could not be read.)
		if not row["unreadable"] and not row["requested_qty"] and not row["issued_qty"] and not row["batch_id"]:
			continue
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
