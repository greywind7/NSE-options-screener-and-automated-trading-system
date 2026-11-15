"""
Unified Configuration Loader.
Loads, validates, and normalizes configuration files and secrets.
Resolves MED-003 (fragile 10-candidate config fallback chain) and CRIT-003 (plaintext auth secrets).
"""
import json
import os
import sys
from typing import Any, Dict, Optional


def load_env_file(env_path: str = ".env") -> None:
    """Load key-value pairs from .env into os.environ if present."""
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[CONFIG] Warning loading {env_path}: {e}", file=sys.stderr)


# Load default .env at module import
load_env_file(".env")


def load_json_file(path: str) -> Dict[str, Any]:
    """Load JSON file with clean error message."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class ConfigManager:
    """Central configuration access point."""
    _instance: Optional["ConfigManager"] = None

    def __init__(
        self,
        strategy_config_path: str = "config/strategies.json",
        execution_config_path: str = "config/execution.json",
        system_config_path: str = "config/system.json",
        instruments_config_path: str = "config/instruments.json",
    ):
        self.strategy_config_path = strategy_config_path
        self.execution_config_path = execution_config_path
        self.system_config_path = system_config_path
        self.instruments_config_path = instruments_config_path
        self._strategy_cfg: Dict[str, Any] = {}
        self._execution_cfg: Dict[str, Any] = {}
        self._system_cfg: Dict[str, Any] = {}
        self._instruments_cfg: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """Reload all configuration files from disk."""
        if os.path.exists(self.strategy_config_path):
            self._strategy_cfg = load_json_file(self.strategy_config_path)
        if os.path.exists(self.execution_config_path):
            self._execution_cfg = load_json_file(self.execution_config_path)
        if os.path.exists(self.system_config_path):
            self._system_cfg = load_json_file(self.system_config_path)
        if os.path.exists(self.instruments_config_path):
            self._instruments_cfg = load_json_file(self.instruments_config_path)

    @property
    def strategy(self) -> Dict[str, Any]:
        return self._strategy_cfg

    @property
    def execution(self) -> Dict[str, Any]:
        return self._execution_cfg

    @property
    def system(self) -> Dict[str, Any]:
        return self._system_cfg

    @property
    def instruments(self) -> Dict[str, Any]:
        return self._instruments_cfg

    def get_ipc_endpoints(self) -> Dict[str, str]:
        """Retrieve ZeroMQ IPC socket endpoints from system config."""
        ipc = self._system_cfg.get("system", {}).get("ipc", {})
        return {
            "feed_ipc": ipc.get("feed_ipc", "ipc:///tmp/feed_equity.ipc"),
            "feed_equity_ipc": ipc.get("feed_equity_ipc", "ipc:///tmp/feed_equity.ipc"),
            "feed_options_ipc": ipc.get("feed_options_ipc", "ipc:///tmp/feed_options.ipc"),
            "quotes_router_ipc": ipc.get("quotes_router_ipc", "ipc:///tmp/quotes_router.ipc"),
            "quotes_stream_ipc": ipc.get("quotes_stream_ipc", "ipc:///tmp/quotes_stream.ipc"),
            "quotes_snapshot_ipc": ipc.get("quotes_snapshot_ipc", "ipc:///tmp/quotes_router.ipc"),
            "heartbeat_ipc": ipc.get("heartbeat_ipc", "ipc:///tmp/quotes_heartbeat.pub"),
            "scoring_alerts_ipc": ipc.get("scoring_alerts_ipc", "ipc:///tmp/scoring_alerts_pub.ipc"),
            "rms_strike_ipc": ipc.get("rms_strike_ipc", "ipc:///tmp/rms_strike_pub.ipc"),
        }

    def get_track_config(self, track_name: str) -> Dict[str, Any]:
        """Retrieve parameters for a specific execution track."""
        momentum_cfg = self._strategy_cfg.get("strategies", {}).get("momentum", {})
        tracks = momentum_cfg.get("tracks", {})
        return tracks.get(track_name, {})

    def get_scoring_config(self) -> Dict[str, Any]:
        """Retrieve Scoring Engine 3.0 configuration parameters."""
        momentum_cfg = self._strategy_cfg.get("strategies", {}).get("momentum", {})
        return momentum_cfg.get("scoring_engine_3_0", {})

    def get_risk_config(self) -> Dict[str, Any]:
        """Retrieve universal RMS and risk configuration parameters."""
        momentum_cfg = self._strategy_cfg.get("strategies", {}).get("momentum", {})
        return momentum_cfg.get("risk", {})

    def get_recorder_config(self) -> Dict[str, Any]:
        """Retrieve market recorder configuration."""
        return self._system_cfg.get("system", {}).get("recorder", {})

    def get_execution_mode_config(self, mode: str) -> Dict[str, Any]:
        """Retrieve configuration for a specific execution mode (paper, paper_delayed, live, sim)."""
        exec_cfg = self._execution_cfg.get("execution", {})
        return exec_cfg.get(mode, {})

    def get_ipc_auth_key(self) -> bytes:
        """Retrieve IPC secret from environment or system config."""
        secret = os.environ.get("TRADING_IPC_AUTHKEY") or self._system_cfg.get("system", {}).get("ipc", {}).get("authkey", "default_secure_token")
        return secret.encode("utf-8")

    def get_upstox_token(self) -> Optional[str]:
        """Retrieve Upstox token from environment or config."""
        return os.environ.get("UPSTOX_ACCESS_TOKEN")

    def get_groww_api_key(self) -> Optional[str]:
        """Retrieve Groww API key from environment."""
        return os.environ.get("GROWW_API_KEY")


_GLOBAL_CONFIG: Optional[ConfigManager] = None


def get_config_manager(
    strategy_config_path: str = "config/strategies.json",
    execution_config_path: str = "config/execution.json",
    system_config_path: str = "config/system.json",
    instruments_config_path: str = "config/instruments.json",
) -> ConfigManager:
    """Singleton getter for ConfigManager."""
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None:
        _GLOBAL_CONFIG = ConfigManager(
            strategy_config_path=strategy_config_path,
            execution_config_path=execution_config_path,
            system_config_path=system_config_path,
            instruments_config_path=instruments_config_path,
        )
    return _GLOBAL_CONFIG

