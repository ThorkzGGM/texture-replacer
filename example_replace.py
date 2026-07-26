"""
Standalone CLI example — replace textures without Telegram.

Usage:
  python example_replace.py original.bundle ./textures_folder output.bundle
"""

import sys
from pathlib import Path

from bot import list_textures, replace_textures


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python example_replace.py <bundle> <png_dir> <output>")
        sys.exit(1)

    bundle = Path(sys.argv[1])
    png_dir = Path(sys.argv[2])
    output = Path(sys.argv[3])

    print("Textures in bundle:")
    for name, w, h in list_textures(bundle):
        print(f"  {name}  ({w}×{h})")

    replacements = {
        p.stem: p for p in png_dir.glob("*.png")
    }
    if not replacements:
        print("No PNG files found in", png_dir)
        sys.exit(1)

    print("\nReplacing:", list(replacements.keys()))
    replaced = replace_textures(bundle, replacements, output)
    print(f"\nDone. Replaced {len(replaced)} texture(s) → {output}")


if __name__ == "__main__":
    main()
