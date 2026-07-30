"""
Local load testing for enshrly - safe, read-mostly, side-effect-free endpoints only.

Run:
    python manage.py runserver
    locust -f locustfile.py --host=http://127.0.0.1:8000
Then open http://localhost:8089 to set user count/spawn rate, or run headless:
    locust -f locustfile.py --host=http://127.0.0.1:8000 -u 20 -r 5 --run-time 30s --headless

Deliberately EXCLUDED (would have real side effects or burn real quota if load
tested): checkout/payment views, WhatsApp OTP send (signup/login - also rate
limited, see accounts/utils.py:check_rate_limit, so hammering them from many
concurrent "users" sharing one IP would just trip the rate limiter rather than
measure real performance), wp_connect_api_view (creates a real WordPressSite
and burns a real WPConnectionToken per call), and anything touching
run_ai_generation_cycle (real Gemini API cost per call).
"""
from locust import HttpUser, task, between


class AnonymousBrowsingUser(HttpUser):
    wait_time = between(1, 3)

    @task(3)
    def home_page(self):
        self.client.get("/", name="home")

    @task(2)
    def packages_page(self):
        self.client.get("/payments/packages/", name="packages")

    @task(1)
    def wp_plugin_data_api(self):
        # Read-only, unauthenticated (no token) - sanity-checks the
        # AISourceGroup/WordPressScheduleSlot query performance under load
        # as those tables grow, without touching any customer data.
        self.client.get("/api/wp-plugin-data/", name="wp_plugin_data_api")
