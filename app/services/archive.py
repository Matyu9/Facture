"""Archive des PDF émis : un fichier par pièce, écrit une fois à l'émission, jamais réécrit.

C'est l'original qu'on renvoie au client et qu'on conserve dix ans (art. L123-22 du code de commerce). Son empreinte
SHA-256 est gardée en base : une altération du fichier se voit à la lecture (et dans l'état de l'installation).
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from flask import current_app


class ArchiveError(RuntimeError):
    pass


def _root():
    return Path(current_app.config["ARCHIVE_DIR"])


def _safe(part):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(part)).strip("._") or "_"


def store(invoice, content):
    """Écrit le PDF de la pièce qui vient d'être numérotée. Renvoie (chemin relatif, empreinte)."""
    relative = Path(_safe(invoice.issuer.code)) / str(invoice.sequence_year) / f"{_safe(invoice.number)}.pdf"
    target = _root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # Un fichier de même nom ne peut venir que d'une émission dont la transaction a échoué (le numéro n'a pas été
    # validé) : on le remplace. Écriture dans un fichier temporaire puis renommage : jamais de PDF à moitié écrit.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o440)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return relative.as_posix(), hashlib.sha256(content).hexdigest()


def read(invoice):
    """Contenu du PDF archivé, vérifié par son empreinte. ArchiveError s'il manque ou a été modifié."""
    if not invoice.pdf_path:
        raise ArchiveError(f"La pièce {invoice.display_number} n'a pas de PDF archivé.")
    path = (_root() / invoice.pdf_path).resolve()
    if _root().resolve() not in path.parents:
        raise ArchiveError("Chemin d'archive invalide.")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ArchiveError(f"PDF de la pièce {invoice.number} introuvable dans l'archive.") from exc
    if hashlib.sha256(content).hexdigest() != invoice.pdf_sha256:
        raise ArchiveError(
            f"Le PDF archivé de la pièce {invoice.number} a été modifié : son empreinte ne correspond plus."
        )
    return content


def filename(invoice):
    return f"{_safe(invoice.number or f'brouillon-{invoice.id}')}.pdf"
