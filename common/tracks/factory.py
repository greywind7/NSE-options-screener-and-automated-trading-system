"""
Dynamic Track Construction Factory.

Builds and instantiates strategy execution tracks from configuration-driven schemas.
Supports:
- Track profile configuration loading from `config/tracks/{profile}.json` or direct paths.
  Presets: 'default', 'v1', 'v2', or user-defined JSON files.
- Standalone Base Tracks with explicit moneyness modes (e.g. fast_momentum_ATM, delta_scalp_ITM1).
- Composite Tracks with sub-track compositions and shared vs equal-share capital pools.
- Full registry of v1 baseline tracks and v2 multi-regime tracks (12 total archetypes).
"""
import json
import os
from typing import Any, Dict, List, Optional
from common.interfaces import ExecutionContext
from common.tracks.base_track import BaseTrack
from common.tracks.fast_momentum import FastMomentumTrack
from common.tracks.trend_pullback import TrendPullbackTrack
from common.tracks.fixed_target import FixedTargetTrack
from common.tracks.composite_track import CompositeTrack
from common.tracks.structural_convexity import StructuralConvexityTrack
from common.tracks.opening_thrust_convex import OpeningThrustConvexityTrack
from common.tracks.delta_scalp import DeltaScalpTrack
from common.tracks.micro_chandelier import MicroChandelierTrack
from common.tracks.gamma_runner import GammaRunnerTrack
from common.tracks.volatility_expansion import VolatilityExpansionTrack
from common.tracks.adaptive_regime import AdaptiveRegimeTrack
from common.tracks.trend_pullback_v2 import TrendPullbackV2Track
from core.config.config_loader import get_config_manager

TRACK_CLASS_MAP = {
    # v1 baseline tracks
    "fast_momentum": FastMomentumTrack,
    "trend_pullback": TrendPullbackTrack,
    "fixed_target": FixedTargetTrack,
    "composite": CompositeTrack,
    # v2 multi-regime tracks
    "structural_convexity": StructuralConvexityTrack,
    "opening_thrust_convex": OpeningThrustConvexityTrack,
    "delta_scalp": DeltaScalpTrack,
    "micro_chandelier": MicroChandelierTrack,
    "gamma_runner": GammaRunnerTrack,
    "volatility_expansion": VolatilityExpansionTrack,
    "adaptive_regime": AdaptiveRegimeTrack,
    "trend_pullback_v2": TrendPullbackV2Track,
}


def load_track_profile(profile_name_or_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Load a track configuration profile from config/tracks/{name}.json or direct path.
    Supported presets: 'default', 'v1', 'v2', or custom profile filename.
    """
    if not profile_name_or_path:
        profile_name_or_path = "default"

    # If direct path
    if os.path.exists(profile_name_or_path) and os.path.isfile(profile_name_or_path):
        with open(profile_name_or_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # Check config/tracks/ directory
    cand_path = os.path.join("config", "tracks", f"{profile_name_or_path}.json")
    if os.path.exists(cand_path):
        with open(cand_path, "r", encoding="utf-8") as f:
            return json.load(f)

    if profile_name_or_path.endswith(".json"):
        cand_path2 = os.path.join("config", "tracks", profile_name_or_path)
        if os.path.exists(cand_path2):
            with open(cand_path2, "r", encoding="utf-8") as f:
                return json.load(f)

    return None


def _get_momentum_config(
    strategy_cfg: Optional[Dict[str, Any]] = None,
    track_profile: Optional[str] = None,
) -> Dict[str, Any]:
    if strategy_cfg is not None:
        prof = track_profile or strategy_cfg.get("track_profile") or strategy_cfg.get("profile")
        if prof:
            loaded = load_track_profile(prof)
            if loaded:
                res = dict(strategy_cfg)
                res["enabled_tracks"] = loaded.get("enabled_tracks", [])
                t_dict = dict(res.get("tracks", {}))
                t_dict.update(loaded.get("tracks", {}))
                res["tracks"] = t_dict
                return res

        if "strategies" in strategy_cfg and "momentum" in strategy_cfg["strategies"]:
            return strategy_cfg["strategies"]["momentum"]
        elif "momentum" in strategy_cfg:
            return strategy_cfg["momentum"]
        return strategy_cfg

    if track_profile:
        loaded = load_track_profile(track_profile)
        if loaded:
            return loaded

    default_prof = load_track_profile("default")
    if default_prof:
        return default_prof

    cfg = get_config_manager()
    return cfg.strategy.get("strategies", {}).get("momentum", {})


def _get_enabled_tracks_spec(momentum_cfg: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    return (
        momentum_cfg.get("enabled_tracks")
        or momentum_cfg.get("enabled")
        or momentum_cfg.get("tracks", {}).get("enabled_tracks")
        or momentum_cfg.get("tracks", {}).get("enabled")
    )


def get_enabled_track_names(
    strategy_cfg: Optional[Dict[str, Any]] = None,
    track_profile: Optional[str] = None,
) -> List[str]:
    """
    Extract top-level executable track names from strategy configuration or profile.
    Example output for standard config:
        ['fast_momentum_ATM', 'fast_momentum_ITM', 'combination']
    """
    momentum_cfg = _get_momentum_config(strategy_cfg, track_profile=track_profile)
    spec = _get_enabled_tracks_spec(momentum_cfg)

    if not spec:
        # Fallback legacy track list
        t_dict = momentum_cfg.get("tracks", {})
        if t_dict:
            return list(t_dict.keys())
        return ["fast_momentum", "trend_pullback", "fixed_target", "composite"]

    names: List[str] = []
    for entry in spec:
        if not isinstance(entry, dict):
            if isinstance(entry, str):
                names.append(entry)
            continue

        t_type = entry.get("type", "base").lower()
        t_name = entry.get("name")
        if not t_name:
            continue

        if t_type == "base":
            modes = entry.get("modes", [])
            if modes:
                for m in modes:
                    names.append(f"{t_name}_{m}")
            else:
                names.append(t_name)
        elif t_type == "composite":
            names.append(t_name)

    return names


def _parse_composition(composition: Any) -> List[tuple]:
    """Normalize composition structures (list of dicts or single dict) into [(sub_name, mode), ...]."""
    items = []
    if isinstance(composition, list):
        for elem in composition:
            if isinstance(elem, dict):
                for sub_name, modes in elem.items():
                    if isinstance(modes, list):
                        for m in modes:
                            items.append((sub_name, m))
                    elif isinstance(modes, str):
                        items.append((sub_name, modes))
    elif isinstance(composition, dict):
        for sub_name, modes in composition.items():
            if isinstance(modes, list):
                for m in modes:
                    items.append((sub_name, m))
            elif isinstance(modes, str):
                items.append((sub_name, modes))
    return items


def _instantiate_base_track(
    base_type: str,
    track_name: str,
    context: ExecutionContext,
    preferred_mode: Optional[str] = None,
    parent_track: Optional[str] = None,
    track_params: Optional[Dict[str, Any]] = None,
    entry_freeze_time: Optional[str] = None,
    eod_squareoff_time: Optional[str] = None,
) -> BaseTrack:
    p = track_params or {}
    cls = TRACK_CLASS_MAP.get(base_type, FastMomentumTrack)
    cap = float(p.get("capital", 250000.0))
    be_val = p.get("breakeven_trigger_pct")
    be_trigger = float(be_val) if be_val is not None else 0.025
    if be_trigger > 1.0:
        be_trigger /= 100.0

    frz_time = entry_freeze_time or p.get("entry_freeze_time")
    eod_time = eod_squareoff_time or p.get("eod_squareoff_time")

    if base_type == "fast_momentum":
        stagnation = float(p.get("stagnation_timeout_sec") or p.get("timeout_sec") or 300.0)
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.12)),
            sl_pct=float(p.get("sl_pct", 0.08)),
            breakeven_trigger_pct=be_trigger,
            target_decay_sec=float(p.get("target_decay_sec", 180.0)),
            decay_floor_pct=float(p.get("decay_floor_pct", 0.04)),
            stagnation_timeout_sec=stagnation,
            max_trades=int(p.get("max_trades", 2)),
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "trend_pullback":
        timeout = float(p.get("timeout_sec") or p.get("stagnation_timeout_sec") or 1800.0)
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.25)),
            sl_pct=float(p.get("sl_pct", 0.12)),
            breakeven_trigger_pct=be_trigger,
            max_trades=int(p.get("max_trades", 1)),
            trailing_step_pct=float(p.get("trailing_step_pct", 0.02)),
            timeout_sec=timeout,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "fixed_target":
        timeout = float(p.get("timeout_sec") or p.get("stagnation_timeout_sec") or 600.0)
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.15)),
            sl_pct=float(p.get("sl_pct", 0.10)),
            breakeven_trigger_pct=be_trigger,
            fixed_target_inr=float(p.get("fixed_target_inr", 750.0)),
            timeout_sec=timeout,
            max_trades=int(p.get("max_trades", 2)),
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "structural_convexity":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 1.0)),
            sl_pct=float(p.get("sl_pct", 0.12)),
            breakeven_trigger_pct=float(p.get("breakeven_trigger_pct", 0.08)),
            profit_trail_threshold_pct=float(p.get("profit_trail_threshold_pct", 0.15)),
            profit_trail_ratio=float(p.get("profit_trail_ratio", 0.50)),
            preferred_mode=preferred_mode or "ATM",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "opening_thrust_convex":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.35)),
            sl_pct=float(p.get("sl_pct", 0.06)),
            breakeven_trigger_pct=float(p.get("breakeven_trigger_pct", 0.15)),
            cutoff_time=str(p.get("cutoff_time", "10:45")),
            preferred_mode=preferred_mode or "ATM",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "delta_scalp":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.07)),
            sl_pct=float(p.get("sl_pct", 0.04)),
            velocity_timeout_sec=float(p.get("velocity_timeout_sec", 180.0)),
            preferred_mode=preferred_mode or "ITM2",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "micro_chandelier":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.10)),
            sl_pct=float(p.get("sl_pct", 0.05)),
            stagnation_timeout_sec=float(p.get("stagnation_timeout_sec", 300.0)),
            preferred_mode=preferred_mode or "ITM1",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "gamma_runner":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.75)),
            sl_pct=float(p.get("sl_pct", 0.15)),
            preferred_mode=preferred_mode or "OTM2",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "volatility_expansion":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.25)),
            sl_pct=float(p.get("sl_pct", 0.08)),
            breakeven_trigger_pct=float(p.get("breakeven_trigger_pct", 0.10)),
            deceleration_timeout_sec=float(p.get("deceleration_timeout_sec", 300.0)),
            preferred_mode=preferred_mode or "ATM",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "adaptive_regime":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.18)),
            sl_pct=float(p.get("sl_pct", 0.07)),
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    elif base_type == "trend_pullback_v2":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.25)),
            sl_pct=float(p.get("sl_pct", 0.06)),
            breakeven_trigger_pct=float(p.get("breakeven_trigger_pct", 0.04)),
            trailing_step_pct=float(p.get("trailing_step_pct", 0.008)),
            timeout_sec=float(p.get("timeout_sec", 3600.0)),
            preferred_mode=preferred_mode or "ITM1",
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )
    else:
        return cls(
            context=context,
            track_name=track_name,
            breakeven_trigger_pct=be_trigger,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            allocated_capital=cap,
            entry_freeze_time=frz_time,
            eod_squareoff_time=eod_time,
        )


def create_tracks(
    context: ExecutionContext,
    track_filter: Optional[List[str]] = None,
    strategy_cfg: Optional[Dict[str, Any]] = None,
    entry_freeze_time: Optional[str] = None,
    eod_squareoff_time: Optional[str] = None,
    track_profile: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construct and wire track instances based on configuration, selected profile, and filters.

    Args:
        context: ExecutionContext (Paper, Delayed, Live, or Headless Sim).
        track_filter: Optional subset of track names to instantiate. If single token matches a
                      profile preset ('v1', 'v2', 'default'), it switches profile accordingly.
        strategy_cfg: Optional dictionary overriding strategy configuration.
        entry_freeze_time: Optional 'HH:MM' cutoff time for new entries.
        eod_squareoff_time: Optional 'HH:MM' auto-exit time for open trades.
        track_profile: Specific profile name ('default', 'v1', 'v2') or JSON filepath.

    Returns:
        Dict[str, Union[BaseTrack, CompositeTrack]] mapping effective track_name -> instance.
    """
    eff_profile = track_profile
    if track_filter and len(track_filter) == 1:
        cand_prof = track_filter[0].strip().lower()
        if cand_prof in ("v1", "v2", "default") or os.path.exists(os.path.join("config", "tracks", f"{cand_prof}.json")):
            eff_profile = cand_prof
            track_filter = None

    momentum_cfg = _get_momentum_config(strategy_cfg, track_profile=eff_profile)
    spec = _get_enabled_tracks_spec(momentum_cfg)
    track_params_dict = momentum_cfg.get("tracks", {})

    cfg_mgr = get_config_manager()
    exec_cfg = cfg_mgr.execution.get("execution", {})

    eff_freeze = (
        entry_freeze_time
        or momentum_cfg.get("entry_freeze_time")
        or exec_cfg.get("entry_freeze_time")
        or exec_cfg.get(f"{context.mode}_entry_freeze_time")
        or "15:00"
    )

    eff_squareoff = (
        eod_squareoff_time
        or momentum_cfg.get("eod_squareoff_time")
        or exec_cfg.get("eod_squareoff_time")
        or exec_cfg.get(f"{context.mode}_eod_squareoff_time")
        or "15:15"
    )

    eff_brokerage = (
        momentum_cfg.get("brokerage_per_order")
        or exec_cfg.get("brokerage_per_order")
        or exec_cfg.get(f"{context.mode}", {}).get("brokerage_per_order")
        or 30.0
    )

    active_filter: Optional[set] = None
    if track_filter:
        active_filter = {t.strip() for t in track_filter if t.strip() and t.strip().upper() != "ALL"}
        if not active_filter:
            active_filter = None

    tracks: Dict[str, Any] = {}

    if not spec:
        # Legacy fallback
        track_names = get_enabled_track_names(strategy_cfg)
        for t_name in track_names:
            if active_filter and t_name not in active_filter:
                continue
            p = dict(track_params_dict.get(t_name, {}))
            p.setdefault("brokerage_per_order", eff_brokerage)
            tracks[t_name] = _instantiate_base_track(
                base_type=t_name,
                track_name=t_name,
                context=context,
                track_params=p,
                entry_freeze_time=eff_freeze,
                eod_squareoff_time=eff_squareoff,
            )
        return tracks

    for entry in spec:
        if not isinstance(entry, dict):
            continue

        t_type = entry.get("type", "base").lower()
        t_name = entry.get("name")
        if not t_name:
            continue

        if t_type == "base":
            modes = entry.get("modes", [])
            base_params = dict(track_params_dict.get(t_name, {}))
            base_params.setdefault("brokerage_per_order", eff_brokerage)
            if "capital" in entry:
                base_params["capital"] = entry["capital"]
            # If the exact base name was explicitly requested in filter (e.g. track_filter=["fast_momentum"])
            if active_filter and t_name in active_filter:
                tracks[t_name] = _instantiate_base_track(
                    base_type=t_name,
                    track_name=t_name,
                    context=context,
                    preferred_mode="ATM",
                    track_params=base_params,
                    entry_freeze_time=eff_freeze,
                    eod_squareoff_time=eff_squareoff,
                )
            if modes:
                for m in modes:
                    full_name = f"{t_name}_{m}"
                    if active_filter and full_name not in active_filter:
                        continue
                    tracks[full_name] = _instantiate_base_track(
                        base_type=t_name,
                        track_name=full_name,
                        context=context,
                        preferred_mode=m,
                        track_params=base_params,
                        entry_freeze_time=eff_freeze,
                        eod_squareoff_time=eff_squareoff,
                    )
            elif not (active_filter and t_name in active_filter):
                if not active_filter or t_name in active_filter:
                    tracks[t_name] = _instantiate_base_track(
                        base_type=t_name,
                        track_name=t_name,
                        context=context,
                        track_params=base_params,
                        entry_freeze_time=eff_freeze,
                        eod_squareoff_time=eff_squareoff,
                    )

        elif t_type == "composite":
            if active_filter and t_name not in active_filter:
                # Also check if user directly asked for any of its sub-tracks
                sub_pairs = _parse_composition(entry.get("composition", []))
                for sub_base, sub_m in sub_pairs:
                    sub_key = f"{sub_base}_{sub_m}"
                    if sub_key in active_filter:
                        p = track_params_dict.get(sub_base, {})
                        tracks[sub_key] = _instantiate_base_track(
                            base_type=sub_base,
                            track_name=sub_key,
                            context=context,
                            preferred_mode=sub_m,
                            track_params=p,
                            entry_freeze_time=eff_freeze,
                            eod_squareoff_time=eff_squareoff,
                        )
                continue

            shared_pool = bool(entry.get("shared_pool", True))
            comp_spec = entry.get("composition", [])
            sub_pairs = _parse_composition(comp_spec)

            sub_instances: List[BaseTrack] = []
            for sub_base, sub_m in sub_pairs:
                sub_track_name = f"{sub_base}_{sub_m}"
                p = track_params_dict.get(sub_base, {})
                sub_inst = _instantiate_base_track(
                    base_type=sub_base,
                    track_name=sub_track_name,
                    context=context,
                    preferred_mode=sub_m,
                    parent_track=t_name,
                    track_params=p,
                    entry_freeze_time=eff_freeze,
                    eod_squareoff_time=eff_squareoff,
                )
                sub_instances.append(sub_inst)

            comp_params = track_params_dict.get("composite", {})
            total_cap = float(entry.get("capital") or comp_params.get("capital") or 250000.0)

            composite_inst = CompositeTrack(
                context=context,
                track_name=t_name,
                sub_tracks=sub_instances,
                shared_pool=shared_pool,
                total_capital=total_cap,
            )
            tracks[t_name] = composite_inst

    return tracks

    return tracks
