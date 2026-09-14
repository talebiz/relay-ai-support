"""
Management command: seed the default company policy documents into
ChromaDB if it is currently empty.

Run with:  python manage.py seed_documents

This replaces the hard-coded _SEED_DOCUMENTS that Part 4's
knowledge_base_tools.py loaded lazily on first query. Now the seed
data goes through the same ingestion path (chunk_text + collection.add
with full metadata) as user-uploaded documents, so the retrieval tool
sees a uniform metadata shape regardless of how a document got there.

Safe to run multiple times: if the collection already has documents,
this is a no-op.
"""

from django.core.management.base import BaseCommand

from documents.services import seed_default_documents


class Command(BaseCommand):
    help = "Seed default company policy documents into ChromaDB (no-op if already populated)."

    def handle(self, *args, **options):
        seeded = seed_default_documents()
        if not seeded:
            self.stdout.write(self.style.WARNING(
                "ChromaDB collection already has documents — nothing seeded."
            ))
            return

        for doc in seeded:
            self.stdout.write(
                f"  Seeded: {doc['title']} ({doc['chunks']} chunk(s))"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(seeded)} default document(s) into ChromaDB."
        ))
