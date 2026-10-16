"""BlueAgent CTFd Plugin Entrypoint.

Registers:
- Custom Challenge Type: 'blueagent_patch'
- Flask routes blueprint
- Static assets directory
- Admin control panel menu bar link
- Database schema tables
"""

import logging

from CTFd.plugins import (
    register_admin_plugin_menu_bar,
    register_plugin_assets_directory,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES

from .challenge import BlueAgentPatchChallengeType
from .models import (
    BlueAgentDeployment,
    BlueAgentPatchChallenge,
    BlueAgentTeamMapping,
    BlueAgentVerifyLog,
)
from .routes import blueagent_bp

logger = logging.getLogger(__name__)


def load(app):
    """Initializes and binds the BlueAgent plugin into CTFd runtime."""
    with app.app_context():
        # 1. Initialize plugin database tables
        try:
            app.db.create_all()
        except Exception as exc:
            logger.error("Failed to initialize BlueAgent database tables: %s", exc)

        # 2. Register custom challenge type
        CHALLENGE_CLASSES["blueagent_patch"] = BlueAgentPatchChallengeType

        # 3. Register Flask Blueprint for player and admin endpoints
        app.register_blueprint(blueagent_bp)

        # 4. Register static assets directory (/plugins/blueagent/assets/...)
        register_plugin_assets_directory(app, base_path="/plugins/blueagent/assets/")

        # 5. Register admin navigation menu bar item
        register_admin_plugin_menu_bar(title="BlueAgent", route="/admin/plugins/blueagent")

        logger.info("BlueAgent CTFd plugin successfully loaded.")
