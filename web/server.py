"""
Web Server: Asynchronous HTTP and WebSocket application orchestrating Live, Historical, and Simulation services.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
from datetime import datetime
import json
import os
import signal
import sys
from typing import Any, Dict, List, Optional, Set
import uuid
from aiohttp import web, WSMsgType

from core.common.time_utils import IST
from web.candle_service import CandleService
from web.historical_service import HistoricalService
from web.live_service import LiveService
from web.simulation_service import SimulationService
from web.alert_service import AlertService


class AnalyticsWebServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        recordings_dir: str = "recordings",
    ):
        self.host = host
        self.port = port
        self.recordings_dir = recordings_dir

        self.server_boot_id: str = uuid.uuid4().hex[:12]
        self.server_start_time: str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

        self.live_service = LiveService(recordings_dir=recordings_dir)
        self.historical_service = HistoricalService(recordings_dir=recordings_dir)
        self.simulation_service = SimulationService(recordings_dir=recordings_dir)
        self.candle_service = CandleService(recordings_dir=recordings_dir)
        self.alert_service = AlertService(recordings_dir=recordings_dir)

        self.app = web.Application()
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self._ws_broadcast_task: Optional[asyncio.Task] = None

        self._setup_routes()
        atexit.register(self.teardown)

    def teardown(self) -> None:
        """Synchronous teardown invoked by atexit or server shutdown."""
        try:
            self.simulation_service.cleanup()
        except Exception:
            pass

    def _setup_routes(self) -> None:
        # Static files
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_dir, exist_ok=True)
        os.makedirs(os.path.join(static_dir, "css"), exist_ok=True)
        os.makedirs(os.path.join(static_dir, "js"), exist_ok=True)

        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_static("/static", static_dir, name="static")

        # General status
        self.app.router.add_get("/api/status", self.handle_api_status)

        # 1. Live Routes
        self.app.router.add_get("/api/live/overview", self.handle_live_overview)
        self.app.router.add_get("/api/live/track/{mode}/{track}", self.handle_live_track_detail)
        self.app.router.add_get("/api/live/ws", self.handle_live_ws)

        # 2. Historical Routes
        self.app.router.add_get("/api/historical/dates", self.handle_historical_dates)
        self.app.router.add_get("/api/historical/overview", self.handle_historical_overview)
        self.app.router.add_get("/api/historical/track/{mode}/{track}", self.handle_historical_track_detail)

        # 3. Simulation Routes
        self.app.router.add_get("/api/sim/config", self.handle_sim_config)
        self.app.router.add_get("/api/sim/status", self.handle_sim_status)
        self.app.router.add_post("/api/sim/run", self.handle_sim_run)
        self.app.router.add_get("/api/sim/results", self.handle_sim_results)
        self.app.router.add_get("/api/sim/runs", self.handle_sim_runs_list)
        self.app.router.add_get("/api/sim/run/{run_id}", self.handle_sim_run_detail)
        self.app.router.add_post("/api/sim/clear", self.handle_sim_clear)
        self.app.router.add_get("/sim/results", self.handle_sim_results_page)

        # 4. Chart / Candle Aggregation Routes
        self.app.router.add_get("/api/chart/candles", self.handle_chart_candles)

        # 5. Alert Routes
        self.app.router.add_get("/api/alerts", self.handle_alerts)
        self.app.router.add_get("/api/alerts/candles", self.handle_alert_candles)

        # Lifecycle hooks
        self.app.on_startup.append(self.on_startup)
        self.app.on_cleanup.append(self.on_cleanup)

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve main SPA HTML page."""
        index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html")
        return web.Response(text="Web UI Initializing...", content_type="text/plain")

    async def handle_api_status(self, request: web.Request) -> web.Response:
        """Return general backend state."""
        live_feed_active = bool(self.live_service.lvc)
        return web.json_response({
            "status": "online",
            "server_boot_id": self.server_boot_id,
            "server_start_time": self.server_start_time,
            "server_time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "live_date": self.live_service.get_today_date_str(),
            "live_feed_active": live_feed_active,
            "cached_sim_date": self.simulation_service.cached_date,
            "sim_running": self.simulation_service.is_running,
        })

    # 1. Live Handlers
    async def handle_live_overview(self, request: web.Request) -> web.Response:
        data = self.live_service.scan_live_journals(force=False)
        return web.json_response(data)

    async def handle_live_track_detail(self, request: web.Request) -> web.Response:
        mode = request.match_info["mode"]
        track = request.match_info["track"]
        data = self.live_service.get_track_detail(mode, track)
        return web.json_response(data)

    async def handle_live_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket connection streaming live telemetry and MTM updates to UI."""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        self.ws_clients.add(ws)

        try:
            # Send initial state immediately
            initial_data = self.live_service.scan_live_journals(force=True)
            await ws.send_json({"type": "LIVE_OVERVIEW", "payload": initial_data})

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        req = json.loads(msg.data)
                        action = req.get("action")
                        if action == "PING":
                            await ws.send_json({"type": "PONG"})
                        elif action == "GET_TRACK":
                            mode = req.get("mode", "paper")
                            track = req.get("track", "")
                            detail = self.live_service.get_track_detail(mode, track)
                            await ws.send_json({"type": "TRACK_DETAIL", "payload": detail})
                    except Exception:
                        pass
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.ws_clients.discard(ws)

        return ws

    async def _ws_broadcast_loop(self) -> None:
        """Periodic broadcast of live telemetry to all connected WebSocket clients."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if self.ws_clients:
                    live_data = self.live_service.scan_live_journals(force=False)
                    msg_text = json.dumps({"type": "LIVE_UPDATE", "payload": live_data})
                    dead_clients = set()
                    for ws in self.ws_clients:
                        if not ws.closed:
                            try:
                                await ws.send_str(msg_text)
                            except Exception:
                                dead_clients.add(ws)
                        else:
                            dead_clients.add(ws)
                    self.ws_clients -= dead_clients
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)

    # 2. Historical Handlers
    async def handle_historical_dates(self, request: web.Request) -> web.Response:
        dates = self.historical_service.get_available_dates()
        return web.json_response({"dates": dates})

    async def handle_historical_overview(self, request: web.Request) -> web.Response:
        target_date = request.query.get("date")
        if not target_date:
            dates = self.historical_service.get_available_dates()
            dates_with_journals = [d for d in dates if d.get("has_journals")]
            if dates_with_journals:
                target_date = dates_with_journals[-1]["date"]
            elif dates:
                target_date = dates[-1]["date"]
            else:
                target_date = datetime.now(IST).strftime("%Y-%m-%d")
        overview = self.historical_service.get_historical_overview(target_date)
        return web.json_response(overview)

    async def handle_historical_track_detail(self, request: web.Request) -> web.Response:
        mode = request.match_info["mode"]
        track = request.match_info["track"]
        target_date = request.query.get("date") or datetime.now(IST).strftime("%Y-%m-%d")
        detail = self.historical_service.get_historical_track_detail(target_date, mode, track)
        return web.json_response(detail)

    # 3. Simulation Handlers
    async def handle_sim_config(self, request: web.Request) -> web.Response:
        cfg = self.simulation_service.get_default_config()
        return web.json_response(cfg)

    async def handle_sim_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "is_running": self.simulation_service.is_running,
            "current_job_id": self.simulation_service.current_job_id,
            "progress_status": self.simulation_service.progress_status,
            "cached_date": self.simulation_service.cached_date,
            "last_run_time": round(self.simulation_service.last_run_time, 2),
            "has_results": self.simulation_service.latest_results is not None,
            "error": self.simulation_service.latest_error,
            "server_boot_id": self.server_boot_id,
        })

    async def handle_sim_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        if self.simulation_service.is_running:
            return web.json_response({"error": "Simulation already running"}, status=409)

        # Launch background task and yield once so run_simulation_job initializes state
        task = asyncio.create_task(self.simulation_service.run_simulation_job(body))
        await asyncio.sleep(0)

        return web.json_response({
            "status": "started",
            "run_id": self.simulation_service.current_job_id,
            "message": "Simulation launched in background worker.",
        })

    async def handle_sim_results(self, request: web.Request) -> web.Response:
        if self.simulation_service.is_running:
            return web.json_response({
                "status": "running",
                "progress": self.simulation_service.progress_status,
            })
        if self.simulation_service.latest_results:
            return web.json_response({
                "status": "completed",
                "results": self.simulation_service.latest_results,
            })
        if self.simulation_service.latest_error:
            return web.json_response({
                "status": "error",
                "error": self.simulation_service.latest_error,
            }, status=500)
        return web.json_response({"status": "idle", "results": None})

    async def handle_sim_results_page(self, request: web.Request) -> web.Response:
        """Serve standalone clean simulation results HTML page for opening in a new browser tab."""
        page_path = os.path.join(os.path.dirname(__file__), "static", "sim_results.html")
        if os.path.exists(page_path):
            with open(page_path, "r", encoding="utf-8") as f:
                content = f.read()
            return web.Response(text=content, content_type="text/html")
        return web.Response(text="Simulation Results Page Not Found", status=404)

    async def handle_sim_runs_list(self, request: web.Request) -> web.Response:
        """Return list of simulation runs in this session along with server boot identifier."""
        runs = [
            {
                "id": r["id"],
                "date": r["date"],
                "runtime": r["runtime"],
                "timestamp": r.get("timestamp", ""),
            }
            for r in self.simulation_service.runs_history.values()
        ]
        return web.json_response({
            "server_boot_id": self.server_boot_id,
            "runs": runs,
        })

    async def handle_sim_clear(self, request: web.Request) -> web.Response:
        """Clear all simulation runs, cached data, worker tasks, and temporary simulation files."""
        self.simulation_service.cleanup()
        return web.json_response({
            "status": "cleared",
            "message": "All simulation queue runs, temporary files, and cached datasets cleared.",
            "server_boot_id": self.server_boot_id,
        })

    async def handle_sim_run_detail(self, request: web.Request) -> web.Response:
        """Return full details, parameters, matrix, and trade curves for a specific simulation run."""
        run_id = request.match_info.get("run_id", "")
        detail = self.simulation_service.get_run_detail(run_id)
        if detail:
            return web.json_response({"status": "ok", "run": detail})
        return web.json_response({"error": f"Run '{run_id}' not found"}, status=404)

    # 4. Candle Aggregation Handler
    async def handle_chart_candles(self, request: web.Request) -> web.Response:
        """
        Return 5-minute (or requested timeframe) OHLCV candles for cash equity or option contract.
        Query params:
          date: YYYY-MM-DD or YYYYMMDD
          token: Instrument token
          symbol: Symbol name
          is_option: '1' or 'true' for option, else '0' or 'false'
          interval: minutes per candle (default: 5)
        """
        target_date = request.query.get("date") or self.live_service.get_today_date_str()
        token = request.query.get("token", "")
        symbol = request.query.get("symbol", "")
        is_opt_raw = request.query.get("is_option", "0").lower()
        is_option = is_opt_raw in ("1", "true", "yes")
        try:
            interval = int(request.query.get("interval", 5))
        except (ValueError, TypeError):
            interval = 5

        candles = self.candle_service.get_candles(
            target_date=target_date,
            token=token,
            symbol=symbol,
            is_option=is_option,
            interval_minutes=interval,
        )
        return web.json_response({
            "date": target_date,
            "token": token,
            "symbol": symbol,
            "is_option": is_option,
            "interval": interval,
            "candles": candles,
        })

    # 5. Alert Handlers
    async def handle_alerts(self, request: web.Request) -> web.Response:
        """Return setup alerts for requested date (defaults to today)."""
        target_date = request.query.get("date")
        data = self.alert_service.get_alerts(target_date)
        return web.json_response(data)

    async def handle_alert_candles(self, request: web.Request) -> web.Response:
        """
        Return 5-minute candles and trigger marker for a specific alert symbol.
        Attaches to live CandleManager SHM if online and caches locally.
        """
        symbol = request.query.get("symbol", "")
        target_date = request.query.get("date")
        trigger_p_raw = request.query.get("trigger_price")
        trigger_ts_raw = request.query.get("trigger_ts")
        t_p = float(trigger_p_raw) if trigger_p_raw else None
        t_ts = float(trigger_ts_raw) if trigger_ts_raw else None

        data = self.alert_service.get_alert_candles(
            symbol=symbol,
            target_date=target_date,
            trigger_price=t_p,
            trigger_ts=t_ts,
        )
        return web.json_response(data)

    # Lifecycles
    async def on_startup(self, app: web.Application) -> None:
        await self.live_service.start()
        self._ws_broadcast_task = asyncio.create_task(self._ws_broadcast_loop())

    async def on_cleanup(self, app: web.Application) -> None:
        if self._ws_broadcast_task:
            self._ws_broadcast_task.cancel()
        await self.live_service.stop()
        self.simulation_service.cleanup()
        for ws in set(self.ws_clients):
            await ws.close()

    def run(self) -> None:
        """Run web server synchronously."""
        print("=" * 80)
        print(f"🚀 ANTIGRAVITY ANALYTICS & SIMULATION WEB UI")
        print(f"URL: http://localhost:{self.port} (or http://{self.host}:{self.port})")
        print("=" * 80)

        def _sig_handler(sig, frame):
            print("\n[SERVER] Shutdown signal received. Exiting immediately...")
            self.teardown()
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except Exception:
            pass

        try:
            web.run_app(self.app, host=self.host, port=self.port, access_log=None)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.teardown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Analytics & Simulation Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Host address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP Port (default: 8080)")
    parser.add_argument("--record-dir", default="recordings", help="Recordings directory (default: recordings)")
    args = parser.parse_args()

    server = AnalyticsWebServer(host=args.host, port=args.port, recordings_dir=args.record_dir)
    server.run()


if __name__ == "__main__":
    main()
