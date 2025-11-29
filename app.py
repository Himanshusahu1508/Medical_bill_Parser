# app/invoice_api.py
import os
import io
import json
import base64
import tempfile
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import fitz  # pymupdf
from PIL import Image, ImageEnhance, ImageOps
from rapidfuzz import fuzz

# optional: load .env locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# genai import is optional - robust checks below
try:
    import google.generativeai as genai
except Exception:
    genai = None

POPPLER_BIN = os.environ.get("POPPLER_BIN", "")  # not used with pymupdf
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
PDF_RENDER_DPI = int(os.environ.get("PDF_RENDER_DPI", "150"))

if genai and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception:
        pass

app = FastAPI(title="Invoice Extractor (no tesseract, pymupdf)")

class ExtractRequest(BaseModel):
    document: str

def download_pdf(url: str, timeout: int = 30) -> str:
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"download failed: {e}")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(r.content); tmp.close()
    return tmp.name

def pdf_to_pil_images(pdf_path: str, dpi: int = PDF_RENDER_DPI) -> List[Image.Image]:
    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72.0  # scale factor (72 is the default PDF point/dpi)
    mat = fitz.Matrix(zoom, zoom)
    for page in doc:
        pix = page.get_pixmap(matrix=mat, alpha=False)
        mode = "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    return images

def enhance_image(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    img = ImageOps.autocontrast(img)
    img = ImageEnhance.Sharpness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.03)
    return img

def encode_image_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

# (Include your robust ask_llm_extract function here — same as earlier safe wrapper)
def ask_llm_extract(images_b64: List[str]) -> Dict[str, Any]:
    if genai is None or not GEMINI_API_KEY:
        return {"pages": [], "issues": ["genai SDK not installed or GEMINI_API_KEY missing"]}
    PROMPT = (
        "You are an invoice extraction assistant. For each page image provided, "
        "extract line-items (item_name, item_quantity default 1, item_rate if available, item_amount). "
        "Return JSON: {\"pages\":[{\"page_no\":\"1\",\"line_items\":[{...}]}], \"issues\":[] }"
    )
    parts = [PROMPT] + [{"mime_type":"image/jpeg","data":b64} for b64 in images_b64]
    # try generate_content and fallback to genai.generate, safe parse like before
    try:
        model = genai.GenerativeModel(GEMINI_MODEL, generation_config={"response_mime_type":"application/json"})
        resp = model.generate_content(parts)
        raw = getattr(resp, "text", None)
        if raw:
            return json.loads(raw)
        if isinstance(resp, (dict, list)):
            return resp
        if hasattr(resp, "json"):
            return resp.json()
    except Exception:
        try:
            resp2 = genai.generate(parts=parts, model=GEMINI_MODEL, response_mime_type="application/json")
            raw2 = getattr(resp2, "text", None) or resp2
            if isinstance(raw2, str):
                return json.loads(raw2)
            if isinstance(raw2, (dict, list)):
                return raw2
        except Exception as e:
            return {"pages": [], "issues": [f"LLM call failed: {e}"]}
    return {"pages": [], "issues": ["LLM returned unexpected response"]}

def dedupe_items(items: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    used=set(); out=[]
    for it in items:
        k=( (it.get("item_name") or "").strip().lower(), round(float(it.get("item_amount") or 0),2) )
        if k in used: continue
        used.add(k); out.append(it)
    return out

@app.post("/extract-bill-data")
async def extract_bill_data(req: ExtractRequest):
    doc=req.document.strip()
    if doc.lower().startswith(("http://","https://")):
        pdf = download_pdf(doc)
    elif os.path.exists(doc):
        pdf = doc
    else:
        raise HTTPException(status_code=400, detail="provide URL or local path")
    try:
        pages = pdf_to_pil_images(pdf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pdf render failed: {e}")
    b64s=[]; pagewise=[]
    for i,pg in enumerate(pages, start=1):
        pg2=enhance_image(pg); b64s.append(encode_image_b64(pg2))
        pagewise.append({"page_no":str(i),"page_type":"Bill Detail","bill_items":[]})
    llm_out = ask_llm_extract(b64s)
    extracted=[]
    for p in llm_out.get("pages",[]):
        pn=p.get("page_no","1"); items=p.get("line_items",[])
        for pg in pagewise:
            if pg["page_no"]==pn: pg["bill_items"]=items
        for it in items:
            extracted.append({"item_name":it.get("item_name"),"item_quantity":it.get("item_quantity"),"item_rate":it.get("item_rate"),"item_amount":it.get("item_amount"),"_page_no":pn})
    cleaned = dedupe_items(extracted)
    total = round(sum(float(x.get("item_amount") or 0) for x in cleaned),2)
    return {"is_success":True,"data":{"pagewise_line_items":pagewise,"unique_line_items":cleaned,"total_items_count":len(cleaned),"sum_total":total,"issues":llm_out.get("issues",[]) }}

# Simple upload route (optional)
@app.post("/extract-bill-data/upload")
async def upload(file: UploadFile = File(...)):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1])
    tmp.write(await file.read()); tmp.close()
    return await extract_bill_data(ExtractRequest(document=tmp.name))
