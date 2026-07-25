from argparse import ArgumentParser
from pathlib import Path
import sys

PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(PROJECT_SRC))

from quote_app.excel.template_builder import build_template  # noqa: E402


def main() -> None:
    parser = ArgumentParser(description="Build the clean quotation workbook template.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    build_template(arguments.source, arguments.destination)


if __name__ == "__main__":
    main()
