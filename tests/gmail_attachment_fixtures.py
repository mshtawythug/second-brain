"""Synthetic Gmail messages with receipt attachments, and a fake ``gws`` (RM-45).

Everything is built from the named constants below: invented merchants at
``example.com`` addresses, the documented test card number, and ids that are
plainly fake. Nothing here is copied from a real mailbox.
"""
from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MERCHANT = "Example Hardware Co."
MERCHANT_ADDRESS = "1 Example Way, Exampleville"
SHOP_FROM = "Example Hardware <receipts@shop.example.com>"
OWNER_TO = "Owner <owner@example.com>"
TEST_PAN = "4111 1111 1111 1111"
RECEIPT_LINES = (MERCHANT, MERCHANT_ADDRESS, f"Card {TEST_PAN}", "Total 12.34")
SUBJECT = "Your Example Hardware receipt"


def b64url(data: bytes) -> str:
    """``data`` as the Gmail API encodes ``body.data``: base64url, unpadded."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def receipt_pdf(
    lines: Iterable[str] = RECEIPT_LINES, *, pages: int = 1, encrypt: str | None = None
) -> bytes:
    """A text-layer PDF holding ``lines`` on each of ``pages`` pages (reportlab)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, encrypt=encrypt)
    for _ in range(pages):
        y = 720
        for line in lines:
            pdf.drawString(72, y, line)
            y -= 18
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def receipt_png(width: int = 40, height: int = 20) -> bytes:
    """A small blank PNG (Pillow); the tests fake the OCR engine that reads it."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (width, height), color=255).save(buffer, format="PNG")
    return buffer.getvalue()


def attachment_part(
    part_id: str,
    filename: str,
    mime: str,
    *,
    size: int,
    attachment_id: str | None = None,
    inline: bytes | None = None,
    disposition: str | None = "attachment",
    content_id: str | None = None,
) -> dict[str, Any]:
    """One MIME part as ``users.messages.get`` returns it."""
    headers = []
    if disposition is not None:
        headers.append({"name": "Content-Disposition", "value": f'{disposition}; filename="x"'})
    if content_id is not None:
        headers.append({"name": "Content-ID", "value": content_id})
    body: dict[str, Any] = {"size": size}
    if attachment_id is not None:
        body["attachmentId"] = attachment_id
    if inline is not None:
        body["data"] = b64url(inline)
    return {
        "partId": part_id,
        "mimeType": mime,
        "filename": filename,
        "headers": headers,
        "body": body,
    }


def message_with_parts(
    message_id: str,
    thread_id: str,
    parts: list[dict[str, Any]],
    *,
    internal_date: str = "1767225600000",
    labels: Iterable[str] = ("Label_receipts",),
    text: str = "Thanks for your order.",
    subject: str = SUBJECT,
) -> dict[str, Any]:
    """A ``multipart/mixed`` message: a text body part, then ``parts``."""
    text_part = {
        "partId": "0",
        "mimeType": "text/plain",
        "filename": "",
        "headers": [],
        "body": {"size": len(text), "data": b64url(text.encode())},
    }
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "internalDate": internal_date,
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": SHOP_FROM},
                {"name": "To", "value": OWNER_TO},
                {"name": "Date", "value": "Thu, 01 Jan 2026 00:00:00 +0000"},
            ],
            "body": {"size": 0},
            "parts": [text_part, *parts],
        },
    }


def nested_multipart(depth: int, leaf: dict[str, Any]) -> dict[str, Any]:
    """``leaf`` wrapped in ``depth`` levels of ``multipart/mixed``."""
    node = leaf
    for level in range(depth):
        node = {
            "partId": f"9.{level}",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [],
            "body": {"size": 0},
            "parts": [node],
        }
    return node


@dataclass
class FakeGws:
    """Serves ``messages list``, ``messages get`` and ``messages attachments get``.

    ``attachments`` maps ``(message id, attachment id)`` to the bytes to return, or to
    a ``str`` returned verbatim as stdout (a malformed answer). ``calls`` records argv.
    """

    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    attachments: dict[tuple[str, str], bytes | str] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        params = json.loads(argv[argv.index("--params") + 1])
        if argv[:5] == ["gws", "gmail", "users", "messages", "list"]:
            stubs = [{"id": m["id"], "threadId": m["threadId"]} for m in self.messages.values()]
            return json.dumps({"messages": stubs})
        if argv[:5] == ["gws", "gmail", "users", "messages", "get"]:
            return json.dumps(self.messages[params["id"]])
        if argv[:6] == ["gws", "gmail", "users", "messages", "attachments", "get"]:
            answer = self.attachments[(params["messageId"], params["id"])]
            if isinstance(answer, str):
                return answer
            return json.dumps({"size": len(answer), "data": b64url(answer)})
        raise AssertionError(f"unexpected gws call: {argv[:6]}")

    def attachment_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[4:6] == ["attachments", "get"]]
