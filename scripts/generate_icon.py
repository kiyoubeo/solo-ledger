"""Generate multi-resolution Windows .ico icon for SoloLedger desktop application."""

from pathlib import Path

from PIL import Image, ImageDraw


def generate_sololedger_ico(output_path: Path) -> Path:
    """Render a crisp 256x256 vector-style ledger + checkmark badge and save multi-size .ico."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded royal-blue background card
    draw.rounded_rectangle(
        [(12, 12), (244, 244)],
        radius=52,
        fill=(30, 64, 175, 255),
        outline=(59, 130, 246, 255),
        width=6,
    )

    # White receipt / ledger sheet
    draw.rounded_rectangle(
        [(56, 44), (192, 206)],
        radius=16,
        fill=(255, 255, 255, 255),
    )

    # Header accent bar on ledger sheet
    draw.rounded_rectangle(
        [(74, 66), (174, 84)],
        radius=6,
        fill=(37, 99, 235, 255),
    )

    # Ledger rows
    for y_top, row_w in ((104, 88), (128, 72), (152, 60)):
        draw.rounded_rectangle(
            [(74, y_top), (74 + row_w, y_top + 11)],
            radius=5,
            fill=(148, 163, 184, 255),
        )

    # Emerald tax-compliance checkmark badge at bottom-right
    badge_box = [(140, 140), (222, 222)]
    draw.ellipse(badge_box, fill=(16, 185, 129, 255), outline=(255, 255, 255, 255), width=6)
    draw.line(
        [(162, 182), (176, 196), (202, 166)],
        fill=(255, 255, 255, 255),
        width=11,
        joint="curve",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    icon_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(output_path, format="ICO", sizes=icon_sizes)
    return output_path


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    target = repo_root / "assets" / "sololedger.ico"
    saved = generate_sololedger_ico(target)
    print(f"Generated icon: {saved} ({saved.stat().st_size} bytes)")
