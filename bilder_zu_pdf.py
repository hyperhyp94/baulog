"""
bilder_zu_pdf.py — fügt mehrere Fotos (Abschnitte eines Kassenbons) zu EINEM PDF zusammen,
damit Paperless den Beleg als ein Dokument archiviert und per OCR durchsuchbar macht.

Läuft mit dem System-Python (Pillow):  python3 bilder_zu_pdf.py ziel.pdf bild1.jpg bild2.jpg …
"""

import sys

from PIL import Image, ImageOps


def main():
    ziel, quellen = sys.argv[1], sys.argv[2:]
    seiten = []
    for q in quellen:
        with Image.open(q) as im:
            seiten.append(ImageOps.exif_transpose(im).convert("RGB"))
    if not seiten:
        raise SystemExit("keine Bilder")
    seiten[0].save(ziel, "PDF", resolution=150.0, save_all=True, append_images=seiten[1:])


if __name__ == "__main__":
    main()
