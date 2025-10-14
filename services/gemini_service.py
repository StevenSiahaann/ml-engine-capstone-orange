import os
import io
import re
import json
import logging
from typing import Optional, Union, Dict, Any, List

from PIL import Image, UnidentifiedImageError
import google.generativeai as genai
from utils.prompt_template import PROMPT_TEXT

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    _handler.setFormatter(_fmt)
    logger.addHandler(_handler)
logger.setLevel(logging.DEBUG if os.getenv("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO)

_API_KEY = os.getenv("GOOGLE_API_KEY")
if not _API_KEY:
    logger.error("GOOGLE_API_KEY is not set. Please export it in environment variables.")
else:
    genai.configure(api_key=_API_KEY)

def _ok(data: Any = None, message: str = "OK") -> Dict[str, Any]:
    return {"error": False, "message": message, "data": data}

def _fail(message: str, data: Any = None) -> Dict[str, Any]:
    return {"error": True, "message": message, "data": data}

def _build_multimodal_payload(
    prompt_text: str,
    image: Optional[Image.Image] = None,
    extra_text: Optional[str] = None
) -> List[Union[str, Image.Image]]:
    parts: List[Union[str, Image.Image]] = []
    parts.append((prompt_text or "Extract the receipt data and return JSON only.").strip())
    if extra_text and extra_text.strip():
        parts.append(extra_text.strip())
    if image is not None:
        parts.append(image)
    return parts

def _extract_response_text(response) -> str:
    try:
        if hasattr(response, "candidates") and response.candidates:
            for cand in response.candidates:
                if getattr(cand, "content", None) and getattr(cand.content, "parts", None):
                    texts = []
                    for p in cand.content.parts:
                        t = getattr(p, "text", None)
                        if t:
                            texts.append(t)
                    if texts:
                        joined = "\n".join(texts).strip()
                        if joined:
                            return joined
    except Exception as e:
        logger.debug(f"_extract_response_text traverse candidates failed: {type(e).__name__}: {e}")

    if hasattr(response, "text") and isinstance(response.text, str) and response.text.strip():
        return response.text.strip()

    raise ValueError("Tidak ada teks yang bisa diambil dari response Gemini.")

def _extract_json_blob(text: str) -> str:
    cleaned = text.replace("```json", "```").strip()
    cleaned = cleaned.replace("```", "").strip()

    try:
        json.loads(cleaned)
        return cleaned
    except Exception:
        pass

    match = re.search(r'\{(?:[^{}]|(?R))*\}', cleaned, re.DOTALL)
    if match:
        return match.group(0)

    match_simple = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match_simple:
        return match_simple.group(0)

    raise ValueError("Tidak ditemukan blok JSON yang valid pada keluaran model.")

def process_receipt_image(
    image_bytes: Optional[bytes] = None,
    *,
    model_name: str = "models/gemini-2.5-flash", 
    prompt_text: Optional[str] = None,
    extra_text: Optional[str] = None
) -> Dict[str, Any]:
    if not _API_KEY:
        return _fail("GOOGLE_API_KEY tidak terpasang di environment.", None)
    if image_bytes is None and not (extra_text or prompt_text or PROMPT_TEXT):
        return _fail("Tidak ada input yang diproses. Beri image_bytes atau teks.", None)

    try:
        generation_config = {"response_mime_type": "application/json"}
        model = genai.GenerativeModel(model_name, generation_config=generation_config)
        logger.debug(f"Initialized GenerativeModel: {model_name} with JSON mime.")
    except TypeError:
        model = genai.GenerativeModel(model_name)
        logger.debug(f"Initialized GenerativeModel: {model_name} (no JSON mime).")
    except Exception as e:
        logger.error(f"Gagal inisialisasi model {model_name}: {type(e).__name__}: {e}")
        return _fail(f"Gagal inisialisasi model {model_name}.", {"exception": str(e)})

    image_pil = None
    if image_bytes is not None:
        try:
            image_pil = Image.open(io.BytesIO(image_bytes))
            image_pil.verify()  # cepat mendeteksi file corrupt
            image_pil = Image.open(io.BytesIO(image_bytes))  # reopen untuk pemakaian
            logger.debug(f"Gambar terdeteksi: mode={image_pil.mode}, size={image_pil.size}")
        except UnidentifiedImageError:
            logger.warning("Gagal mengenali file gambar (UnidentifiedImageError).")
            return _fail("Gambar tidak valid atau format tidak didukung.", None)
        except Exception as e:
            logger.error(f"Error saat membaca gambar: {type(e).__name__}: {e}")
            return _fail("Terjadi kesalahan saat membaca gambar.", {"exception": str(e)})

    final_prompt = (prompt_text or PROMPT_TEXT or "").strip()
    parts = _build_multimodal_payload(final_prompt, image=image_pil, extra_text=extra_text)
    logger.debug(f"Payload parts count: {len(parts)} | with_image={image_pil is not None} | has_extra_text={bool(extra_text)}")

    try:
        response = model.generate_content(parts)
        logger.info("Panggilan Gemini sukses.")
    except Exception as e:
        se = str(e)
        hint = None
        if "404" in se and "not found" in se.lower():
            hint = "Model tidak tersedia pada versi SDK ini. Coba model lain (mis. models/gemini-2.5-flash) atau upgrade google-generativeai."
        elif "429" in se or "rate" in se.lower():
            hint = "Kemungkinan rate limit/quota. Coba ulang beberapa saat lagi."
        elif "UNAUTHENTICATED" in se or "API key" in se:
            hint = "Periksa GOOGLE_API_KEY dan akses project."
        logger.error(f"Gagal memanggil Gemini: {type(e).__name__}: {e}")
        return _fail("Gagal memanggil layanan Gemini.", {"exception": se, "hint": hint})

    try:
        resp_text = _extract_response_text(response)
        logger.debug(f"Teks mentah dari model (panjang={len(resp_text)}).")
    except Exception as e:
        logger.error(f"Gagal ekstrak teks dari response: {type(e).__name__}: {e}")
        return _fail("Tidak ada teks yang bisa diambil dari response Gemini.", {"exception": str(e)})

    try:
        try:
            data = json.loads(resp_text)
        except json.JSONDecodeError:
            json_blob = _extract_json_blob(resp_text)
            data = json.loads(json_blob)

        logger.info("Parsing JSON berhasil.")
        return _ok(data=data, message="Berhasil ekstraksi data.")
    except json.JSONDecodeError as e:
        logger.warning(f"JSONDecodeError: {e}")
        preview = resp_text[:400]
        return _fail("Gagal mem-parsing JSON dari respons Gemini.", {"preview": preview})
    except Exception as e:
        logger.error(f"Gagal mengekstrak JSON: {type(e).__name__}: {e}")
        return _fail("Respons model tidak berisi JSON yang valid.", {"exception": str(e)})
