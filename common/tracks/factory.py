"""
Dynamic Track Construction Factory.
Builds strategy tracks from configuration-driven 'enabled_tracks' schema.
Supports:
- Base tracks with explicit strike modes (e.g. fast_momentum_ATM, fast_momentum_ITM)
- Composite tracks with sub-track compositions and shared vs equal-share pools.
"""
from typing import Any, Dict, List, Optional
from common.interfaces import ExecutionContext
from common.tracks.base_track import BaseTrack
from common.tracks.fast_momentum import FastMomentumTrack
from common.tracks.trend_pullback import TrendPullbackTrack
from common.tracks.fixed_target import FixedTargetTrack
from common.tracks.composite_track import CompositeTrack
from core.config.config_loader import get_config_manager

TRACK_CLASS_MAP = {
    "fast_momentum": FastMomentumTrack,
    "trend_pullback": TrendPullbackTrack,
    "fixed_target": FixedTargetTrack,
    "composite": CompositeTrack,
}


def _get_momentum_config(strategy_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if strategy_cfg is not None:
        if "strategies" in strategy_cfg and "momentum" in strategy_cfg["strategies"]:
            return strategy_cfg["strategies"]["momentum"]
        elif "momentum" in strategy_cfg:
            return strategy_cfg["momentum"]
        return strategy_cfg

    cfg = get_config_manager()
    return cfg.strategy.get("strategies", {}).get("momentum", {})


def _get_enabled_tracks_spec(momentum_cfg: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    return (
        momentum_cfg.get("enabled_tracks")
        or momentum_cfg.get("enabled")
        or momentum_cfg.get("tracks", {}).get("enabled_tracks")
        or momentum_cfg.get("tracks", {}).get("enabled")
    )


def get_enabled_track_names(strategy_cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    Extract top-level executable track names from strategy configuration.
    Example output for standard config:
        ['fast_momentum_ATM', 'fast_momentum_ITM', 'combination']
    """
    momentum_cfg = _get_momentum_config(strategy_cfg)
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
) -> BaseTrack:
    p = track_params or {}
    cls = TRACK_CLASS_MAP.get(base_type, FastMomentumTrack)

    if base_type == "fast_momentum":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.12)),
            sl_pct=float(p.get("sl_pct", 0.08)),
            max_trades=int(p.get("max_trades", 2)),
            preferred_mode=preferred_mode,
            parent_track=parent_track,
        )
    elif base_type == "trend_pullback":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.25)),
            sl_pct=float(p.get("sl_pct", 0.12)),
            max_trades=int(p.get("max_trades", 1)),
            trailing_step_pct=float(p.get("trailing_step_pct", 0.02)),
            preferred_mode=preferred_mode,
            parent_track=parent_track,
        )
    elif base_type == "fixed_target":
        return cls(
            context=context,
            track_name=track_name,
            target_pct=float(p.get("target_pct", 0.15)),
            sl_pct=float(p.get("sl_pct", 0.10)),
            fixed_target_inr=float(p.get("fixed_target_inr", 750.0)),
            max_trades=int(p.get("max_trades", 2)),
            preferred_mode=preferred_mode,
            parent_track=parent_track,
        )
    else:
        return cls(
            context=context,
            track_name=track_name,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
        )


def create_tracks(
    context: ExecutionContext,
    track_filter: Optional[List[str]] = None,
    strategy_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Construct track instances based on configuration and optional track_filter.
    Returns: Dict[str, Union[BaseTrack, CompositeTrack]] mapping track_name -> instance.
    """
    momentum_cfg = _get_momentum_config(strategy_cfg)
    spec = _get_enabled_tracks_spec(momentum_cfg)
    track_params_dict = momentum_cfg.get("tracks", {})

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
            p = track_params_dict.get(t_name, {})
            tracks[t_name] = _instantiate_base_track(
                base_type=t_name,
                track_name=t_name,
                context=context,
                track_params=p,
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
            base_params = track_params_dict.get(t_name, {})
            # If the exact base name was explicitly requested in filter (e.g. track_filter=["fast_momentum"])
            if active_filter and t_name in active_filter:
                tracks[t_name] = _instantiate_base_track(
                    base_type=t_name,
                    track_name=t_name,
                    context=context,
                    preferred_mode="ATM",
                    track_params=base_params,
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
                    )
            elif not (active_filter and t_name in active_filter):
                if not active_filter or t_name in active_filter:
                    tracks[t_name] = _instantiate_base_track(
                        base_type=t_name,
                        track_name=t_name,
                        context=context,
                        track_params=base_params,
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
