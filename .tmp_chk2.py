from app.config.settings import settings
from app.core.oauth_login_guard import _cleanup_task
print("cleanup_enabled:", settings.oauth_attempt_cleanup_enabled)
print("interval:", settings.oauth_attempt_cleanup_interval_sec)
print("module task:", _cleanup_task)