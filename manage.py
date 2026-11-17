"""Command-line entry point for project management tasks."""

import argparse
from pathlib import Path

from generator import DEFAULT_OUTPUT_DIR, DEFAULT_SEED, write_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Herramientas del demo de pedidos")
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", help="Generar CSV crudo y manifiesto")
    generate_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    if args.command == "generate":
        manifest = write_dataset(args.output_dir, args.seed)
        print(f"Pedidos originales: {manifest['original_order_count']}")
        print(f"Filas totales: {manifest['total_csv_row_count']}")
        print(f"Inyecciones: {manifest['injection_counts']}")
        print(f"CSV y manifiesto: {args.output_dir.resolve()}")
        print(f"SHA-256: {manifest['csv_sha256']}")


if __name__ == "__main__":
    main()
