"""One local entry point: python start.py [setup|play|inspect|legacy]."""

from pathlib import Path
import os
import sys


def main(argv=None):
    if sys.version_info < (3, 11):
        print("Dwarf Fortress Care Eval requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent
    # The native benchmark uses only the standard library. Running the checkout
    # directly needs no pip, virtual environment, model SDK, or package install.
    sys.path.insert(0, str(root / "src"))
    from dfeval.play_cli import main as run
    original_directory = Path.cwd()
    try:
        os.chdir(root)
        arguments = list(sys.argv[1:] if argv is None else argv)
        return run(arguments)
    finally:
        os.chdir(original_directory)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError) as exc:
        print(f"Launcher: {exc}", file=sys.stderr)
        raise SystemExit(1)
