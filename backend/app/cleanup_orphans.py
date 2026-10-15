"""
One-time cleanup: permanently delete products that nobody is tracking
(plus their offers and price history).

Run from the backend folder, with the venv active:
    python -m app.cleanup_orphans            # only lists what would be deleted
    python -m app.cleanup_orphans --delete   # actually deletes them
"""

import sys

from app import models  # noqa: F401  (registers tables)
from app.database import SessionLocal
from app.services.cleanup import delete_product_completely, find_orphan_products


def main() -> None:
    do_delete = "--delete" in sys.argv
    db = SessionLocal()
    try:
        orphans = find_orphan_products(db)
        if not orphans:
            print("No untracked products found. Nothing to do.")
            return

        print(f"{len(orphans)} product(s) are not tracked by anyone:")
        for product in orphans:
            print(f"  id={product.id}  {product.title[:80]}")

        if not do_delete:
            print("\nNothing deleted. Run again with --delete to remove them.")
            return

        for product in orphans:
            delete_product_completely(db, product.id)
        db.commit()
        print(f"\nDeleted {len(orphans)} product(s) permanently.")
    finally:
        db.close()


if __name__ == "__main__":
    main()