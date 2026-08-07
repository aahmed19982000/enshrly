import time

from django.core.management.base import BaseCommand

from syndicator.ai_utils import fetch_news_items_from_source
from syndicator.models import AISource


class Command(BaseCommand):
    help = (
        "Re-tests every AISource flagged use_proxy=True with a DIRECT (no proxy) "
        "fetch, to find sources that no longer need (or never needed) to be "
        "routed through SCRAPING_PROXY_URL - each one routed unnecessarily burns "
        "proxy bandwidth/quota for no reason, which is what caused the repeated "
        "402 Payment Required outages. Sources whose direct fetch still fails "
        "keep use_proxy on; sources whose direct fetch succeeds are safe to turn "
        "it off for. Defaults to a dry run - pass --apply to actually flip them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually set use_proxy=False on sources confirmed to work without it. Without this flag, only reports.",
        )

    def handle(self, *args, **options):
        sources = AISource.objects.filter(use_proxy=True).order_by('name')
        total = sources.count()
        if not total:
            self.stdout.write("No sources currently flagged use_proxy=True.")
            return

        self.stdout.write(f"Testing {total} proxy-routed source(s) directly (no proxy)...\n")

        can_disable = []
        still_needed = []

        for i, source in enumerate(sources, 1):
            errors = []
            items = fetch_news_items_from_source(
                source.url, proxies=None, error_out=errors, limit=5, use_gemini_fallback=False,
            )
            if items:
                can_disable.append(source)
                self.stdout.write(self.style.SUCCESS(
                    f"[{i}/{total}] {source.name}: يعمل مباشرة بدون بروكسي ({len(items)} عنصر) - يمكن إيقاف use_proxy."
                ))
            else:
                still_needed.append(source)
                reason = errors[0] if errors else "لا توجد عناصر"
                self.stdout.write(self.style.WARNING(
                    f"[{i}/{total}] {source.name}: لسه محتاج بروكسي - {reason}"
                ))
            if i < total:
                time.sleep(1)

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"يمكن إيقاف use_proxy لـ {len(can_disable)} مصدر: " + ", ".join(s.name for s in can_disable))
        self.stdout.write(f"لسه محتاجين البروكسي ({len(still_needed)}): " + ", ".join(s.name for s in still_needed))

        if options["apply"] and can_disable:
            AISource.objects.filter(pk__in=[s.pk for s in can_disable]).update(use_proxy=False)
            self.stdout.write(self.style.SUCCESS(f"\nتم إيقاف use_proxy فعليًا لـ {len(can_disable)} مصدر."))
        elif can_disable:
            self.stdout.write("\n(dry run - أعد التشغيل بإضافة --apply عشان تطبّق الإيقاف فعليًا)")
