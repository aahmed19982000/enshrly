from decimal import Decimal

from django.core.management.base import BaseCommand

from syndicator.models import AIImportLog


class Command(BaseCommand):
    help = (
        "Zeroes out estimated_cost on AIImportLog rows created for source-fetch "
        "failures (error_message contains 'فشل جلب المصدر'), where no AI call "
        "was ever made so the legacy token-fallback cost never should have applied."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many rows would be updated without changing anything.",
        )

    def handle(self, *args, **options):
        qs = AIImportLog.objects.filter(
            status="failed", error_message__contains="فشل جلب المصدر"
        ).exclude(estimated_cost=0)
        count = qs.count()

        if options["dry_run"]:
            self.stdout.write(f"Would zero out estimated_cost on {count} row(s).")
            return

        qs.update(estimated_cost=Decimal("0"))
        self.stdout.write(self.style.SUCCESS(f"Zeroed out estimated_cost on {count} row(s)."))
