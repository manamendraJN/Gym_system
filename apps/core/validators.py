from django import forms
from PIL import Image

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_DOC_BYTES = 5 * 1024 * 1024


def validate_image_upload(upload):
    """Newly uploaded images only: size cap and real image format (Pillow has already parsed the file)."""
    if upload and hasattr(upload, "image"):
        if upload.size > MAX_IMAGE_BYTES:
            raise forms.ValidationError("The image must be 2 MB or smaller.")
        if upload.image.format not in {"JPEG", "PNG", "WEBP"}:
            raise forms.ValidationError("Use a JPEG, PNG or WebP image.")
    return upload


def validate_document_upload(upload):
    """PDF / JPEG / PNG up to 5 MB; the content must match, not just the file name."""
    if not upload or not hasattr(upload, "size"):
        return upload
    if upload.size > MAX_DOC_BYTES:
        raise forms.ValidationError("The file must be 5 MB or smaller.")
    head = upload.read(8)
    upload.seek(0)
    name = upload.name.lower()
    if name.endswith(".pdf"):
        if not head.startswith(b"%PDF"):
            raise forms.ValidationError("This file is not a valid PDF.")
    elif name.endswith((".jpg", ".jpeg", ".png")):
        try:
            Image.open(upload).verify()
        except Exception:
            raise forms.ValidationError("This file is not a valid image.")
        finally:
            upload.seek(0)
    else:
        raise forms.ValidationError("Upload a PDF, JPEG or PNG file.")
    return upload
