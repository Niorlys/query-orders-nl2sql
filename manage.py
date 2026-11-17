"""Command-line entry point for project management tasks."""

import argparse
import csv
import sqlite3
from pathlib import Path

from generator import DEFAULT_OUTPUT_DIR, DEFAULT_SEED, write_dataset
from storage import DEFAULT_DATABASE_PATH, clean_to_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description="Herramientas del demo de pedidos")
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", help="Generar CSV crudo y manifiesto")
    generate_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    clean_parser = commands.add_parser("clean", help="Limpiar el CSV y cargarlo en SQLite")
    clean_parser.add_argument("--input", type=Path, default=DEFAULT_OUTPUT_DIR / "pedidos.csv")
    clean_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    if args.command == "generate":
        manifest = write_dataset(args.output_dir, args.seed)
        print(f"Pedidos originales: {manifest['original_order_count']}")
        print(f"Filas totales: {manifest['total_csv_row_count']}")
        print(f"Inyecciones: {manifest['injection_counts']}")
        print(f"CSV y manifiesto: {args.output_dir.resolve()}")
        print(f"SHA-256: {manifest['csv_sha256']}")
    elif args.command == "clean":
        try:
            summary = clean_to_sqlite(args.input, args.database)
        except (OSError, UnicodeError, csv.Error, sqlite3.Error, ValueError) as error:
            parser.exit(1, f"Error al limpiar/cargar pedidos: {error}\n")
        print(f"Filas leídas: {summary['total']}")
        print(f"Cargadas (pedidos): {summary['accepted']}")
        print(f"Duplicadas (pedidos_duplicados): {summary['duplicate']}")
        print(f"Rechazadas (pedidos_rechazados): {summary['rejected']}")
        print("Motivos de rechazo (una fila puede tener varios):")
        if summary["rejection_reasons"]:
            for reason, count in summary["rejection_reasons"].items():
                print(f"  {reason}: {count}")
        else:
            print("  Sin rechazos.")
        print(f"SQLite: {args.database.resolve()}")
        print("Conteos verificados en SQLite. Carga completada.")


if __name__ == "__main__":
    main()
