#!/usr/bin/env python3
"""
Initialize the database schema.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from database.schema import init_database, get_schema_version

def main():
    """Initialize the database."""
    print("Initializing database...")

    try:
        # Load config
        config = load_config()
        db_path = config['database']['path']

        # Check if database exists
        if Path(db_path).exists():
            version = get_schema_version(db_path)
            print(f"Database exists at {db_path} (schema version: {version})")
        else:
            print(f"Creating new database at {db_path}")

        # Initialize schema
        init_database(config)

        # Verify
        version = get_schema_version(db_path)
        print(f"Database initialized successfully (schema version: {version})")

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
