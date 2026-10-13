"""Read bounded PDF attachments with Poppler, without passing bot credentials to the parser."""
import os
from pathlib import Path
import re
import subprocess
import tempfile

MAX_PAGES = 60
MAX_CHARS = 120_000
MAX_PREVIEWS = 4


class PDFUnreadable(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def read_pdf(data):
    """Return all extracted text and up to four labeled page previews; never pretend a scan was read."""
    if not data.lstrip().startswith(b"%PDF-"):
        raise PDFUnreadable("invalid_pdf")
    with tempfile.TemporaryDirectory(prefix="pdf-input-") as directory:
        root = Path(directory)
        options = {}
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            import pwd
            account = pwd.getpwnam("sandbox")
            os.chown(root, account.pw_uid, account.pw_gid)
            options = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
        source = root / "source.pdf"
        source.write_bytes(data)
        source.chmod(0o644)

        def run(*args, timeout=30):
            if os.name == "posix":
                args = ("prlimit", "--as=805306368", "--fsize=8388608", "--cpu=30", "--", *args)
            return subprocess.run(args, cwd=root, capture_output=True, check=True, timeout=timeout,
                                  env={"PATH": os.defpath, "HOME": str(root), "LANG": "C.UTF-8"}, **options)

        try:
            info = run("pdfinfo", str(source)).stdout.decode("utf-8", "replace")
            match = re.search(r"^Pages:\s+(\d+)", info, re.M)
            if not match:
                raise PDFUnreadable("invalid_pdf")
            pages = int(match[1])
            if not 0 < pages <= MAX_PAGES:
                raise PDFUnreadable("pdf_limit")
            text_path = root / "text.txt"
            run("pdftotext", "-layout", "-enc", "UTF-8", str(source), str(text_path), timeout=45)
            if text_path.stat().st_size > MAX_CHARS * 4:
                raise PDFUnreadable("pdf_limit")
            text = text_path.read_text(encoding="utf-8", errors="replace")
            if len(text) > MAX_CHARS:
                raise PDFUnreadable("pdf_limit")
            if sum(c.isalnum() for c in text) < 80:
                raise PDFUnreadable("no_pdf_text")
        except (OSError, subprocess.SubprocessError) as exc:
            raise PDFUnreadable("invalid_pdf") from exc

        chunks = text.split("\f")
        text = "\n\n".join(f"[PDF page {i}/{pages}]\n{chunk.strip()}"
                           for i, chunk in enumerate(chunks[:pages], 1) if chunk.strip())
        selected = list(range(1, pages + 1)) if pages <= MAX_PREVIEWS else [
            1 + round(i * (pages - 1) / (MAX_PREVIEWS - 1)) for i in range(MAX_PREVIEWS)]
        previews = []
        for page in selected:
            prefix = root / f"page-{page}"
            try:
                run("pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1568",
                    "-jpeg", "-jpegopt", "quality=80", str(source), str(prefix))
                previews.append((page, prefix.with_suffix(".jpg").read_bytes()))
            except (OSError, subprocess.SubprocessError):
                pass  # The complete text is still usable; missing previews are not claimed as seen.
        return {"text": text, "pages": pages, "previews": previews}
